"""Router: Planner 호출 전 저비용 규칙 기반 사전 분류 훅 + 적합성 게이트 (Step 5).

harness-implementation-plan-ko.md Section 5, Section 12.1, Section 7 Step 5를 구현한다.

이 MVP에는 아직 진짜 LLM Planner가 없다(Planner도 규칙 기반이다). 그래서 지금은
router와 planner의 경계가 다소 느슨해 보일 수 있지만, 구조는 미리 분리해둔다 — 나중에
Planner가 실제 LLM 호출로 바뀌어도 이 저비용 사전 필터는 그대로 재사용 가능해야 하기
때문이다(router는 "명백한 경우 LLM/규칙 엔진 호출 자체를 건너뛰기 위한 게이트").
"""
from __future__ import annotations

from .schemas import FitnessCheck, TaskInput, TeamPattern

# Section 12.1: 단순 사실 확인/한 줄 질문처럼 "비교"나 "역할 분업"의 이득이 없는 작업은
# 하네스화하지 않는다. 애매하면 기본값 passed=True (과소적용보다 과대적용이 안전).
_TRIVIAL_PROMPT_MAX_LEN = 20

# 키워드 검사는 **짧은 프롬프트에만** 적용한다(2026-08-04). 이전에는 길이와 무관하게
# 부분 문자열로 검사해서, 1,948자짜리 아키텍처 설계 프롬프트가 본문 중간의 "누가
# 승인하며" 한 구절 때문에 게이트를 탈락하고 direct_call로 떨어졌다. "누구야"/"맞아?"/
# "몇 개"도 긴 문서형 프롬프트 안에 자연스럽게 등장할 수 있어 같은 함정이 있었다.
# 이 오판은 "애매하면 passed=True" 철학과 반대 방향(과소적용)이라 상한을 둔다.
_TRIVIAL_KEYWORD_MAX_LEN = 60
_TRIVIAL_KEYWORDS = (
    "몇 시", "몇 개", "언제야", "누구야", "누가", "정의가 뭐", "뜻이 뭐", "맞아?", "맞나요",
)

# Section 5의 team_pattern 결정 표를 키워드 매칭으로 구현한 것. 순서가 우선순위다
# (예: "설계 리뷰"는 "설계"보다 먼저 검사해야 sequential_review로 분류된다).
#
# **`hierarchical_delegation`은 이 표에 없다** (2026-07-29, ADR 0009로 강등).
# 네 번 측정해서 한 번도 `direct_call` 대비 우위를 입증하지 못했고, 결함 없는 4차
# 측정에서는 세 조건이 전부 만점인데 체인이 1.5배(3역할)~3.2배(5역할) 비쌌다.
# 품질 차이가 관측되지 않는 경로를 **기본값**으로 두는 것은 적합성 게이트 철학
# ("이득이 없으면 비용을 지불하지 않는다", Section 12.1)과 모순이다.
#
# 패턴은 삭제하지 않았다 — `constraints: ["team_pattern:hierarchical_delegation"]`
# opt-in으로 진입한다(ADR 0006/0007과 같은 방식). 측정이 프롬프트 1종·k=3이라
# 다른 유형에서 값이 날 가능성은 열어둔다.
#
# `task_type`은 유지한다(`research`/`sequential_review`) — rubric 선택과
# `delegation_chain` 구성이 여기 묶여 있어서, 없애면 opt-in으로 들어온 체인이
# 기본 rubric으로 떨어진다.
_TASK_TYPE_RULES: list[tuple[tuple[str, ...], str, TeamPattern]] = [
    (("설계 리뷰", "구현 리뷰", "단계적 검토", "순차 검토"), "sequential_review", "fan_out_judge"),
    (("리서치", "조사", "research"), "research", "fan_out_judge"),
    (("설계", "아키텍처", "architecture"), "architecture design", "fan_out_judge"),
    (("작성해줘", "써줘", "콘텐츠", "content"), "content generation", "fan_out_judge"),
]


def check_fitness(task: TaskInput) -> FitnessCheck:
    """하네스화(패턴 분기) 자체가 필요한 작업인지 저비용으로 사전 판정한다 (Section 12.1)."""
    prompt = task.prompt.strip()

    is_trivial = len(prompt) <= _TRIVIAL_PROMPT_MAX_LEN or (
        len(prompt) <= _TRIVIAL_KEYWORD_MAX_LEN
        and any(keyword in prompt for keyword in _TRIVIAL_KEYWORDS)
    )

    if is_trivial:
        return FitnessCheck(
            passed=False,
            reason="단순 사실 확인/짧은 질문으로 판단되어 패턴 분기 없이 direct_call로 처리",
        )

    return FitnessCheck(
        passed=True,
        reason="여러 관점 비교 또는 역할 분업의 이득이 있는 작업으로 판단 (기본값)",
    )


def classify_team_pattern(task: TaskInput) -> tuple[str, TeamPattern] | None:
    """명백한 키워드가 있으면 (task_type, team_pattern)을 즉시 정해서 반환한다.

    애매하면 None을 반환해 Planner가 자체 기본 규칙(fan_out_judge 기본값)을 적용하도록
    한다 (Section 5: "분류 애매 -> 기본값 fan_out_judge + ask_user로 확인").
    """
    prompt = task.prompt
    for keywords, task_type, team_pattern in _TASK_TYPE_RULES:
        if any(keyword in prompt for keyword in keywords):
            return task_type, team_pattern
    return None
