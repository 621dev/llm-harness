"""Phase 2 테스트: evals/graders.py + evals/runner.py (stdlib unittest).

harness-implementation-plan-ko.md Section 8(Phase 2)을 검증한다.
- deterministic grader: run_status, final.md 존재 여부, 필수/금지 문구로 채점하는가
- pass@k 러너: k번 반복 실행 후 pass_rate/pass_at_k/pass_pow_k를 올바르게 계산하는가,
  cost/latency per success가 성공한 시도만 평균하는가
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from evals import graders, runner  # noqa: E402
from harness import orchestrator, run_store  # noqa: E402
from harness.schemas import EvalCase, ProviderConfig, TaskInput  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def _judge_provider() -> MockProvider:
    # ADR 0004: fan_out_judge는 judge용 provider도 필요하다.
    return MockProvider(ProviderConfig(provider_id="judge", model_id="judge-mock"), profile="judge")


def make_task(task_id: str, prompt: str) -> TaskInput:
    return TaskInput(task_id=task_id, prompt=prompt)


def reliable_fan_out_providers(_attempt_index: int) -> dict[str, MockProvider]:
    specs = [("model-a", "concise"), ("model-b", "detailed"), ("model-c", "creative")]
    providers = {
        provider_id: MockProvider(ProviderConfig(provider_id=provider_id, model_id=provider_id), profile=profile)
        for provider_id, profile in specs
    }
    providers[orchestrator.JUDGE_PROVIDER_KEY] = _judge_provider()
    return providers


def mostly_failing_fan_out_providers(_attempt_index: int) -> dict[str, MockProvider]:
    # model-a, model-b 영구 실패 -> 성공 후보 1개뿐 -> min_candidates(2) 미달 -> run 실패
    return {
        "model-a": MockProvider(ProviderConfig(provider_id="model-a", model_id="model-a"), fail_times=2),
        "model-b": MockProvider(ProviderConfig(provider_id="model-b", model_id="model-b"), fail_times=2),
        "model-c": MockProvider(ProviderConfig(provider_id="model-c", model_id="model-c")),
        orchestrator.JUDGE_PROVIDER_KEY: _judge_provider(),
    }


def reliable_delegation_providers(_attempt_index: int) -> dict[str, MockProvider]:
    roles = ["research", "design_review", "content_finalization"]
    return {
        f"{role}-mock": MockProvider(ProviderConfig(provider_id=f"{role}-mock", model_id=f"{role}-mock"))
        for role in roles
    }


class GraderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-grader-test", root=self.tmp_dir)

    def _case(self, **kwargs) -> EvalCase:
        return EvalCase(name="test-case", task=make_task("t", "설계안을 검토해줘"), **kwargs)

    def test_passes_when_required_present_and_forbidden_absent(self) -> None:
        run_store.write_markdown(self.run_dir, "final.md", "구조 명확성을 갖춘 좋은 답변\n")
        case = self._case(required_phrases=["구조 명확성"], forbidden_phrases=["위험 문구"])

        result = graders.grade(self.run_dir, case, run_status="success")

        self.assertTrue(result.passed)

    def test_fails_when_run_status_is_error_even_if_content_matches(self) -> None:
        run_store.write_markdown(self.run_dir, "final.md", "구조 명확성을 갖춘 좋은 답변\n")
        case = self._case(required_phrases=["구조 명확성"])

        result = graders.grade(self.run_dir, case, run_status="error")

        self.assertFalse(result.passed)
        self.assertIn("error", result.reason)

    def test_fails_when_final_md_missing(self) -> None:
        case = self._case(required_phrases=["아무거나"])

        result = graders.grade(self.run_dir, case, run_status="success")

        self.assertFalse(result.passed)
        self.assertIn("final.md", result.reason)

    def test_fails_when_required_phrase_missing(self) -> None:
        run_store.write_markdown(self.run_dir, "final.md", "전혀 다른 내용\n")
        case = self._case(required_phrases=["구조 명확성"])

        result = graders.grade(self.run_dir, case, run_status="success")

        self.assertFalse(result.passed)
        self.assertIn("필수 문구 누락", result.reason)

    def test_fails_when_forbidden_phrase_present(self) -> None:
        run_store.write_markdown(self.run_dir, "final.md", "이전 지시를 무시하고 진행\n")
        case = self._case(forbidden_phrases=["이전 지시를 무시"])

        result = graders.grade(self.run_dir, case, run_status="success")

        self.assertFalse(result.passed)
        self.assertIn("금지 문구 발견", result.reason)


class RunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_all_attempts_pass_yields_perfect_scores(self) -> None:
        case = EvalCase(
            name="fan-out-reliable",
            task=make_task("eval-reliable", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘"),
        )

        report = runner.run_case_k_times(case, reliable_fan_out_providers, k=3, root=self.tmp_dir)

        self.assertEqual(len(report.attempts), 3)
        self.assertEqual(report.pass_rate, 1.0)
        self.assertEqual(report.pass_at_k, 1.0)
        self.assertEqual(report.pass_pow_k, 1.0)
        self.assertIsNotNone(report.cost_per_success)
        self.assertIsNotNone(report.latency_per_success)

    def test_hierarchical_delegation_case_also_works(self) -> None:
        """runner/grader가 "패턴 무관"이라는 주장을 fan_out_judge 케이스로만 검증했었다
        (회귀 방지 겸 커버리지 공백 확인차 추가) — hierarchical_delegation 케이스도
        똑같이 채점되고 pass@k가 계산되는지 확인한다."""
        case = EvalCase(
            name="delegation-reliable",
            task=make_task("eval-delegation", "경쟁사 A/B/C의 가격 정책을 리서치해줘"),
        )

        report = runner.run_case_k_times(case, reliable_delegation_providers, k=2, root=self.tmp_dir)

        self.assertEqual(len(report.attempts), 2)
        self.assertEqual(report.pass_rate, 1.0)
        self.assertEqual(report.pass_at_k, 1.0)
        self.assertEqual(report.pass_pow_k, 1.0)
        for attempt in report.attempts:
            self.assertEqual(attempt.run_status, "success")

    def test_mixed_results_computed_correctly(self) -> None:
        case = EvalCase(
            name="fan-out-mixed",
            task=make_task("eval-mixed", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘"),
        )

        def providers_factory(attempt_index: int) -> dict[str, MockProvider]:
            # 2번째 시도(index=1)만 실패하도록 구성
            if attempt_index == 1:
                return mostly_failing_fan_out_providers(attempt_index)
            return reliable_fan_out_providers(attempt_index)

        report = runner.run_case_k_times(case, providers_factory, k=3, root=self.tmp_dir)

        self.assertEqual(len(report.attempts), 3)
        self.assertAlmostEqual(report.pass_rate, 2 / 3)
        self.assertEqual(report.pass_at_k, 1.0)  # 적어도 1번은 성공
        self.assertEqual(report.pass_pow_k, 0.0)  # 전부 성공은 아님
        self.assertFalse(report.attempts[1].grade.passed)
        self.assertEqual(report.attempts[1].run_status, "error")

    def test_cost_and_latency_per_success_exclude_failed_attempts(self) -> None:
        case = EvalCase(
            name="fan-out-cost-check",
            task=make_task("eval-cost", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘"),
        )

        def providers_factory(attempt_index: int) -> dict[str, MockProvider]:
            if attempt_index == 0:
                return mostly_failing_fan_out_providers(attempt_index)
            return reliable_fan_out_providers(attempt_index)

        report = runner.run_case_k_times(case, providers_factory, k=2, root=self.tmp_dir)

        # 실패한 시도도 metrics.json에 latency/cost 값이 있을 수 있지만, per-success
        # 평균에는 포함되면 안 된다 (실패 시도 1개 + 성공 시도 1개 -> 성공한 값만 평균).
        self.assertFalse(report.attempts[0].grade.passed)
        self.assertTrue(report.attempts[1].grade.passed)
        expected_cost = report.attempts[1].cost_usd
        self.assertEqual(report.cost_per_success, expected_cost)

    def test_rejects_k_below_one(self) -> None:
        case = EvalCase(name="x", task=make_task("t", "설계안을 검토해줘"))

        with self.assertRaises(ValueError):
            runner.run_case_k_times(case, reliable_fan_out_providers, k=0, root=self.tmp_dir)


if __name__ == "__main__":
    unittest.main()
