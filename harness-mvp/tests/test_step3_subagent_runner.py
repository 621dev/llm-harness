"""Step 3 테스트: mock subagent 체인 + subagent_runner (stdlib unittest).

harness-implementation-plan-ko.md Section 7 Step 3 체크리스트를 검증한다.
- 2단계 체인(research -> design_review)이 순서대로 실행되고 각 스텝 결과가
  artifacts/chain/step-N-role.md 로 저장되는가
- 컨텍스트 격리: Observation.summary는 짧은 요약이고, 전체 내용은 파일에만 있는가
- 스텝 실패 후 재시도로 복구되는가 (Section 6)
- 재시도까지 실패하면 체인이 그 지점에서 중단되고, 이후 스텝은 아예 실행되지
  않는가 (Section 6: 체인 중단)
- 동일 입력으로 재실행 시 동일 결과가 나오는가 (재현성)

실행: python -m unittest tests/test_step3_subagent_runner.py -v (harness-mvp 디렉토리에서)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import run_store, subagent_runner  # noqa: E402
from harness.schemas import DelegationStep, ProviderConfig  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def make_two_step_chain(*, fail_times: dict[str, int] | None = None):
    fail_times = fail_times or {}
    steps = [
        DelegationStep(role="research", provider_id="gemini-mock"),
        DelegationStep(role="design_review", provider_id="codex-mock"),
    ]
    providers = {
        "gemini-mock": MockProvider(
            ProviderConfig(provider_id="gemini-mock", model_id="gemini-mock"),
            profile="detailed",
            fail_times=fail_times.get("gemini-mock", 0),
        ),
        "codex-mock": MockProvider(
            ProviderConfig(provider_id="codex-mock", model_id="codex-mock"),
            profile="concise",
            fail_times=fail_times.get("codex-mock", 0),
        ),
    }
    return steps, providers


class SubagentRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-subagent-test", root=self.tmp_dir)

    def test_two_step_chain_completes_in_order(self) -> None:
        steps, providers = make_two_step_chain()

        observations, completed = subagent_runner.run_chain(
            steps, providers, "이 프로젝트를 리서치해줘", self.run_dir
        )

        self.assertTrue(completed)
        self.assertEqual(len(observations), 2)
        self.assertEqual([o.status for o in observations], ["success", "success"])
        self.assertEqual(steps[0].status, "success")
        self.assertEqual(steps[1].status, "success")

        step1_path = self.run_dir / "artifacts" / "chain" / "step-1-research.md"
        step2_path = self.run_dir / "artifacts" / "chain" / "step-2-design_review.md"
        self.assertTrue(step1_path.exists())
        self.assertTrue(step2_path.exists())

        # 두 번째 스텝의 입력은 첫 번째 스텝의 "전체" 출력이어야 한다 (컨텍스트 격리는
        # 오케스트레이터에게 보이는 요약에만 적용되고, 체인 내부 전달은 전체 내용으로 함).
        step1_content = step1_path.read_text(encoding="utf-8")
        step2_content = step2_path.read_text(encoding="utf-8")
        self.assertIn("이 프로젝트를 리서치해줘", step1_content)
        self.assertIn("gemini-mock", step2_content)  # step1 출력이 step2 입력에 그대로 반영됨

    def test_summary_is_short_even_when_output_is_large(self) -> None:
        steps, providers = make_two_step_chain()
        large_input = "리서치 " * 200  # 대용량 mock 출력을 유도

        observations, completed = subagent_runner.run_chain(steps, providers, large_input, self.run_dir)

        self.assertTrue(completed)
        step1_file_content = (self.run_dir / steps[0].output_ref).read_text(encoding="utf-8")
        # 파일에는 전체 내용이, Observation.summary에는 짧은 요약만 있어야 한다 (컨텍스트 격리)
        self.assertGreater(len(step1_file_content), len(observations[0].summary))
        self.assertLess(len(observations[0].summary), 200)

    def test_step_recovers_after_one_retry(self) -> None:
        steps, providers = make_two_step_chain(fail_times={"gemini-mock": 1})

        observations, completed = subagent_runner.run_chain(steps, providers, "리서치해줘", self.run_dir)

        self.assertTrue(completed)
        self.assertEqual(observations[0].status, "success")
        self.assertEqual(providers["gemini-mock"].call_count, 2)  # 최초 호출 + 재시도 1회

    def test_step_exhausts_retry_halts_chain_before_next_step(self) -> None:
        # 3단계 체인을 구성해서, 2번째 스텝이 완전히 실패했을 때 3번째 스텝은 아예
        # 실행되지 않는지(체인 중단) 확인한다.
        steps = [
            DelegationStep(role="research", provider_id="gemini-mock"),
            DelegationStep(role="design_review", provider_id="codex-mock"),
            DelegationStep(role="final_review", provider_id="claude-mock"),
        ]
        providers = {
            "gemini-mock": MockProvider(
                ProviderConfig(provider_id="gemini-mock", model_id="gemini-mock"), profile="detailed"
            ),
            "codex-mock": MockProvider(
                ProviderConfig(provider_id="codex-mock", model_id="codex-mock"),
                profile="concise",
                fail_times=2,  # MAX_RETRIES=1 이므로 총 2번 호출 다 실패
            ),
            "claude-mock": MockProvider(
                ProviderConfig(provider_id="claude-mock", model_id="claude-mock"), profile="creative"
            ),
        }

        observations, completed = subagent_runner.run_chain(steps, providers, "리서치해줘", self.run_dir)

        self.assertFalse(completed)
        self.assertEqual(len(observations), 2)  # final_review는 시도조차 안 함
        self.assertEqual(observations[0].status, "success")
        self.assertEqual(observations[1].status, "error")
        self.assertEqual(steps[0].status, "success")
        self.assertEqual(steps[1].status, "error")
        self.assertEqual(steps[2].status, "success")  # 기본값 그대로, 실행된 적 없음
        self.assertIsNone(steps[2].output_ref)  # 3번째 스텝 파일 자체가 생성되지 않음
        self.assertEqual(providers["claude-mock"].call_count, 0)

        step3_path = self.run_dir / "artifacts" / "chain" / "step-3-final_review.md"
        self.assertFalse(step3_path.exists())

    def test_rerun_same_input_is_reproducible(self) -> None:
        steps_a, providers_a = make_two_step_chain()
        observations_a, _ = subagent_runner.run_chain(steps_a, providers_a, "동일 입력", self.run_dir)

        steps_b, providers_b = make_two_step_chain()
        observations_b, _ = subagent_runner.run_chain(steps_b, providers_b, "동일 입력", self.run_dir)

        self.assertEqual([o.summary for o in observations_a], [o.summary for o in observations_b])


if __name__ == "__main__":
    unittest.main()
