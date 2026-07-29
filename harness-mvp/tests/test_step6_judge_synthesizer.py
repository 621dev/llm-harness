"""Step 6 테스트: judge.py + synthesizer.py (stdlib unittest).

harness-implementation-plan-ko.md Section 6, Section 7 Step 6,
`docs/adr/0004-judge-real-llm-upgrade.md`를 검증한다. judge는 이제
judge_provider로 실제 LLM 호출 1회를 하므로(모킹), 여기서는 실제 LLM 대신
MockProvider(profile="judge")를 판정 대역으로 써서 호출/파싱/레이블 매핑
로직을 검증한다 — 편향 회피 자체(길이로 판단하지 않는지)는 실제 LLM으로만
검증 가능하므로 자동 테스트 범위 밖이다(진행상황 문서의 수동 e2e 기록 참고).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import judge, synthesizer  # noqa: E402
from harness.schemas import Candidate, ProviderConfig  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def make_judge() -> MockProvider:
    return MockProvider(ProviderConfig(provider_id="judge", model_id="judge-mock"), profile="judge")


class _StaticProvider(Provider):
    """judge_candidate.status == "error" 경로(호출 자체가 끝내 실패)를 검증하기 위한
    대역 — MockProvider의 fail_times는 재시도 후 결국 성공하므로 여기엔 안 맞는다."""

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        raise ProviderError("judge provider 영구 실패 (테스트용)")


class JudgeTest(unittest.TestCase):
    def test_raises_when_no_successful_candidates(self) -> None:
        candidates = [Candidate(model_id="model-a", content="", status="error")]

        with self.assertRaises(ValueError):
            judge.evaluate(candidates, rubric=["명확성"], judge_provider=make_judge())

    def test_longer_candidate_wins_via_mock_judge(self) -> None:
        # MockProvider(profile="judge")는 길이 기반 결정적 판정을 낸다(mock 자체의
        # 결정성 확보용). 여기서는 judge.evaluate가 judge_provider 응답의 레이블을
        # 실제 model_id로 정확히 되돌려 매핑하는지(블라인드 셔플 이후에도)를 검증한다.
        long_answer = Candidate(model_id="model-a", content="충분히 길고 근거가 있는 답변 " * 5, status="success")
        short_answer = Candidate(model_id="model-b", content="짧음", status="success")

        judging = judge.evaluate([long_answer, short_answer], rubric=["명확성"], judge_provider=make_judge())

        self.assertEqual(judging.winner, "model-a")
        score_by_candidate = {s.candidate: s.score for s in judging.scores}
        self.assertGreater(score_by_candidate["model-a"], score_by_candidate["model-b"])
        # 부실 판정(<=20자)엔 flaws가 채워진다 — reject-first 응답 형태 확인.
        weaknesses_by_candidate = {s.candidate: s.weaknesses for s in judging.scores}
        self.assertIn("내용이 부실함", weaknesses_by_candidate["model-b"])

    def test_close_scores_recommend_merge(self) -> None:
        a = Candidate(model_id="model-a", content="비슷한 길이의 답변 A " * 5, status="success")
        b = Candidate(model_id="model-b", content="비슷한 길이의 답변 B " * 5, status="success")

        judging = judge.evaluate([a, b], rubric=["명확성"], judge_provider=make_judge())

        self.assertEqual(judging.recommended_strategy, "merge_top_candidates")

    def test_clear_winner_recommends_adopt(self) -> None:
        strong = Candidate(model_id="model-a", content="충분히 길고 근거가 있는 답변 " * 10, status="success")
        weak = Candidate(model_id="model-b", content="짧음", status="success")

        judging = judge.evaluate([strong, weak], rubric=["명확성"], judge_provider=make_judge())

        self.assertEqual(judging.recommended_strategy, "adopt_winner")

    def test_judge_call_exhausts_retry_raises_judge_error(self) -> None:
        candidates = [Candidate(model_id="model-a", content="답변", status="success")]
        failing_judge = _StaticProvider(ProviderConfig(provider_id="judge", model_id="judge-mock"))

        with self.assertRaises(judge.JudgeError):
            judge.evaluate(candidates, rubric=["명확성"], judge_provider=failing_judge)

    def test_malformed_json_response_raises_judge_error(self) -> None:
        candidates = [Candidate(model_id="model-a", content="답변", status="success")]

        class _GarbageProvider(Provider):
            def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
                return Candidate(model_id="judge-mock", content="이건 JSON이 아니다", status="success")

        garbage = _GarbageProvider(ProviderConfig(provider_id="judge", model_id="judge-mock"))

        with self.assertRaises(judge.JudgeError):
            judge.evaluate(candidates, rubric=["명확성"], judge_provider=garbage)

    def test_records_judge_call_latency_and_cost(self) -> None:
        candidates = [Candidate(model_id="model-a", content="충분히 긴 답변 " * 5, status="success")]

        judging = judge.evaluate(candidates, rubric=["명확성"], judge_provider=make_judge())

        self.assertIsNotNone(judging.latency_ms)  # MockProvider judge 응답은 latency_ms=5 고정


class CheckPassTest(unittest.TestCase):
    """iterative_refinement용 합격 판정(judge.check_pass) — evaluate()와 다른
    프롬프트/응답 형식이라 MockProvider(profile="judge")가 아닌 전용 스텁으로 검증한다."""

    class _VerdictProvider(Provider):
        def __init__(self, config: ProviderConfig, response: str) -> None:
            super().__init__(config)
            self.response = response

        def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
            return Candidate(model_id=self.model_id, content=self.response, latency_ms=5, status="success")

    def make_evaluator(self, response: str) -> Provider:
        return self._VerdictProvider(ProviderConfig(provider_id="eval", model_id="eval-mock"), response)

    def test_passed_true_parses(self) -> None:
        evaluator = self.make_evaluator('{"passed": true, "feedback": ""}')

        verdict = judge.check_pass("충분한 답변", rubric=["명확성"], judge_provider=evaluator)

        self.assertTrue(verdict.passed)
        self.assertEqual(verdict.feedback, "")
        self.assertIsNotNone(verdict.latency_ms)

    def test_passed_false_returns_feedback(self) -> None:
        evaluator = self.make_evaluator(
            '{"unmet_items": ["명확성"], "passed": false, "feedback": "근거를 추가하라"}'
        )

        verdict = judge.check_pass("부실한 답변", rubric=["명확성"], judge_provider=evaluator)

        self.assertFalse(verdict.passed)
        self.assertEqual(verdict.feedback, "근거를 추가하라")
        self.assertEqual(verdict.unmet_rubric_items, ["명확성"])

    def test_fail_without_named_rubric_item_is_flagged(self) -> None:
        """2차 측정에서 관측된 실패 유형(2026-07-29): evaluator가 rubric에 없는 요건을
        발명해 불합격시켰다("시각 자료가 없다" — rubric은 출처 신뢰성/커버리지 2개뿐).
        같은 응답에서 "매우 훌륭하게 작성되었습니다"라고 칭찬하기까지 했다.

        **판정을 뒤집지는 않는다** — 품질 판단 주체는 evaluator라는 게 ADR 0004의
        결정이고, 하네스가 pass로 바꾸면 판정자를 덮어쓰는 셈이다. 대신 눈에 보이게
        표시해서 측정 결과와 다음 라운드 피드백에서 드러나게 한다.
        """
        evaluator = self.make_evaluator('{"unmet_items": [], "passed": false, "feedback": "그림이 없다"}')

        verdict = judge.check_pass("답변", rubric=["출처 신뢰성"], judge_provider=evaluator)

        self.assertFalse(verdict.passed)  # 판정은 그대로
        self.assertEqual(verdict.unmet_rubric_items, [])
        self.assertIn("판정 신뢰도", verdict.feedback)  # 그러나 표시가 붙는다
        self.assertIn("그림이 없다", verdict.feedback)  # 원래 사유는 보존

    def test_pass_without_unmet_items_is_not_flagged(self) -> None:
        """통과에는 미충족 항목이 없는 게 정상이므로 표시가 붙어선 안 된다."""
        evaluator = self.make_evaluator('{"unmet_items": [], "passed": true, "feedback": ""}')

        verdict = judge.check_pass("답변", rubric=["명확성"], judge_provider=evaluator)

        self.assertEqual(verdict.feedback, "")

    def test_prompt_scopes_judgement_to_rubric_only(self) -> None:
        """rubric 밖 개선 아이디어를 판정 근거로 쓰지 말라는 지시가 있어야 한다."""
        captured = self.capture_prompt(rubric=["출처 신뢰성"])

        self.assertIn("rubric 항목에 해당하지 않는 지적은 쓰지 마라", captured)
        self.assertIn("unmet_items", captured)

    def test_original_request_is_given_to_the_evaluator(self) -> None:
        """2차 측정의 direct #3 불합격 원인(2026-07-29): 프롬프트가 "검토해줘"라서 답변에
        검토 섹션이 있었는데, evaluator가 그걸 "심사자인 내 역할 침범"으로 읽고 삭제를
        지시했다. **판정자가 원본 요청을 못 봤기 때문**에 생긴 오판이다."""
        captured = self.capture_prompt(rubric=["명확성"], request="문서를 쓰고 검토해줘")

        self.assertIn("문서를 쓰고 검토해줘", captured)
        self.assertIn("결함이 아니다", captured)

    def test_request_block_is_omitted_when_absent(self) -> None:
        """요청을 안 주면 그 절이 아예 없어야 한다 — 빈 절은 모델에 혼선만 준다."""
        captured = self.capture_prompt(rubric=["명확성"])

        self.assertNotIn("원본 요청", captured)

    def capture_prompt(self, *, rubric: list[str], request: str | None = None) -> str:
        captured: list[str] = []

        class _Capture(Provider):
            def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
                captured.append(prompt)
                return Candidate(
                    model_id=self.model_id,
                    content='{"unmet_items": [], "passed": true, "feedback": ""}',
                    status="success",
                )

        judge.check_pass(
            "평가 대상",
            rubric,
            _Capture(ProviderConfig(provider_id="eval", model_id="eval-mock")),
            request=request,
        )
        return captured[0]

    def test_prompt_contains_rubric_and_content(self) -> None:
        captured: list[str] = []

        class _Capture(Provider):
            def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
                captured.append(prompt)
                return Candidate(model_id=self.model_id, content='{"passed": true, "feedback": ""}', status="success")

        evaluator = _Capture(ProviderConfig(provider_id="eval", model_id="eval-mock"))
        judge.check_pass("평가 대상 콘텐츠", rubric=["출처 신뢰성"], judge_provider=evaluator)

        self.assertIn("출처 신뢰성", captured[0])
        self.assertIn("평가 대상 콘텐츠", captured[0])

    def test_non_json_response_raises_judge_error(self) -> None:
        evaluator = self.make_evaluator("이건 JSON이 아니다")

        with self.assertRaises(judge.JudgeError):
            judge.check_pass("답변", rubric=["명확성"], judge_provider=evaluator)

    def test_missing_passed_bool_raises_judge_error(self) -> None:
        evaluator = self.make_evaluator('{"passed": "yes", "feedback": ""}')

        with self.assertRaises(judge.JudgeError):
            judge.check_pass("답변", rubric=["명확성"], judge_provider=evaluator)

    def test_evaluator_permanent_failure_raises_judge_error(self) -> None:
        failing = _StaticProvider(ProviderConfig(provider_id="eval", model_id="eval-mock"))

        with self.assertRaises(judge.JudgeError):
            judge.check_pass("답변", rubric=["명확성"], judge_provider=failing)


class SynthesizerTest(unittest.TestCase):
    def test_adopt_winner_returns_winner_content_only(self) -> None:
        candidates = [
            Candidate(model_id="model-a", content="A의 충분히 길고 근거가 있는 답변 " * 5, status="success"),
            Candidate(model_id="model-b", content="B", status="success"),
        ]
        judging = judge.evaluate(candidates, rubric=["명확성"], judge_provider=make_judge())

        result = synthesizer.synthesize(candidates, judging)

        self.assertEqual(result, candidates[0].content)

    def test_merge_strategy_combines_top_two_contents(self) -> None:
        candidates = [
            Candidate(model_id="model-a", content="비슷한 길이의 답변 A " * 5, status="success"),
            Candidate(model_id="model-b", content="비슷한 길이의 답변 B " * 5, status="success"),
        ]
        judging = judge.evaluate(candidates, rubric=["명확성"], judge_provider=make_judge())
        self.assertEqual(judging.recommended_strategy, "merge_top_candidates")  # 전제 확인

        result = synthesizer.synthesize(candidates, judging)

        self.assertIn(candidates[0].content, result)
        self.assertIn(candidates[1].content, result)


if __name__ == "__main__":
    unittest.main()
