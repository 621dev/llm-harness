"""run 예산 상한 테스트 (2026-07-29, ECC `cost-aware-llm-pipeline` 재분석에서 도입).

**고치기 전**: `metrics.json`에 비용을 사후 기록만 했고 `src/`에 `budget` 개념이 0건이었다.
라운드/턴 **횟수** 상한은 있었지만 **금액 상한이 없어** run이 얼마를 쓰든 멈출 장치가
없었다. 라운드 수가 같아도 프롬프트/응답이 길면 금액은 몇 배가 된다.

여기서 고정하는 것:

- 상한이 없으면(기본값) 아무것도 막지 않는다 — 기존 run 동작 그대로
- 상한에 걸리면 **다음 호출을 시작하지 않는다**(이미 쓴 돈은 되돌릴 수 없다)
- 상한에 걸린 run은 error가 아니라 **partial** — 이미 만든 산출물을 버리면 그때까지 쓴
  비용이 통째로 낭비된다
- 금액과 구독 호출은 **서로 다른 자원**이라 각각 상한이 따로 걸린다
- 실패한 호출도 예산을 깎는다(재시도로 소모한 한도는 되돌아오지 않는다)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import model_runner, orchestrator, run_store  # noqa: E402
from harness.budget import BudgetTracker  # noqa: E402
from harness.schemas import Candidate, ProviderConfig, TaskInput  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402


class _CountingProvider(Provider):
    """호출마다 지정한 비용을 청구하는 provider. 실제 호출은 전혀 없다."""

    def __init__(
        self,
        provider_id: str,
        *,
        auth_mode: str = "api_key",
        cost_usd: float | None = 0.01,
        fail: bool = False,
    ) -> None:
        super().__init__(
            ProviderConfig(provider_id=provider_id, model_id=provider_id, auth_mode=auth_mode)
        )
        self.cost_usd = cost_usd
        self.fail = fail
        self.calls = 0

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.calls += 1
        if self.fail:
            raise ProviderError(f"{self.provider_id} 실패 주입")
        return Candidate(model_id=self.model_id, content="결과", cost_usd=self.cost_usd)


class BudgetTrackerTest(unittest.TestCase):
    def test_unlimited_by_default(self) -> None:
        """상한을 안 주면 아무것도 막지 않는다 — 기본값이 기존 동작이어야 한다."""
        tracker = BudgetTracker()

        for _ in range(100):
            tracker.add(Candidate(model_id="m", content="x", cost_usd=999.0, subscription_calls=9))

        self.assertTrue(tracker.unlimited)
        self.assertFalse(tracker.exhausted)

    def test_usd_limit_triggers_at_or_above(self) -> None:
        tracker = BudgetTracker(limit_usd=0.10)

        tracker.add(Candidate(model_id="m", content="x", cost_usd=0.09))
        self.assertFalse(tracker.exhausted)

        tracker.add(Candidate(model_id="m", content="x", cost_usd=0.01))
        # 정확히 상한에 닿아도 더 쓰지 않는다. 0.09+0.01이 부동소수점 때문에
        # 0.09999999999999999가 되는 것까지 여기서 고정한다(허용 오차 없이는 상한을
        # 그냥 지나간다 — 이 테스트가 구현 버그를 잡았다).
        self.assertTrue(tracker.exhausted)

    def test_subscription_calls_limit_is_separate_from_money(self) -> None:
        """구독 호출은 cost_usd가 None이라 금액 상한으로는 절대 안 걸린다."""
        tracker = BudgetTracker(limit_usd=100.0, limit_calls=2)

        tracker.add(Candidate(model_id="m", content="x", cost_usd=None, subscription_calls=2))

        self.assertEqual(tracker.spent_usd, 0.0)  # 금액으로는 0원
        self.assertTrue(tracker.exhausted)  # 그래도 횟수 상한에 걸린다
        self.assertIn("구독 호출 상한", tracker.reason)

    def test_reason_names_which_limit_was_hit(self) -> None:
        """errors.json에 그대로 들어가는 문장이라 어느 상한인지 구분돼야 한다."""
        self.assertIn("예산 상한", BudgetTracker(limit_usd=0.0).reason)
        self.assertIn("걸리지 않았다", BudgetTracker(limit_usd=1.0).reason)


class GenerateWithRetryBudgetTest(unittest.TestCase):
    def test_call_is_not_started_when_exhausted(self) -> None:
        """핵심: 상한을 넘긴 뒤에는 provider를 아예 부르지 않는다."""
        provider = _CountingProvider("p")
        tracker = BudgetTracker(limit_usd=0.005)  # 첫 호출(0.01)로 바로 초과

        first = model_runner.generate_with_retry(provider, "질문", budget=tracker)
        second = model_runner.generate_with_retry(provider, "질문", budget=tracker)

        self.assertEqual(first.status, "success")
        self.assertEqual(provider.calls, 1)  # 두 번째는 호출 자체가 없었다
        self.assertEqual(second.status, "error")
        self.assertIn("(budget)", second.content)
        self.assertEqual(second.subscription_calls, 0)  # 호출 안 했으니 소모 0

    def test_budget_error_is_distinguishable_from_provider_failure(self) -> None:
        """마스킹 금지 — provider 실패와 예산 중단이 같은 문구로 보이면 안 된다."""
        blocked = model_runner.generate_with_retry(
            _CountingProvider("p"), "질문", budget=BudgetTracker(limit_usd=0.0)
        )
        failed = model_runner.generate_with_retry(_CountingProvider("q", fail=True), "질문")

        self.assertIn("(budget)", blocked.content)
        self.assertIn("(error)", failed.content)

    def test_failed_attempts_also_consume_budget(self) -> None:
        """재시도로 소모한 한도는 되돌아오지 않으므로 예산에서도 빠져야 한다."""
        provider = _CountingProvider("p", auth_mode="cli_subscription", fail=True)
        tracker = BudgetTracker(limit_calls=10)

        model_runner.generate_with_retry(provider, "질문", budget=tracker)

        self.assertEqual(tracker.subscription_calls, model_runner.MAX_RETRIES + 1)


class RunAllBudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="budget-runall-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-budget", root=self.tmp_dir)

    def test_remaining_providers_are_not_called(self) -> None:
        """상한에 걸리면 남은 provider는 건드리지 않고 루프를 끝낸다."""
        providers = [_CountingProvider(f"p{i}", cost_usd=0.01) for i in range(4)]
        tracker = BudgetTracker(limit_usd=0.02)

        candidates = model_runner.run_all("질문", providers, self.run_dir, budget=tracker)

        self.assertEqual(len(candidates), 2)  # 2개까지만 만들어졌다
        self.assertEqual([p.calls for p in providers], [1, 1, 0, 0])

    def test_no_limit_runs_everything(self) -> None:
        providers = [_CountingProvider(f"p{i}") for i in range(4)]

        candidates = model_runner.run_all("질문", providers, self.run_dir, budget=BudgetTracker())

        self.assertEqual(len(candidates), 4)


class RefinementBudgetTest(unittest.TestCase):
    """라운드마다 생성+판정 2회를 쓰므로 예산이 가장 빨리 마르는 패턴이다."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="budget-refine-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self._saved = (orchestrator.BUDGET_USD, orchestrator.BUDGET_SUBSCRIPTION_CALLS)

        def restore() -> None:
            orchestrator.BUDGET_USD, orchestrator.BUDGET_SUBSCRIPTION_CALLS = self._saved

        self.addCleanup(restore)

    def run_refinement(self, *, limit_usd: float | None) -> tuple[Path, dict]:
        orchestrator.BUDGET_USD = limit_usd
        # rubric을 통과시키지 않는 evaluator를 써서 라운드가 상한까지 돌게 만든다
        providers = {
            "gen": _CountingProvider("gen", cost_usd=0.01),
            orchestrator.JUDGE_PROVIDER_KEY: _RejectingJudge("judge"),
        }
        task = TaskInput(
            task_id="budget-refine",
            prompt="이 문서를 반복 개선해줘. " * 10,
            constraints=["team_pattern:iterative_refinement"],
        )
        orchestrator.run(task, providers, root=self.tmp_dir)
        run_dir = self.tmp_dir / "run-budget-refine"
        return run_dir, run_store.read_json(run_dir, "errors.json")

    def test_budget_stops_rounds_early_and_reports_why(self) -> None:
        run_dir, errors = self.run_refinement(limit_usd=0.015)  # 1라운드 뒤 초과

        rounds = run_store.read_json(run_dir, "refinement.json")
        self.assertEqual(len(rounds), 1)  # 2라운드는 시작하지 않았다
        self.assertTrue(any("예산 상한" in e["message"] for e in errors))

    def test_result_is_partial_not_discarded(self) -> None:
        """상한에 걸렸다고 1라운드 산출물을 버리면 그때까지 쓴 비용이 낭비된다."""
        run_dir, _ = self.run_refinement(limit_usd=0.015)

        final = run_store.read_markdown(run_dir, "final.md")
        self.assertTrue(final.startswith("(partial)"))
        self.assertIn("결과", final)


