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
    # `research`에서 "출처 신뢰성"을 뺐다 (2026-07-29). **어떤 백엔드로도 달성할 수
    # 없는 항목**이었다 — 세 경로 전부 웹 접근이 없다:
    #   - Gemini API: 요청에 `tools`를 안 보낸다(검색 그라운딩 미사용)
    #   - claude 단발 호출: `-p` 모드에서 검색이 닫혀 있다. 실측으로 확인했고
    #     (`scripts/verify_claude_web_access.py`) 근거는 모델 자기보고가 아니라
    #     API가 세는 `usage.server_tool_use.web_search_requests == 0`이다
    #   - `agentic_task`: `--disallowedTools`로 명시적 차단(ADR 0007)
    #
    # 3차 측정(2026-07-29)에서 **9건 중 6건이 이 항목 하나로 불합격**했고, 세 조건
    # (direct/chain/departments)이 전부 같은 이유였다 — 패턴 차이를 재려는 측정이
    # "달성 불가 항목" 하나에 지배됐다. 운영 도메인도 같은 rubric을 쓰므로 실사용에서도
    # 계속 불합격했을 것이다.
    #
    # 대체는 새 어휘를 만들지 않고 `sequential_review`의 "구체성"을 재사용한다 —
    # 도구 없이 생성한 텍스트만 보고도 판정할 수 있고(예시/명령/수치가 있는지),
    # rubric 어휘가 늘어나면 판정자가 해석할 여지도 같이 늘어난다.
    #
    # 웹 검색 도구를 실제로 붙이면 이 항목을 되살릴 값이 있다(그때는 백엔드별로
    # 달성 가능성이 달라지므로 백엔드 고정이 선행돼야 한다).
    "research": ["핵심 정보 커버리지", "구체성"],
    "sequential_review": ["이전 단계 반영 여부", "구체성"],
}
_DEFAULT_RUBRIC = ["명확성", "정확성"]

# hierarchical_delegation의 기본 delegation_chain. provider_id는 "{role}-mock" 규칙을
# 따른다 — 실제 provider 선택(Phase 3: api_provider/cli_subscription_provider)이 생기기
# 전까지, 호출부(cli.py/테스트)가 이 규칙에 맞는 이름으로 mock provider를 등록해두면 된다.
#
# "research"는 2026-07-27까지 [research, design_review] 2단계였다 —
# server-engineering-learning 도메인에서 실제 5개 task를 e2e로 돌려보니 final.md(=
# design_review의 출력)가 실제로는 "다음 단계(콘텐츠 작성)에 전달할 검토 의견"이지
# 완성된 산출물이 아니었다(design_review가 스스로 "다음 담당자는 콘텐츠 작성/편집
# 단계"라고 명시). design_review의 비평을 반영해 실제로 완성된 결과물을 쓰는
# 담당자가 체인에 없었던 구조적 공백 — content_finalization을 3번째 역할로 추가해
# 메꿨다. subagent_runner.run_chain()도 이 역할이 "원본 요청 + 직전 비평"뿐 아니라
# research 단계의 원본 초안까지 볼 수 있도록 전체 히스토리를 누적해서 넘기게
# 같이 고쳤다(모듈 docstring 참고).
_DEFAULT_DELEGATION_ROLES: dict[str, list[str]] = {
    "research": ["research", "design_review", "content_finalization"],
    "sequential_review": ["design_review", "implementation_review"],
}

# 체인에 쓸 수 있는 역할(=부서) 전체 목록. `delegation_roles:` override가 이 안에서만
# 조합할 수 있다. **`cli._DELEGATION_ROLES`와 반드시 같아야 한다** — 거기가 provider를
# 등록하는 곳이라, 한쪽에만 추가하면 그 역할을 쓰는 순간 KeyError가 난다
# (`test_step4_planner.py`가 두 목록의 일치를 검증한다).
#
# 뒤 3개(drafting/compliance_review/editing)는 2026-07-29 "회사 부서 모방" 검토에서
# 추가했다. 문서 작업을 5부서로 쪼갠 체인을 config만으로 구성해보고, 역할 세분화
# 자체에 값이 있는지 3단계 체인과 비교 측정하기 위한 것이다.
KNOWN_DELEGATION_ROLES = frozenset(
    {
        "research",  # 조사·자료 수집
        "drafting",  # 초안 작성
        "design_review",  # 설계·구성 관점 검토
        "implementation_review",  # 구현·실행 가능성 검토
        "compliance_review",  # 규정·안전·정책 관점 검토
        "editing",  # 문장·형식 정리
        "content_finalization",  # 검토 의견을 반영해 최종본 완성
    }
)

