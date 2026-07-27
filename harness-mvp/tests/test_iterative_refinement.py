"""iterative_refinement 패턴 통합 테스트 (orchestrator._run_iterative_refinement).

세 번째 팀 패턴 "반복 생성-평가 루프"를 검증한다 — 생성(generator) → 합격
판정(evaluator, judge.check_pass) → 피드백 반영 재생성을 MAX_REFINEMENT_ROUNDS
상한까지 반복하고, 상한 도달/중간 실패 시 마지막 생성물을 partial로 승격한다.

MockProvider(profile="judge")는 evaluate()의 후보 비교 프롬프트 전용이라, 여기서는
라운드별 판정을 스크립트로 지정할 수 있는 전용 스텁(ScriptedEvaluator)과 받은
프롬프트를 기록하는 생성자 스텁(RecordingGenerator)을 쓴다 (실제 LLM 미호출).
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import orchestrator, run_store  # noqa: E402
from harness.schemas import Candidate, ProviderConfig, TaskInput  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402

_PROMPT = "가상의 주제로 소개 콘텐츠 구성안을 작성해줘. 대상 독자와 톤을 정해 구체적으로 서술해줘."


def make_task(task_id: str) -> TaskInput:
    return TaskInput(task_id=task_id, prompt=_PROMPT, constraints=["team_pattern:iterative_refinement"])


class RecordingGenerator(Provider):
    """받은 프롬프트를 순서대로 기록하는 생성자 스텁 — 피드백이 다음 라운드
    프롬프트에 실제로 들어가는지 검증할 수 있다."""

    def __init__(self, config: ProviderConfig, *, fail_times: int = 0) -> None:
        super().__init__(config)
        self.prompts: list[str] = []
        self.call_count = 0
        self.fail_times = fail_times

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise ProviderError(f"{self.provider_id} 실패 주입 ({self.call_count}/{self.fail_times})")
        self.prompts.append(prompt)
        return Candidate(
            model_id=self.model_id,
            content=f"시도 {len(self.prompts)}번째 답변",
            latency_ms=10,
            cost_usd=0.002,
            status="success",
        )


class ScriptedEvaluator(Provider):
    """라운드별 판정을 미리 지정하는 evaluator 스텁.

    verdicts의 각 항목: (passed, feedback) 튜플, 또는 "garbage"(JSON 아님 응답).
    """

    def __init__(self, config: ProviderConfig, verdicts: list) -> None:
        super().__init__(config)
        self.verdicts = verdicts
        self.call_count = 0

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        verdict = self.verdicts[min(self.call_count, len(self.verdicts) - 1)]
        self.call_count += 1
        if verdict == "garbage":
            return Candidate(model_id=self.model_id, content="이건 JSON이 아니다", status="success")
        passed, feedback = verdict
        return Candidate(
            model_id=self.model_id,
            content=json.dumps({"passed": passed, "feedback": feedback}, ensure_ascii=False),
            latency_ms=5,
            cost_usd=0.001,
            status="success",
        )


def make_providers(verdicts: list, *, generator_fail_times: int = 0) -> tuple[dict[str, Provider], RecordingGenerator]:
    generator = RecordingGenerator(
        ProviderConfig(provider_id="gen", model_id="gen-mock", auth_mode="api_key"),
        fail_times=generator_fail_times,
    )
    evaluator = ScriptedEvaluator(ProviderConfig(provider_id="eval", model_id="eval-mock"), verdicts)
    return {"gen": generator, orchestrator.JUDGE_PROVIDER_KEY: evaluator}, generator


class IterativeRefinementIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_first_round_pass_completes_run(self) -> None:
        providers, generator = make_providers([(True, "")])

        observation = orchestrator.run(make_task("refine-pass-1"), providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-refine-pass-1"
        self.assertEqual(observation.status, "success")
        self.assertEqual(len(generator.prompts), 1)
        self.assertEqual(run_store.read_markdown(run_dir, "final.md"), "시도 1번째 답변\n")
        rounds = run_store.read_json(run_dir, "refinement.json")
        self.assertEqual(len(rounds), 1)
        self.assertTrue(rounds[0]["passed"])
        self.assertFalse((run_dir / "judging.json").exists())  # 이 패턴엔 후보 비교 Judge가 없음
        self.assertEqual(run_store.read_json(run_dir, "errors.json"), [])

    def test_feedback_is_injected_into_next_round_prompt(self) -> None:
        providers, generator = make_providers([(False, "대상 독자 정의가 빠졌다"), (True, "")])

        observation = orchestrator.run(make_task("refine-pass-2"), providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-refine-pass-2"
        self.assertEqual(observation.status, "success")
        self.assertEqual(len(generator.prompts), 2)
        # 2라운드 프롬프트에 원본 요청 + 이전 답변 + 피드백이 모두 들어있어야 한다.
        second_prompt = generator.prompts[1]
        self.assertIn(_PROMPT, second_prompt)
        self.assertIn("시도 1번째 답변", second_prompt)
        self.assertIn("대상 독자 정의가 빠졌다", second_prompt)
        self.assertEqual(run_store.read_markdown(run_dir, "final.md"), "시도 2번째 답변\n")
        rounds = run_store.read_json(run_dir, "refinement.json")
        self.assertEqual([r["passed"] for r in rounds], [False, True])

    def test_round_limit_promotes_last_attempt_as_partial(self) -> None:
        providers, generator = make_providers([(False, "f1"), (False, "f2"), (False, "f3")])

        observation = orchestrator.run(make_task("refine-exhaust"), providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-refine-exhaust"
        self.assertEqual(observation.status, "warning")
        self.assertEqual(len(generator.prompts), orchestrator.MAX_REFINEMENT_ROUNDS)
        final_content = run_store.read_markdown(run_dir, "final.md")
        self.assertTrue(final_content.startswith("(partial)"))
        self.assertIn(f"시도 {orchestrator.MAX_REFINEMENT_ROUNDS}번째 답변", final_content)
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any(e["stage"] == "iterative_refinement" for e in errors))
        rounds = run_store.read_json(run_dir, "refinement.json")
        self.assertEqual(len(rounds), orchestrator.MAX_REFINEMENT_ROUNDS)

    def test_cost_and_latency_are_summed_across_rounds(self) -> None:
        providers, _generator = make_providers([(False, "f1"), (False, "f2"), (False, "f3")])

        orchestrator.run(make_task("refine-cost"), providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-refine-cost"
        metrics = run_store.read_json(run_dir, "metrics.json")
        rounds = orchestrator.MAX_REFINEMENT_ROUNDS
        # 라운드마다 generator(0.002/10ms) + evaluator(0.001/5ms) 합산 (Cost Blindness 방지)
        self.assertAlmostEqual(metrics["estimated_cost_usd"], rounds * 0.003, places=6)
        self.assertEqual(metrics["latency_ms"], rounds * 15)
        self.assertEqual(metrics["completed_candidates_or_steps"], rounds)

    def test_generator_permanent_failure_on_first_round_ends_without_output(self) -> None:
        providers, _generator = make_providers([(True, "")], generator_fail_times=10)

        observation = orchestrator.run(make_task("refine-gen-fail"), providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-refine-gen-fail"
        self.assertEqual(observation.status, "error")
        self.assertFalse((run_dir / "final.md").exists())
        self.assertEqual(run_store.read_json(run_dir, "refinement.json"), [])
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any("refinement round 1" in e["stage"] for e in errors))

    def test_evaluator_failure_promotes_generated_content_as_partial(self) -> None:
        providers, _generator = make_providers(["garbage"])

        observation = orchestrator.run(make_task("refine-eval-fail"), providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-refine-eval-fail"
        self.assertEqual(observation.status, "warning")
        final_content = run_store.read_markdown(run_dir, "final.md")
        self.assertTrue(final_content.startswith("(partial)"))
        self.assertIn("시도 1번째 답변", final_content)
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any("evaluator" in e["stage"] for e in errors))
        # 판정이 완료된 라운드가 없으므로 refinement.json은 비어 있다.
        self.assertEqual(run_store.read_json(run_dir, "refinement.json"), [])

    def test_missing_judge_provider_raises(self) -> None:
        generator = RecordingGenerator(ProviderConfig(provider_id="gen", model_id="gen-mock"))

        with self.assertRaises(ValueError):
            orchestrator.run(make_task("refine-no-judge"), {"gen": generator}, root=self.tmp_dir)


if __name__ == "__main__":
    unittest.main()
