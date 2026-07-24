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