# 위 역할 중 **산출물이 "다른 산출물에 대한 평가"인 것들** (2026-08-03, ADR 0013).
#
# 체인의 발행물을 고를 때 쓴다 — 검토 역할의 출력은 그 자체로 요청물이 아니라 **요청물을
# 고치기 위한 입력**이다. `_render_chain_final`이 이 목록으로 "무엇을 final.md로 낼지"를
# 정한다.
#
# **`content_finalization`은 여기 없다** — 이름에 review가 없어서가 아니라, 그 역할이
# 실제로 하는 일이 "검토 의견을 반영해 **완성본을 쓰는 것**"이기 때문이다. 판단 기준은
# 이름이 아니라 **산출물의 성격**이다.
REVIEW_DELEGATION_ROLES = frozenset(
    {"design_review", "implementation_review", "compliance_review"}
)

_HIGH_RISK_KEYWORDS = ("배포", "삭제", "프로덕션", "production", "결제", "개인정보", "금융")
_MEDIUM_RISK_KEYWORDS = ("설계", "아키텍처", "정책", "계약")
_RISK_OVERRIDE_PREFIX = "risk_level:"
_TEAM_PATTERN_OVERRIDE_PREFIX = "team_pattern:"
_VALID_TEAM_PATTERNS = ("fan_out_judge", "hierarchical_delegation", "iterative_refinement", "agentic_task")

# 태스크가 자기 부서 구성을 직접 정하는 override (2026-07-29).
#   constraints: ["team_pattern:hierarchical_delegation", "delegation_roles:research,drafting,editing"]
#
# **왜 필요했나**: 체인 역할이 `_DEFAULT_DELEGATION_ROLES[task_type]`으로 고정돼 있어서
# 도메인마다 다른 조직도를 쓸 수 없었다. "실제 회사 부서를 모방한 다중 에이전트" 검토
# (2026-07-29)에서 확인한 갭 중 하나다.
#
# **아직 없는 것(의도적)**: 분업 병렬(`[개발 ∥ 디자인] → 통합`)과 분기·반려는 지원하지
# 않는다. 그건 `delegation_chain`을 리스트에서 DAG로 바꾸는 일이고, 새 패턴 규모다.
# 3단계 체인이 아직 `direct_call` 대비 우위를 입증하지 못한 상태라(2차 측정 동률)
# 역할을 더 늘리는 구조부터 만드는 건 근거가 없다 — ADR 0003과 같은 이유로 보류하고,
# 이 override로 **역할 세분화 자체에 값이 있는지 먼저 싸게 측정**한다.
_DELEGATION_ROLES_OVERRIDE_PREFIX = "delegation_roles:"