class AgentSubscriptionCallsCountedTest(unittest.TestCase):
    """에이전트가 쓴 구독 호출이 예산에 반영되는지 (2026-07-29에 누락 발견).

    `budget.add`는 `generate_with_retry`에만 있었는데 **에이전트는 그 경로를 안 지난다** —
    턴 상한만큼(기본 8회) 구독을 쓰면서 예산 추적에는 0으로 보였다. `agentic_task`가
    구독 한도를 가장 많이 쓰는 패턴이라(§8) 이게 빠지면 `budget_subscription_calls`
    상한이 정작 가장 비싼 패턴을 못 막는다.

    **이 run 안에서는 막을 게 없다**(에이전트 뒤에 다른 호출이 없다). 그래도 고쳐야 하는
    이유는 기록이 틀리면 **상한값을 정할 근거 자체가 틀어지기** 때문이다 — 20이라는 값도
    패턴별 이론상 최대에서 나왔다.
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="budget-agent-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_agent_turns_are_added_to_the_tracker(self) -> None:
        from harness.schemas import AgentRunResult, AgentToolUse, AgentTurn

        turns = [
            AgentTurn(turn_index=i, text="작업", tool_uses=[AgentToolUse(tool="Write", target="a.md")])
            for i in range(1, 6)
        ]

        class _FakeAgent(Provider):
            def generate(self, prompt: str, *, temperature: float = 0.7):  # 에이전트 경로에선 안 쓰인다
                raise AssertionError("generate가 불려선 안 된다")

            def run_agent(self, prompt, workspace, *, max_turns, **kwargs):
                (workspace / "a.md").write_text("본문", encoding="utf-8")
                return AgentRunResult(
                    turns=turns, final_text="완료", num_turns=len(turns),
                    latency_ms=10, cost_usd=None, subscription_calls=len(turns),
                    stop_reason="completed",
                )

        agent = _FakeAgent(
            ProviderConfig(provider_id="agent", model_id="claude-cli", auth_mode="cli_subscription")
        )
        run_dir = run_store.create_run(run_id="run-agent-budget", root=self.tmp_dir)
        tracker = BudgetTracker(limit_calls=20)

        orchestrator._run_agentic_task(
            TaskInput(task_id="agent-budget", prompt="파일을 만들어줘"),
            {orchestrator.AGENT_PROVIDER_KEY: agent},
            run_dir,
            tracker,
        )

        self.assertEqual(tracker.subscription_calls, 5)

    def test_agent_does_not_start_when_calls_are_exhausted(self) -> None:
        """에이전트는 턴 상한까지 여러 번 호출하므로 시작 자체를 막아야 한다 —
        중간에 끊으면 파일을 쓰다 만 상태가 남는다."""

        class _MustNotRun(Provider):
            def generate(self, prompt: str, *, temperature: float = 0.7):
                raise AssertionError("호출되면 안 된다")

            def run_agent(self, *a, **k):
                raise AssertionError("예산이 소진됐는데 에이전트가 시작됐다")

        agent = _MustNotRun(
            ProviderConfig(provider_id="agent", model_id="claude-cli", auth_mode="cli_subscription")
        )
        run_dir = run_store.create_run(run_id="run-agent-blocked", root=self.tmp_dir)
        tracker = BudgetTracker(limit_calls=1)
        tracker.add(Candidate(model_id="m", content="x", subscription_calls=1))  # 소진

        observation = orchestrator._run_agentic_task(
            TaskInput(task_id="agent-blocked", prompt="파일을 만들어줘"),
            {orchestrator.AGENT_PROVIDER_KEY: agent},
            run_dir,
            tracker,
        )

        self.assertEqual(observation.status, "error")
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any(e.get("kind") == "budget" for e in errors))


class _RejectingJudge(_CountingProvider):
    """rubric을 절대 통과시키지 않는 evaluator — 라운드를 상한까지 돌리기 위한 것."""

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.calls += 1
        return Candidate(
            model_id=self.model_id,
            content='{"passed": false, "feedback": "더 구체적으로 써주세요"}',
            cost_usd=self.cost_usd,
        )


if __name__ == "__main__":
    unittest.main()
