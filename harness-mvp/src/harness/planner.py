"""Planner: task -> Plan (Step 4).

harness-implementation-plan-ko.md Section 5, Section 7 Step 4를 구현한다.

이 MVP의 Planner는 진짜 LLM을 호출하지 않는 규칙 기반 산출기다. `router.py`가 먼저
명백한 키워드로 (task_type, team_pattern)을 분류하고(저비용 사전 필터), Planner는 그
결과를 받아 risk_level/rubric/num_candidates/delegation_chain까지 채워서 완성된
Plan을 만든다. router가 애매하다고 판단하면(None) Planner는 자체 기본값
(task_type="unclassified", team_pattern="fan_out_judge")을 적용한다
(Section 5: "분류 애매 -> 기본값 fan_out_judge").
"""
from __future__ import annotations

from . import router
from .schemas import DelegationStep, Plan, RiskLevel, TaskInput, TeamPattern

_DEFAULT_TASK_TYPE = "unclassified"
_DEFAULT_TEAM_PATTERN: TeamPattern = "fan_out_judge"
_DEFAULT_NUM_CANDIDATES = 3

_DEFAULT_RUBRICS: dict[str, list[str]] = {
    "architecture design": ["구조 명확성", "확장성", "MVP 범위 적절성"],
    "content generation": ["명확성", "설득력", "일관성"],
    "research": ["출처 신뢰성", "핵심 정보 커버리지"],
    "sequential_review": ["이전 단계 반영 여부", "구체성"],
}
_DEFAULT_RUBRIC = ["명확성", "정확성"]

# hierarchical_delegation의 기본 delegation_chain. provider_id는 "{role}-mock" 규칙을
# 따른다 — 실제 provider 선택(Phase 3: api_provider/cli_subscription_provider)이 생기기
# 전까지, 호출부(cli.py/테스트)가 이 규칙에 맞는 이름으로 mock provider를 등록해두면 된다.
_DEFAULT_DELEGATION_ROLES: dict[str, list[str]] = {
    "research": ["research", "design_review"],
    "sequential_review": ["design_review", "implementation_review"],
}

_HIGH_RISK_KEYWORDS = ("배포", "삭제", "프로덕션", "production", "결제", "개인정보", "금융")
_MEDIUM_RISK_KEYWORDS = ("설계", "아키텍처", "정책", "계약")
_RISK_OVERRIDE_PREFIX = "risk_level:"


def create_plan(task: TaskInput) -> Plan:
    """task를 받아 team_pattern까지 확정된 Plan을 만든다."""
    hint = router.classify_team_pattern(task)
    task_type, team_pattern = hint if hint is not None else (_DEFAULT_TASK_TYPE, _DEFAULT_TEAM_PATTERN)

    risk_level = _infer_risk_level(task)
    rubric = _DEFAULT_RUBRICS.get(task_type, list(_DEFAULT_RUBRIC))

    if team_pattern == "fan_out_judge":
        return Plan(
            task_id=task.task_id,
            task_type=task_type,
            risk_level=risk_level,
            rubric=rubric,
            team_pattern=team_pattern,
            num_candidates=_DEFAULT_NUM_CANDIDATES,
        )

    roles = _DEFAULT_DELEGATION_ROLES.get(task_type, ["research", "design_review"])
    delegation_chain = [DelegationStep(role=role, provider_id=f"{role}-mock") for role in roles]
    return Plan(
        task_id=task.task_id,
        task_type=task_type,
        risk_level=risk_level,
        rubric=rubric,
        team_pattern=team_pattern,
        delegation_chain=delegation_chain,
    )


def _infer_risk_level(task: TaskInput) -> RiskLevel:
    """risk_level 판정 규칙 (Section 12.2 승인 체크포인트가 참조하는 값).

    테스트/운영에서 결정적으로 제어할 수 있도록 `constraints`에 "risk_level:high" 같은
    명시적 override를 우선 적용하고, 없으면 프롬프트의 키워드로 추정한다. 실제 Planner가
    LLM으로 바뀌면 이 휴리스틱은 더 정교한 위험도 분류로 대체될 예정이다.
    """
    for constraint in task.constraints:
        if constraint.startswith(_RISK_OVERRIDE_PREFIX):
            level = constraint[len(_RISK_OVERRIDE_PREFIX):]
            if level in ("low", "medium", "high"):
                return level  # type: ignore[return-value]

    prompt = task.prompt
    if any(keyword in prompt for keyword in _HIGH_RISK_KEYWORDS):
        return "high"
    if any(keyword in prompt for keyword in _MEDIUM_RISK_KEYWORDS):
        return "medium"
    return "medium"