def create_plan(task: TaskInput) -> Plan:
    """task를 받아 team_pattern까지 확정된 Plan을 만든다."""
    hint = router.classify_team_pattern(task)
    task_type, team_pattern = hint if hint is not None else (_DEFAULT_TASK_TYPE, _DEFAULT_TEAM_PATTERN)

    # 명시적 override가 있으면 router 분류보다 우선한다 (_infer_risk_level의
    # "risk_level:" override와 대칭). iterative_refinement는 라운드마다 LLM을 2회
    # (generator+evaluator) 호출하는 고비용 패턴이라 키워드 자동 라우팅을 두지
    # 않고 이 opt-in으로만 진입하게 한다.
    override = _team_pattern_override(task)
    if override is not None:
        team_pattern = override

    risk_level = _infer_risk_level(task)
    rubric = _DEFAULT_RUBRICS.get(task_type, list(_DEFAULT_RUBRIC))

    if team_pattern == "agentic_task" and not _has_explicit_risk_override(task):
        # 이 패턴만 유일하게 텍스트가 아니라 실제 파일을 만드는 부수 효과가 있다
        # (ADR 0007). 되돌리기 어려운 행동은 사람이 먼저 봐야 하므로 기존 승인
        # 체크포인트(Section 12.2)를 반드시 통과하도록 위험도를 올린다. 명시적
        # "risk_level:" override가 있으면 그쪽이 우선한다 — 테스트가 승인 게이트를
        # 우회해 실행 경로만 검증할 수 있어야 하기 때문.
        risk_level = "high"

    if team_pattern == "agentic_task":
        return Plan(
            task_id=task.task_id,
            task_type=task_type,
            risk_level=risk_level,
            rubric=rubric,
            team_pattern=team_pattern,
        )

    if team_pattern == "fan_out_judge":
        return Plan(
            task_id=task.task_id,
            task_type=task_type,
            risk_level=risk_level,
            rubric=rubric,
            team_pattern=team_pattern,
            num_candidates=_DEFAULT_NUM_CANDIDATES,
        )

    if team_pattern == "iterative_refinement":
        return Plan(
            task_id=task.task_id,
            task_type=task_type,
            risk_level=risk_level,
            rubric=rubric,
            team_pattern=team_pattern,
        )

    roles = _delegation_roles_override(task) or _DEFAULT_DELEGATION_ROLES.get(
        task_type, ["research", "design_review"]
    )
    delegation_chain = [DelegationStep(role=role, provider_id=f"{role}-mock") for role in roles]
    return Plan(
        task_id=task.task_id,
        task_type=task_type,
        risk_level=risk_level,
        rubric=rubric,
        team_pattern=team_pattern,
        delegation_chain=delegation_chain,
    )


def _has_explicit_risk_override(task: TaskInput) -> bool:
    return any(
        constraint.startswith(_RISK_OVERRIDE_PREFIX)
        and constraint[len(_RISK_OVERRIDE_PREFIX):] in ("low", "medium", "high")
        for constraint in task.constraints
    )


def _team_pattern_override(task: TaskInput) -> TeamPattern | None:
    for constraint in task.constraints:
        if constraint.startswith(_TEAM_PATTERN_OVERRIDE_PREFIX):
            pattern = constraint[len(_TEAM_PATTERN_OVERRIDE_PREFIX):]
            if pattern in _VALID_TEAM_PATTERNS:
                return pattern  # type: ignore[return-value]
    return None


def _delegation_roles_override(task: TaskInput) -> list[str] | None:
    """`delegation_roles:a,b,c`로 지정한 부서 구성을 읽는다. 없으면 None.

    **알려진 역할만 허용한다.** 임의 문자열을 받으면 `run_chain`이
    `providers["{role}-mock"]`에서 KeyError로 죽는데(호출부가 그 역할의 provider를
    등록해두지 않았으므로), 그건 오타를 런타임 크래시로 알려주는 셈이다. 여기서
    미리 걸러 어느 역할이 문제인지 말해준다 — provider 등록 목록의 출처는
    `cli._DELEGATION_ROLES`이고, 새 부서를 만들려면 거기에도 추가해야 한다.
    """
    for constraint in task.constraints:
        if not constraint.startswith(_DELEGATION_ROLES_OVERRIDE_PREFIX):
            continue
        raw = constraint[len(_DELEGATION_ROLES_OVERRIDE_PREFIX):]
        roles = [role.strip() for role in raw.split(",") if role.strip()]
        if not roles:
            continue
        unknown = [role for role in roles if role not in KNOWN_DELEGATION_ROLES]
        if unknown:
            raise ValueError(
                f"알 수 없는 위임 역할: {unknown} "
                f"(사용 가능: {sorted(KNOWN_DELEGATION_ROLES)}). "
                f"새 부서를 추가하려면 planner.KNOWN_DELEGATION_ROLES와 "
                f"cli._DELEGATION_ROLES 양쪽에 넣어야 한다."
            )
        return roles
    return None


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
