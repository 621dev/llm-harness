"""Step 4 테스트: planner.py (stdlib unittest).

harness-implementation-plan-ko.md Section 5(team_pattern 결정 표), Section 7 Step 4를
검증한다.
"""
from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import planner  # noqa: E402
from harness.schemas import TaskInput  # noqa: E402


def make_task(task_id: str, prompt: str, constraints: list[str] | None = None) -> TaskInput:
    return TaskInput(task_id=task_id, prompt=prompt, constraints=constraints or [])


class MeasurementRubricConsistencyTest(unittest.TestCase):
    """측정 스크립트의 RUBRIC이 planner의 research rubric과 같은지 (2026-07-29).

    측정은 `direct` 조건에도 체인과 **같은 rubric**을 적용해 판정 기준을 통일한다.
    두 곳이 어긋나면 조건마다 다른 기준으로 채점하면서 "패턴 차이"라고 읽게 된다 —
    2026-07-29에 `출처 신뢰성`을 제거할 때 한쪽만 고치면 바로 그렇게 됐을 자리다.

    **import하지 않고 소스를 파싱한다**: `measure_pattern_value`는 모듈 레벨에서
    `sys.stdout`을 `TextIOWrapper`로 갈아치우므로(콘솔 인코딩 대응), import하면
    pytest의 출력 캡처가 깨진다.
    """

    def test_measurement_rubric_matches_planner_research_rubric(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "measure_pattern_value.py"
        tree = ast.parse(script.read_text(encoding="utf-8"))
        rubric = next(
            ast.literal_eval(node.value)
            for node in tree.body
            if isinstance(node, ast.Assign)
            and any(getattr(t, "id", None) == "RUBRIC" for t in node.targets)
        )

        self.assertEqual(rubric, planner._DEFAULT_RUBRICS["research"])


class ResearchRubricIsAchievableTest(unittest.TestCase):
    """research rubric에 도구가 필요한 항목이 들어오지 못하게 막는다 (2026-07-29).

    `출처 신뢰성`이 rubric에 있었는데 **어떤 백엔드로도 웹 접근이 없어 달성 불가**였다
    (Gemini는 `tools` 미전송, claude 단발은 `-p`에서 검색 닫힘 —
    `usage.server_tool_use.web_search_requests == 0`으로 실측, `agentic_task`는 명시적 차단).
    3차 측정에서 9건 중 6건이 이 항목 하나로 떨어져 패턴 비교가 무의미해졌다.

    웹 검색 도구를 실제로 붙이면 이 테스트를 지우고 항목을 되살리면 된다 — 그때까지는
    "판정할 수 없는 것을 판정 기준에 넣지 않는다"를 여기서 지킨다.
    """

    # 도구(웹 검색/파일 접근) 없이는 판정할 수 없는 항목들.
    _NEEDS_TOOLS = ("출처", "인용", "레퍼런스", "링크", "URL")

    def test_research_rubric_has_no_tool_dependent_item(self) -> None:
        offenders = [
            item
            for item in planner._DEFAULT_RUBRICS["research"]
            if any(word in item for word in self._NEEDS_TOOLS)
        ]

        self.assertEqual(
            offenders,
            [],
            f"도구가 필요한 rubric 항목이다: {offenders}. 웹 검색을 실제로 붙였다면 "
            f"이 테스트를 지우고 항목을 되살릴 것 — 안 붙였으면 어떤 백엔드로도 통과할 수 없다.",
        )


class DelegationRolesOverrideTest(unittest.TestCase):
    """`delegation_roles:` override — 태스크가 자기 부서 구성을 정한다 (2026-07-29).

    "실제 회사 부서를 모방한 다중 에이전트" 검토에서, 체인 역할이
    `_DEFAULT_DELEGATION_ROLES[task_type]`으로 고정돼 도메인마다 다른 조직도를 쓸 수
    없다는 갭을 확인하고 추가했다. 분업 병렬·분기는 **의도적으로 범위 밖**이다
    (DAG 구조가 필요하고, 3단계 체인이 아직 우위를 입증하지 못한 상태라 근거가 없다).
    """

    def test_override_replaces_the_default_chain(self) -> None:
        task = make_task(
            "dept-1",
            "신규 입사자용 절차 안내서를 만들어줘",
            [
                "team_pattern:hierarchical_delegation",
                "delegation_roles:research,drafting,compliance_review,editing,content_finalization",
            ],
        )

        plan = planner.create_plan(task)

        self.assertEqual(
            [step.role for step in plan.delegation_chain],
            ["research", "drafting", "compliance_review", "editing", "content_finalization"],
        )
        # provider_id 규칙은 그대로여야 한다 — cli가 이 이름으로 provider를 등록한다
        self.assertEqual(plan.delegation_chain[1].provider_id, "drafting-mock")

    def test_unknown_role_fails_with_a_clear_message(self) -> None:
        """오타를 런타임 KeyError가 아니라 여기서 잡는다 — 어느 역할이 문제인지 말해준다."""
        task = make_task(
            "dept-2",
            "안내서를 만들어줘",
            ["team_pattern:hierarchical_delegation", "delegation_roles:research,법무팀"],
        )

        with self.assertRaises(ValueError) as ctx:
            planner.create_plan(task)

        self.assertIn("법무팀", str(ctx.exception))
        self.assertIn("cli._DELEGATION_ROLES", str(ctx.exception))  # 어디를 고쳐야 하는지

    def test_empty_override_falls_back_to_default(self) -> None:
        task = make_task(
            "dept-3",
            "경쟁사 가격 정책을 리서치해줘",
            ["team_pattern:hierarchical_delegation", "delegation_roles:", "delegation_roles:  ,  "],
        )

        plan = planner.create_plan(task)

        self.assertEqual(
            [step.role for step in plan.delegation_chain],
            ["research", "design_review", "content_finalization"],
        )

    def test_role_vocabulary_matches_provider_registration(self) -> None:
        """**두 목록이 어긋나면 그 역할을 쓰는 순간 KeyError가 난다.**

        planner는 override를 검증하고 cli는 `{role}-mock` provider를 등록한다 — 한쪽에만
        역할을 추가하면 계획은 통과하는데 `run_chain`이
        `providers["{role}-mock"]`에서 죽는다. content_finalization을 추가할 때
        실제로 이 유형으로 측정 스크립트가 깨진 전례가 있다(2026-07-28).
        """
        from harness import cli

        self.assertEqual(planner.KNOWN_DELEGATION_ROLES, frozenset(cli._DELEGATION_ROLES))

    def test_default_chains_only_use_known_roles(self) -> None:
        """기본 체인이 어휘 밖 역할을 쓰고 있으면 override 검증이 무의미해진다."""
        for task_type, roles in planner._DEFAULT_DELEGATION_ROLES.items():
            with self.subTest(task_type=task_type):
                self.assertEqual(set(roles) - planner.KNOWN_DELEGATION_ROLES, set())


class ChainIsOptInOnlyTest(unittest.TestCase):
    """ADR 0009 회귀 방지: `hierarchical_delegation`은 키워드로 자동 진입하지 않는다.

    네 번 측정해서 체인이 `direct_call` 대비 우위를 입증하지 못했고, 결함 없는 4차
    측정에서 세 조건이 전부 만점인데 체인이 1.5배(3역할)~3.2배(5역할) 비쌌다.
    품질 차이가 관측되지 않는 경로를 **기본값**으로 두는 것은 적합성 게이트 철학
    ("이득이 없으면 비용을 지불하지 않는다")과 모순이다.

    되돌릴 근거가 생기면 새 ADR로 남기고 이 테스트를 지운다.
    """

    # 강등 전에 체인으로 자동 라우팅되던 키워드들.
    _FORMER_CHAIN_PROMPTS = (
        "경쟁사 가격 정책을 리서치해줘",
        "NCP 스토리지 요금을 조사해줘",
        "research the pricing options for us",
        "설계 리뷰 결과를 반영해서 순차 검토를 진행해줘",
        "구현 리뷰를 단계적 검토로 진행해줘",
    )

    def test_former_chain_keywords_no_longer_auto_route_to_chain(self) -> None:
        for prompt in self._FORMER_CHAIN_PROMPTS:
            with self.subTest(prompt=prompt):
                plan = planner.create_plan(make_task("t", prompt))
                self.assertNotEqual(plan.team_pattern, "hierarchical_delegation")

    def test_opt_in_still_works_for_the_same_prompts(self) -> None:
        """강등이 "진입 불가"가 아니라 "명시적 진입"임을 고정한다 — 패턴은 살아 있다."""
        for prompt in self._FORMER_CHAIN_PROMPTS:
            with self.subTest(prompt=prompt):
                plan = planner.create_plan(
                    make_task("t", prompt, constraints=["team_pattern:hierarchical_delegation"])
                )
                self.assertEqual(plan.team_pattern, "hierarchical_delegation")
                self.assertTrue(plan.delegation_chain)  # 체인 구성도 채워져야 한다


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

    def test_research_with_opt_in_uses_default_chain(self) -> None:
        """ADR 0009: 체인은 opt-in 전용이 됐다 — 키워드만으로는 fan_out_judge로 간다."""
        task = make_task("t-2", "경쟁사 가격 정책을 리서치해줘", constraints=["team_pattern:hierarchical_delegation"])

        plan = planner.create_plan(task)

        self.assertEqual(plan.task_type, "research")
        self.assertEqual(plan.team_pattern, "hierarchical_delegation")
        self.assertIsNone(plan.num_candidates)
        self.assertEqual(
            [step.role for step in plan.delegation_chain],
            ["research", "design_review", "content_finalization"],
        )
        self.assertEqual(
            [step.provider_id for step in plan.delegation_chain],
            ["research-mock", "design_review-mock", "content_finalization-mock"],
        )

    def test_sequential_review_uses_review_chain(self) -> None:
        task = make_task(
            "t-3", "설계 리뷰 결과를 반영해서 순차 검토를 진행해줘", constraints=["team_pattern:hierarchical_delegation"]
        )  # ADR 0009: 체인 진입은 opt-in

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

        # ADR 0009 이후 router 분류가 fan_out_judge다 — 알 수 없는 override는 무시되고
        # router 결과가 그대로 남는다는 것이 이 테스트의 요지(패턴 이름이 아니라 무시 동작).
        self.assertEqual(plan.team_pattern, "fan_out_judge")


if __name__ == "__main__":
    unittest.main()
