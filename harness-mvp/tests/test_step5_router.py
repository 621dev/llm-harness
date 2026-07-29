"""Step 5 테스트: router.py(적합성 게이트 + 사전 분류) + model_runner.direct_call.

harness-implementation-plan-ko.md Section 12.1, Section 7 Step 5를 검증한다.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import model_runner, router  # noqa: E402
from harness.schemas import ProviderConfig, TaskInput  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def make_task(prompt: str) -> TaskInput:
    return TaskInput(task_id="t-router", prompt=prompt)


class FitnessGateTest(unittest.TestCase):
    def test_short_trivial_question_fails_gate(self) -> None:
        result = router.check_fitness(make_task("지금 몇 시야?"))
        self.assertFalse(result.passed)

    def test_descriptive_task_passes_gate(self) -> None:
        result = router.check_fitness(
            make_task("이 설계안을 검토해줘: 마이크로서비스로 분리할지 모놀리식으로 유지할지 비교해줘")
        )
        self.assertTrue(result.passed)

    def test_ambiguous_length_defaults_to_passed(self) -> None:
        # 애매한 경우 과소적용보다 과대적용이 안전하다는 원칙(Section 12.1) 확인:
        # 트리비얼 키워드가 없고 길이 기준을 넘으면 기본값은 통과.
        result = router.check_fitness(make_task("이 문제를 조금 더 깊이 있게 살펴봐 주면 좋겠어요"))
        self.assertTrue(result.passed)


class TeamPatternClassificationTest(unittest.TestCase):
    def test_research_keyword_routes_to_fan_out_not_chain(self) -> None:
        """ADR 0009(2026-07-29): 리서치 키워드가 더 이상 체인으로 자동 진입하지 않는다.

        네 번 측정해서 체인이 direct_call 대비 우위를 입증하지 못했고, 결함 없는 4차
        측정에서 세 조건이 전부 만점인데 체인이 1.5~3.2배 비쌌다. 품질 차이가 없는
        경로를 **기본값**으로 두는 것은 적합성 게이트 철학과 모순이다.
        `task_type`은 유지한다 — rubric/체인 구성이 여기 묶여 있다.
        """
        result = router.classify_team_pattern(make_task("경쟁사 가격 정책을 리서치해줘"))
        self.assertEqual(result, ("research", "fan_out_judge"))

    def test_no_keyword_routes_to_hierarchical_delegation(self) -> None:
        """강등 회귀 방지: 어떤 키워드로도 체인에 자동 진입해선 안 된다(opt-in 전용)."""
        for keywords, _task_type, pattern in router._TASK_TYPE_RULES:
            with self.subTest(keywords=keywords[0]):
                self.assertNotEqual(pattern, "hierarchical_delegation")

    def test_architecture_keyword_classified(self) -> None:
        result = router.classify_team_pattern(make_task("이 설계안을 검토해줘"))
        self.assertEqual(result, ("architecture design", "fan_out_judge"))

    def test_ambiguous_prompt_returns_none(self) -> None:
        result = router.classify_team_pattern(make_task("아무 의견이나 자유롭게 줘"))
        self.assertIsNone(result)


class DirectCallTest(unittest.TestCase):
    def test_direct_call_returns_successful_candidate(self) -> None:
        provider = MockProvider(ProviderConfig(provider_id="direct-mock", model_id="direct-mock"))

        candidate = model_runner.direct_call("지금 몇 시야?", provider)

        self.assertEqual(candidate.status, "success")
        self.assertIn("지금 몇 시야?", candidate.content)

    def test_direct_call_recovers_after_one_retry(self) -> None:
        provider = MockProvider(
            ProviderConfig(provider_id="direct-mock", model_id="direct-mock"), fail_times=1
        )

        candidate = model_runner.direct_call("지금 몇 시야?", provider)

        self.assertEqual(candidate.status, "success")
        self.assertEqual(provider.call_count, 2)

    def test_direct_call_records_error_after_exhausting_retry(self) -> None:
        provider = MockProvider(
            ProviderConfig(provider_id="direct-mock", model_id="direct-mock"), fail_times=2
        )

        candidate = model_runner.direct_call("지금 몇 시야?", provider)

        self.assertEqual(candidate.status, "error")


if __name__ == "__main__":
    unittest.main()
