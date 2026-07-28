"""구독 호출 횟수 집계 테스트 (Section 9 Cost Blindness 방지).

배경: `estimated_cost_usd`는 `auth_mode="api_key"`(종량제)만 채운다. 구독
provider(claude/codex CLI)는 cost_usd가 None이라 **비용 지표상 $0으로 보이지만
실제로는 5시간/주간 롤링 한도를 소모하는 실비용**이다 — 2026-07-27 구조 검토에서
"횟수조차 안 센다"는 갭을 확인하고 추가했다.

금액과 한도는 서로 다른 자원이라 합칠 수 없으므로, 금액 대신 **호출 횟수**로
따로 기록한다. 여기서 고정하는 것:
- 재시도도 한도를 소모하니 함께 센다(실패한 시도라고 공짜가 아니다)
- 종량제 provider는 0 — 이미 cost_usd로 보이므로 이중 계상하지 않는다
- 패턴마다 집계 경로가 다른데(후보/체인/라운드/에이전트 턴) 전부 metrics로 모인다
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import model_runner, orchestrator, run_store  # noqa: E402
from harness.schemas import Candidate, ProviderConfig, TaskInput  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402


class _StubProvider(Provider):
    """auth_mode를 지정할 수 있는 최소 provider. 실제 호출은 전혀 없다."""

    def __init__(self, provider_id: str, *, auth_mode: str, fail_times: int = 0, content: str = "결과"):
        super().__init__(
            ProviderConfig(provider_id=provider_id, model_id=provider_id, auth_mode=auth_mode)
        )
        self.fail_times = fail_times
        self.content = content
        self.calls = 0

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise ProviderError(f"{self.provider_id} 실패 주입 {self.calls}")
        return Candidate(model_id=self.model_id, content=self.content, cost_usd=None)


class GenerateWithRetryCountingTest(unittest.TestCase):
    def test_subscription_call_counted_once_on_success(self) -> None:
        provider = _StubProvider("claude-cli", auth_mode="cli_subscription")

        candidate = model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(candidate.subscription_calls, 1)

    def test_retry_also_consumes_quota(self) -> None:
        """실패한 시도도 한도를 깎는다 — 1회 실패 후 성공이면 2회로 세야 한다."""
        provider = _StubProvider("claude-cli", auth_mode="cli_subscription", fail_times=1)

        candidate = model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(candidate.status, "success")
        self.assertEqual(candidate.subscription_calls, 2)

    def test_counted_even_when_all_attempts_fail(self) -> None:
        """끝내 실패해도 소모된 한도는 되돌아오지 않으므로 기록에 남아야 한다."""
        provider = _StubProvider("claude-cli", auth_mode="cli_subscription", fail_times=99)

        candidate = model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(candidate.status, "error")
        self.assertEqual(candidate.subscription_calls, model_runner.MAX_RETRIES + 1)

    def test_api_key_provider_counts_zero(self) -> None:
        """종량제는 cost_usd로 이미 보이므로 여기서 이중 계상하지 않는다."""
        provider = _StubProvider("gemini", auth_mode="api_key")

        candidate = model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(candidate.subscription_calls, 0)


class MetricsAggregationTest(unittest.TestCase):
    """패턴별로 집계 경로가 달라서, 각 경로가 metrics까지 실제로 도달하는지 확인한다."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="sub-calls-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def metrics_of(self, run_id: str) -> dict:
        return run_store.read_json(self.tmp_dir / run_id, "metrics.json")

    def test_hierarchical_delegation_sums_chain_steps(self) -> None:
        task = TaskInput(task_id="sub-chain", prompt="NCP XX를 조사해줘. 그 다음 검토해줘.")
        providers = {
            "research-mock": _StubProvider("research-mock", auth_mode="cli_subscription"),
            "design_review-mock": _StubProvider("design_review-mock", auth_mode="cli_subscription"),
            "implementation_review-mock": _StubProvider(
                "implementation_review-mock", auth_mode="cli_subscription"
            ),
        }

        orchestrator.run(task, providers, root=self.tmp_dir)

        # 기본 체인은 research + design_review 2스텝 = 구독 호출 2회
        self.assertEqual(self.metrics_of("run-sub-chain")["subscription_calls"], 2)

    def test_mixed_auth_modes_count_only_subscription(self) -> None:
        """같은 run 안에 종량제와 구독이 섞여 있으면 구독분만 세야 한다."""
        task = TaskInput(task_id="sub-mixed", prompt="NCP XX를 조사해줘. 그 다음 검토해줘.")
        providers = {
            "research-mock": _StubProvider("research-mock", auth_mode="api_key"),
            "design_review-mock": _StubProvider("design_review-mock", auth_mode="cli_subscription"),
            "implementation_review-mock": _StubProvider(
                "implementation_review-mock", auth_mode="api_key"
            ),
        }

        orchestrator.run(task, providers, root=self.tmp_dir)

        self.assertEqual(self.metrics_of("run-sub-mixed")["subscription_calls"], 1)

    def test_direct_call_path_records_calls(self) -> None:
        """적합성 게이트 탈락 경로도 구독을 쓰면 기록돼야 한다(누락되기 쉬운 경로)."""
        task = TaskInput(task_id="sub-direct", prompt="1+1은?")  # 짧아서 게이트 탈락
        providers = {"claude": _StubProvider("claude", auth_mode="cli_subscription")}

        orchestrator.run(task, providers, root=self.tmp_dir)

        self.assertEqual(self.metrics_of("run-sub-direct")["subscription_calls"], 1)

    def test_metrics_defaults_to_zero_when_no_subscription_used(self) -> None:
        task = TaskInput(task_id="sub-none", prompt="1+1은?")
        providers = {"gemini": _StubProvider("gemini", auth_mode="api_key")}

        orchestrator.run(task, providers, root=self.tmp_dir)

        self.assertEqual(self.metrics_of("run-sub-none")["subscription_calls"], 0)


class DashboardSubscriptionColumnTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="sub-calls-dash-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def make_run(self, run_id: str, *, pattern: str, calls: int | None) -> None:
        run_dir = run_store.create_run(run_id=run_id, root=self.tmp_dir)
        run_store.write_json(run_dir, "plan.json", {"team_pattern": pattern})
        metrics = {"latency_ms": 100, "estimated_cost_usd": None}
        if calls is not None:
            metrics["subscription_calls"] = calls
        run_store.write_json(run_dir, "metrics.json", metrics)
        run_store.write_markdown(run_dir, "final.md", "결과")
        run_store.write_json(run_dir, "errors.json", [])

    def test_totals_are_summed_not_averaged(self) -> None:
        """구독 한도는 'run당 평균'이 아니라 '누적 소모량'이 관리 대상이다."""
        from harness import dashboard

        self.make_run("run-a", pattern="agentic_task", calls=5)
        self.make_run("run-b", pattern="agentic_task", calls=3)

        report = dashboard.build_dashboard(root=self.tmp_dir)

        stats = next(p for p in report.patterns if p.team_pattern == "agentic_task")
        self.assertEqual(stats.total_subscription_calls, 8)

    def test_old_runs_without_the_field_are_treated_as_zero(self) -> None:
        """이 필드 도입 이전 run의 metrics.json에는 키가 없다 — 깨지면 안 된다."""
        from harness import dashboard

        self.make_run("run-old", pattern="fan_out_judge", calls=None)

        report = dashboard.build_dashboard(root=self.tmp_dir)

        stats = next(p for p in report.patterns if p.team_pattern == "fan_out_judge")
        self.assertEqual(stats.total_subscription_calls, 0)


if __name__ == "__main__":
    unittest.main()
