"""Step 4 테스트: planner.py (stdlib unittest).

harness-implementation-plan-ko.md Section 5(team_pattern 결정 표), Section 7 Step 4를
검증한다.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import planner  # noqa: E402
from harness.schemas import TaskInput  # noqa: E402


def make_task(task_id: str, prompt: str, constraints: list[str] | None = None) -> TaskInput:
    return TaskInput(task_id=task_id, prompt=prompt, constraints=constraints or [])


class PlannerTest(unittest.TestCase):
    def test_architecture_design_maps_to_fan_out_judge(self) -> None:
        task = make_task("t-1", "이 설계안을 검토해줘: 마이크로서비스로 갈지 모놀리식으로 갈지")

        plan = planner.create_plan(task)

        self.assertEqual(plan.task_id, task.task_id)
        self.assertEqual(plan.task_type, "architecture design")
        self.assertEqual(plan.team_pattern, "fan_out_judge")
        self.assertEqual(plan.num_candidates, 3)
        self.assertEqual(plan.rubric, ["구조 명확성", "확장성", "MVP 범위 적절성"])
        self.assertEqual(plan.delegation_chain, [])

    def test_research_maps_to_hierarchical_delegation_with_default_chain(self) -> None:
        task = make_task("t-2", "경쟁사 가격 정책을 리서치해줘")

        plan = planner.create_plan(task)

        self.assertEqual(plan.task_type, "research")
        self.assertEqual(plan.team_pattern, "hierarchical_delegation")
        self.assertIsNone(plan.num_candidates)
        self.assertEqual([step.role for step in plan.delegation_chain], ["research", "design_review"])
        self.assertEqual([step.provider_id for step in plan.delegation_chain], ["research-mock", "design_review-mock"])

    def test_sequential_review_uses_review_chain(self) -> None:
        task = make_task("t-3", "설계 리뷰 결과를 반영해서 순차 검토를 진행해줘")

        plan = planner.create_plan(task)

        self.assertEqual(plan.task_type, "sequential_review")
        self.assertEqual(plan.team_pattern, "hierarchical_delegation")
        self.assertEqual([step.role for step in plan.delegation_chain], ["design_review", "implementation_review"])

    def test_ambiguous_prompt_defaults_to_fan_out_judge(self) -> None:
        task = make_task("t-4", "이번 프로젝트에 대해 자유롭게 의견을 줘 아무거나 다 좋아")

        plan = planner.create_plan(task)

        self.assertEqual(plan.task_type, "unclassified")
        self.assertEqual(plan.team_pattern, "fan_out_judge")
        self.assertEqual(plan.rubric, ["명확성", "정확성"])

    def test_risk_level_override_via_constraints(self) -> None:
        task = make_task("t-5", "일반적인 문의사항입니다 특별한 위험은 없어요", constraints=["risk_level:high"])

        plan = planner.create_plan(task)

        self.assertEqual(plan.risk_level, "high")

    def test_risk_level_inferred_from_keywords(self) -> None:
        task = make_task("t-6", "프로덕션 결제 시스템에 배포할 변경사항을 검토해줘")

        plan = planner.create_plan(task)

        self.assertEqual(plan.risk_level, "high")

    def test_risk_level_defaults_to_medium(self) -> None:
        task = make_task("t-7", "이번 주 팀 회식 장소 후보를 추천해줘")

        plan = planner.create_plan(task)

        self.assertEqual(plan.risk_level, "medium")

    def test_team_pattern_override_to_iterative_refinement(self) -> None:
        # router가 research -> hierarchical_delegation으로 분류할 프롬프트라도
        # 명시적 override가 우선한다 (키워드 자동 라우팅 없음 — opt-in 전용).
        task = make_task(
            "t-8", "경쟁사 가격 정책을 리서치해줘", constraints=["team_pattern:iterative_refinement"]
        )

        plan = planner.create_plan(task)

        self.assertEqual(plan.team_pattern, "iterative_refinement")
        self.assertIsNone(plan.num_candidates)
        self.assertEqual(plan.delegation_chain, [])
        self.assertEqual(plan.task_type, "research")  # task_type/rubric은 router 분류를 유지

    def test_agentic_task_forces_high_risk_for_approval_gate(self) -> None:
        # 이 패턴만 실제 파일을 만드는 부수 효과가 있어 사람 승인이 필수다(ADR 0007).
        # 프롬프트에 고위험 키워드가 전혀 없어도 high로 올라가야 한다.
        task = make_task(
            "t-10", "학습 자료를 파일로 만들어줘", constraints=["team_pattern:agentic_task"]
        )

        plan = planner.create_plan(task)

        self.assertEqual(plan.team_pattern, "agentic_task")
        self.assertEqual(plan.risk_level, "high")
        self.assertIsNone(plan.num_candidates)
        self.assertEqual(plan.delegation_chain, [])

    def test_explicit_risk_override_beats_agentic_task_default(self) -> None:
        # 테스트/자동화에서 승인 게이트를 우회해 실행 경로만 검증할 수 있어야 한다.
        task = make_task(
            "t-11",
            "학습 자료를 파일로 만들어줘",
            constraints=["team_pattern:agentic_task", "risk_level:medium"],
        )

        plan = planner.create_plan(task)

        self.assertEqual(plan.risk_level, "medium")

    def test_team_pattern_override_with_unknown_value_is_ignored(self) -> None:
        task = make_task(
            "t-9", "경쟁사 가격 정책을 리서치해줘", constraints=["team_pattern:없는패턴"]
        )

        plan = planner.create_plan(task)

        self.assertEqual(plan.team_pattern, "hierarchical_delegation")  # router 분류 그대로


if __name__ == "__main__":
    unittest.main()
