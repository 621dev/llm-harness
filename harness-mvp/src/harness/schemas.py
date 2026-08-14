"""스키마 정의 (pydantic 기반).

harness-implementation-plan-ko.md Section 3 (핵심 스키마) 를 구현한다.
모든 컴포넌트 간 입출력은 이 스키마를 통해서만 주고받는다 (action/observation contract).

원래 Step 0 스캐폴딩은 Cowork sandbox에 외부 네트워크가 없어 표준 라이브러리
dataclasses로 임시 구현했었다. 이 파일은 그걸 pydantic.BaseModel로 교체한 버전이다.
dataclasses 대비 달라지는 점:

- 강한 타입 검증: 생성 시점에 잘못된 값(Literal에 없는 값, 타입 불일치 등)을 넣으면
  `pydantic.ValidationError`(ValueError의 서브클래스)가 즉시 발생한다.
- 자동 JSON 파싱/직렬화: `Model.model_validate(dict_or_json)` / `model.model_dump(mode="json")`
  로 왕복 변환이 가능하다 (datetime 등도 자동으로 JSON 호환 타입으로 변환).

호출부 변경: 기존 `obj.to_dict()` 대신 `obj.model_dump(mode="json")`을 쓴다
(`orchestrator.py`의 `write_json`/`write_markdown` 호출부 참고).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field

TeamPattern = Literal["fan_out_judge", "hierarchical_delegation", "iterative_refinement", "agentic_task"]
AuthMode = Literal["api_key", "cli_subscription"]
RiskLevel = Literal["low", "medium", "high"]
StepStatus = Literal["success", "error"]

# 조각은 "아직 안 돌았다"는 상태가 실재한다 — 매니저가 분해한 직후와 워커가 돌기 전
# 사이가 그 상태이고, `delegation.json`에 그대로 남아야 "무엇이 실행되지 않았나"가 보인다
# (예산 상한에 걸려 안 돌린 조각과 실패한 조각은 다른 사실이다).
PartStatus = Literal["pending", "success", "error"]
ObservationStatus = Literal["success", "warning", "error"]
NextAction = Literal["retry", "continue", "skip", "ask_user"]
ApprovalStatus = Literal["not_required", "pending", "approved", "rejected"]


class TaskInput(BaseModel):
    """하네스에 들어오는 원본 작업 입력. input.json 으로 저장된다."""

    task_id: str
    prompt: str
    # 현재 실제로 해석되는 건 "risk_level:<level>"(planner._infer_risk_level)과
    # "team_pattern:<pattern>"(planner._team_pattern_override) 접두사뿐이다.
    # 그 외 문자열은 input.json에 기록만 되고 plan/실행에는 반영되지 않는다 — 규칙 기반
    # mock 단계라 무해하지만, 진짜 LLM 프롬프트에 자동 반영될 거라 오해하지 않도록 명시.
    constraints: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class DelegationStep(BaseModel):
    """Hierarchical Delegation 패턴의 체인 한 단계."""

    role: str  # 예: "research", "design_review"
    provider_id: str
    input_ref: Optional[str] = None
    output_ref: Optional[str] = None
    status: StepStatus = "success"
    subscription_calls: int = 0  # 이 스텝이 소모한 구독 호출 수 (Candidate와 같은 의미)
    # 컨텍스트 격리는 "전체 텍스트를 오케스트레이터 컨텍스트에 노출하지 않는다"는 것이지
    # 작은 숫자 지표까지 숨기는 게 아니다. latency/cost는 metrics.json 집계(Cost Blindness
    # 방지, Section 9)를 위해 여기 남겨둔다.
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None


class WorkerPart(BaseModel):
    """매니저가 쪼갠 산출물 한 조각. 워커 하나가 이걸 받아 **파일 하나**를 만든다 (ADR 0014).

    `DelegationStep`(역할 체인)의 후신이다. 차이가 둘인데 그게 이 패턴의 전부다:

    1. `role`("누가 하나") 대신 `title`("무엇을 만드나") — 조각은 최종 문서의 한 섹션이다.
    2. **조각끼리 서로를 보지 않는다.** 체인은 앞 단계 출력 전문을 다음 단계에 다시
       실어 보내 입력이 제곱에 가깝게 자랐다(3역할 78배, 5역할 320배). 조각은 원본
       요청과 자기 지시만 받으므로 **조각 수에 선형**이다.

    `content`가 없는 게 의도다 — 본문은 `output_ref` 파일에만 있고, 매니저 컨텍스트에는
    `title`/`chars`/경로만 올라간다. 그게 이 패턴이 아끼려는 것이다.
    """

    title: str
    instruction: str
    provider_id: Optional[str] = None  # 실제로 어느 워커가 만들었나 (실행 후 채워짐)
    output_ref: Optional[str] = None
    chars: Optional[int] = None  # 본문 대신 크기만 — 매니저가 보는 유일한 규모 정보
    status: PartStatus = "pending"
    subscription_calls: int = 0
    # 컨텍스트 격리는 "본문을 노출하지 않는다"는 것이고 작은 숫자 지표를 숨기는 게
    # 아니다(DelegationStep과 같은 이유 — Cost Blindness 방지, Section 9).
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    input_tokens: Optional[int] = None


class DelegationPlan(BaseModel):
    """매니저가 만든 분해 계획. **이 패턴에서 매니저가 실제로 붙잡고 있는 유일한 것.**

    작게 유지되는 게 핵심이다 — 조각 본문이 아니라 제목·지시·머리글만 담는다.
    `concat` 조립 모드에서 최종 문서의 뼈대가 이것이고, 본문은 조각 파일에서 온다.
    """

    document_title: str
    intro: str  # 문서 머리글. 본문이 만들어지기 **전에** 쓰므로 조각을 읽지 않아도 된다
    parts: list[WorkerPart] = Field(default_factory=list)


class ProviderConfig(BaseModel):
    """어떤 provider를 어떤 인증 모드로 호출할지 (Section 10 참고)."""

    provider_id: str
    model_id: str
    auth_mode: AuthMode = "api_key"


class Plan(BaseModel):
    """Planner의 산출물. plan.md/plan.json 으로 저장된다."""

    task_id: str
    task_type: str
    risk_level: RiskLevel = "medium"
    rubric: list[str] = Field(default_factory=list)
    team_pattern: TeamPattern = "fan_out_judge"
    num_candidates: Optional[int] = None  # fan_out_judge 전용
    delegation_chain: list[DelegationStep] = Field(default_factory=list)  # hierarchical_delegation 전용


class Candidate(BaseModel):
    """Model Runner 산출물 (fan_out_judge 전용)."""

    model_id: str
    content: str
    # `tokens`는 **출력** 토큰이다(Gemini `candidatesTokenCount`). 이름이 애매하지만
    # 기존 run 산출물과의 호환 때문에 유지하고, 입력은 별 필드로 둔다.
    tokens: Optional[int] = None
    # 입력 토큰 (2026-07-29 추가). 그전까지 비용을 **출력 토큰만으로** 계산해서
    # 체인처럼 입력이 큰 패턴의 비용이 과소 집계됐다 — 체인은 스텝마다 이전 결과를
    # 통째로 받아 입력이 direct_call의 90배가 넘는데(실측) 그게 cost_usd에 전혀
    # 반영되지 않았다. 종량제 키를 쓰면 입력도 실제 청구 대상이므로 따로 센다.
    input_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None  # auth_mode="api_key" 일 때만 채움
    status: StepStatus = "success"
    # auth_mode="cli_subscription" provider를 실제로 호출한 횟수(재시도 포함).
    # 구독 호출은 cost_usd가 None이라 비용 지표에 전혀 안 잡히는데, 실제로는 5시간/주간
    # 롤링 한도를 소모하는 실비용이다 — 금액 대신 횟수로라도 보이게 한다
    # (Section 9 Cost Blindness 방지). api_key provider면 0. model_runner가 채운다.
    subscription_calls: int = 0


class JudgingScore(BaseModel):
    """후보 하나에 대한 judge 판정.

    `weaknesses`만 있고 strengths가 없는 건 의도적이다 — ADR 0004의 reject-first
    원칙상 judge는 "결함을 근거와 함께 찾는" 일만 한다("문제 없음"을 기본값으로
    주지 않기 위해). ADR 0004는 `strengths` 재활용 여부를 구현 단계 판단으로
    열어뒀고, 구현은 weaknesses만 쓰기로 정했다 — 늘 빈 배열이던 strengths
    필드는 2026-07-28 정기적 정리에서 제거했다.
    """

    candidate: str
    score: float
    weaknesses: list[str] = Field(default_factory=list)


class Judging(BaseModel):
    """Judge 산출물. judging.json 으로 저장된다 (fan_out_judge 전용).

    latency_ms/cost_usd는 judge 호출 자체(candidate 생성과 별개)의 지연/비용이다
    (ADR 0004, Section 9 "Cost Blindness 방지" — 작은 숫자 지표는 컨텍스트
    격리 대상이 아니라 DelegationStep과 동일한 이유로 계속 남긴다).
    """

    scores: list[JudgingScore]
    recommended_strategy: str
    winner: str
    # 1·2등 점수가 판정자가 구분하기 어려울 만큼 붙어 있었나 (2026-07-29, ADR 0011).
    #
    # 예전엔 이 상태를 `recommended_strategy="merge_top_candidates"`로 표현하고
    # synthesizer가 상위 두 후보를 이어붙였는데, 그러면 **final.md가 완결된 문서 두 개**가
    # 됐다(7차 측정에서 "절차서 하나"를 요구한 프롬프트를 위반해 불합격). 병합은 폐기했지만
    # **"판정자가 못 갈랐다"는 신호 자체는 버리지 않는다** — 측정이 이걸 읽고 있고,
    # 우열이 근소한 run을 사후에 구분할 수 있어야 한다.
    top_scores_near_tie: bool = False
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    subscription_calls: int = 0  # judge 호출이 구독 provider였다면 그 횟수


class RefinementVerdict(BaseModel):
    """judge.check_pass()의 산출물 (iterative_refinement 전용).

    Judging(N개 후보 비교)과 다른 질문 — "이 콘텐츠 하나가 rubric을 통과하는가"에
    대한 pass/fail + 다음 라운드에 전달할 구체적 피드백이다. latency_ms/cost_usd는
    evaluator 호출 자체의 지연/비용 (Cost Blindness 방지, Judging과 동일한 이유).
    """

    passed: bool
    feedback: str
    # 미충족으로 지목된 rubric 항목 (2026-07-29). 2차 측정에서 evaluator가 rubric에
    # 없는 요건("시각 자료가 없다")을 발명해 불합격시킨 게 관측돼서, 판정을 rubric
    # 항목에 묶기 위해 출력에 넣게 했다. `passed=False`인데 이게 비어 있으면 바로
    # 그 실패 유형이라는 신호다(feedback 앞에 표시가 붙는다 — judge.py 참고).
    unmet_rubric_items: list[str] = Field(default_factory=list)
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    subscription_calls: int = 0  # evaluator 호출이 구독 provider였다면 그 횟수


class RefinementRound(BaseModel):
    """iterative_refinement 패턴의 라운드 하나. refinement.json에 목록으로 저장된다.

    latency_ms/cost_usd는 해당 라운드의 generator+evaluator 호출 합산이다.
    """

    round_index: int
    content: str
    passed: bool
    feedback: str
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None
    subscription_calls: int = 0  # 이 라운드의 generator+evaluator 구독 호출 합산


class AgentToolUse(BaseModel):
    """에이전트가 실제로 호출한 도구 하나 (agentic_task 전용, ADR 0007).

    다른 패턴에는 없는 개념이다 — 이 패턴에서만 모델이 텍스트를 내놓는 대신
    도구를 호출해 파일을 만든다. target은 도구 입력에서 뽑은 짧은 식별자
    (파일 도구면 파일 경로), 없으면 None.
    """

    tool: str  # 예: "Write", "Read", "Edit"
    target: Optional[str] = None


class AgentTurn(BaseModel):
    """에이전트 루프의 턴 하나. agent_turns.json에 목록으로 저장된다.

    "하네스가 자율 에이전트를 감싼다"는 건 이 기록이 남는다는 뜻이다 — 에이전트가
    무슨 판단으로 어떤 도구를 썼는지 사후에 재현/감사할 수 있어야 한다.
    """

    turn_index: int
    text: str = ""
    tool_uses: list[AgentToolUse] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """agent_runner.run_agent_task()의 산출물 (agentic_task 전용).

    stop_reason: "completed"(에이전트가 스스로 종료) / "max_turns"(턴 상한 도달 —
    실패가 아니라 partial 승격 대상) / "error"(에이전트가 오류로 종료).
    produced_files는 CLI 보고가 아니라 실행 후 워크스페이스를 실제로 스캔한
    결과다(에이전트 자기 보고를 신뢰하지 않는다).
    """

    turns: list[AgentTurn] = Field(default_factory=list)
    final_text: str = ""
    produced_files: list[str] = Field(default_factory=list)
    # 안전 경계가 실제로 막아낸 시도들(CLI의 permission_denials). 비어 있는 게
    # 정상이지만, 값이 있다는 건 에이전트가 경계 밖으로 나가려 했다는 뜻이라
    # 감사 증거로 남긴다 — 경계가 "설정돼 있다"가 아니라 "작동했다"의 근거다.
    blocked_tool_uses: list[AgentToolUse] = Field(default_factory=list)
    num_turns: Optional[int] = None
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None  # auth_mode="cli_subscription"이면 None (구독 사용량은 $ 집계 밖)
    # 에이전트는 턴마다 모델을 부르므로 run 하나가 곧 여러 번의 구독 호출이다 —
    # 이 패턴이 다른 패턴보다 구독 한도를 많이 쓴다는 게 지표로 보여야 한다.
    subscription_calls: int = 0
    stop_reason: Literal["completed", "max_turns", "error"] = "completed"


class RunMetrics(BaseModel):
    """metrics.json 으로 저장된다. 패턴 무관 공통."""

    latency_ms: int
    completed_candidates_or_steps: int
    failed_candidates_or_steps: int
    estimated_cost_usd: Optional[float] = None  # auth_mode="api_key" 일 때만
    # 구독(cli_subscription) provider 호출 횟수. estimated_cost_usd가 $0으로 보이는
    # run도 실제로는 구독 한도를 소모했을 수 있어서, 그 몫을 횟수로 남긴다
    # (Section 9 Cost Blindness 방지 — 금액과 한도는 다른 자원이다).
    subscription_calls: int = 0


class Observation(BaseModel):
    """모든 action 호출의 공용 반환 타입 (Action/Observation contract)."""

    status: ObservationStatus
    summary: str
    artifacts: list[str] = Field(default_factory=list)
    next_actions: list[NextAction] = Field(default_factory=list)


class FitnessCheck(BaseModel):
    """router.check_fitness(task) 산출물. fitness_check.json 으로 저장된다 (Section 12.1).

    passed=False 면 Planner/패턴 분기를 건너뛰고 model_runner.direct_call() 로 처리한다.
    실제 판정 로직은 Step 5 에서 구현된다 (지금은 스키마만 선반영).
    """

    passed: bool
    reason: str


class Approval(BaseModel):
    """사람 승인 체크포인트 산출물. approval.json 으로 저장된다 (Section 12.2).

    plan.risk_level == "high" 일 때만 "pending" 으로 생성되고, 사람이 approved/rejected
    로 갱신하기 전까지 candidate/chain 실행을 막는다. 실제 게이팅 로직은 Step 8 에서
    orchestrator.run() 에 연결된다 (지금은 스키마만 선반영).
    """

    status: ApprovalStatus = "not_required"
    note: Optional[str] = None
    decided_at: Optional[datetime] = None


class EvalCase(BaseModel):
    """Phase 2 Eval Harness의 입력. 동일 task를 k번 반복 실행해서 pass@k를 측정한다.

    required_phrases/forbidden_phrases는 deterministic grader가 final.md를 채점할 때
    쓰는 규칙이다 (Section: "deterministic grader를 우선 사용하고, 주관적 품질 평가는
    이후 model judge/human review로 보완"). risk_level="high"로 분류될 프롬프트는
    피해야 한다 — 승인 대기(pending)에서 멈추면 grader가 채점할 final.md 자체가 없다.
    """

    name: str
    task: TaskInput
    required_phrases: list[str] = Field(default_factory=list)
    forbidden_phrases: list[str] = Field(default_factory=list)


class GradeResult(BaseModel):
    """graders.grade()의 산출물. run 하나에 대한 pass/fail 판정."""

    passed: bool
    reason: str
    checked_phrases: list[str] = Field(default_factory=list)


class AttemptResult(BaseModel):
    """EvalCase를 한 번 실행한 결과 (run_id + 채점 + 비용/지연)."""

    run_id: str
    run_status: ObservationStatus
    grade: GradeResult
    latency_ms: Optional[int] = None
    cost_usd: Optional[float] = None


class EvalReport(BaseModel):
    """runner.run_case_k_times()의 최종 산출물.

    pass_rate/pass_at_k/pass_pow_k는 affaan-m/ECC의 pass@1/pass@k/pass^k 지표를
    "동일 케이스를 k번 반복 실행한 한 세트"에 대해 근사한 값이다 (실제 통계적 pass@k는
    이런 세트를 여러 번 반복해서 평균내야 하지만, 이 MVP는 인프라 검증이 목적이라
    단순화했다 — Phase 5 이후 다듬을 여지가 있다).

    - pass_rate: k번 시도 중 개별 성공 비율 (pass@1의 경험적 근사)
    - pass_at_k: k번 중 최소 1번이라도 성공했는가 (0.0 또는 1.0)
    - pass_pow_k: k번 전부 성공했는가 (0.0 또는 1.0)
    - cost_per_success/latency_per_success: 성공한 시도만 평균 (실패는 분모에서 제외)
    """

    case_name: str
    attempts: list[AttemptResult]
    pass_rate: float
    pass_at_k: float
    pass_pow_k: float
    cost_per_success: Optional[float] = None
    latency_per_success: Optional[float] = None


class FailureCategory(BaseModel):
    """failure_analysis.analyze_failures()가 만드는 실패 유형 하나.

    key는 errors.json의 stage 값(예: "safety", "chain step 'research'") 또는
    safety_review.json note를 세미콜론 단위로 쪼갠 개별 finding 문구다. count가
    많을수록 반복적으로 관측된 실패라는 뜻이다.
    """

    key: str
    count: int
    example_run_ids: list[str] = Field(default_factory=list)


class FailureReport(BaseModel):
    """failure_analysis.analyze_failures()의 최종 산출물 (Phase 5).

    자동으로 규칙/프롬프트를 고치지 않는다 — Planner/Judge/Safety 규칙 개선은 이
    리포트를 사람이 보고 판단한다(ADR 0003의 재검토 트리거와 연결).
    """

    total_runs_scanned: int
    runs_with_errors: int
    runs_with_safety_review: int
    error_categories: list[FailureCategory] = Field(default_factory=list)
    safety_categories: list[FailureCategory] = Field(default_factory=list)


class PatternStats(BaseModel):
    """dashboard.build_dashboard()가 만드는 team_pattern별 집계 하나 (Phase 6).

    team_pattern="direct_call"은 적합성 게이트(FitnessCheck.passed=False)를 통과하지
    못해 패턴 분기 없이 단일 호출로 처리된 run이다(plan.json 자체가 없음).
    """

    team_pattern: str
    total_runs: int
    success_count: int
    warning_count: int
    error_count: int
    avg_latency_ms: Optional[float] = None
    avg_cost_usd: Optional[float] = None
    # 평균이 아니라 누적 합계다 — 구독 한도는 "run당 평균"보다 "이 패턴이 지금까지
    # 얼마나 썼는가"가 실제로 관리해야 할 값이기 때문(cost와 성격이 다르다).
    total_subscription_calls: int = 0


class DashboardReport(BaseModel):
    """dashboard.build_dashboard()의 최종 산출물. dashboard.render_html()로 정적
    HTML로 렌더링된다 (Phase 6).

    eval pass@k는 포함하지 않는다 — EvalReport는 디스크에 저장된 적이 없어(Phase 2),
    "저장된 run 산출물만 집계"라는 이 리포트의 범위 밖이다.
    """

    total_runs_scanned: int
    patterns: list[PatternStats] = Field(default_factory=list)


class FetchResult(BaseModel):
    """fetchers.Fetcher.fetch()의 조회 결과 (cloud-ops 도메인, 클라우드 가격 API 등).

    Provider.generate()가 만드는 Candidate(LLM이 새로 생성한 콘텐츠)와 역할이 다르다 —
    FetchResult는 LLM을 거치지 않은 외부 시스템의 원본 조회값이다. "판단/생성"이 아니라
    "읽기 전용 조회"라는 점에서 cloud-ops 도메인을 '텍스트 출력만'으로 좁힌 결정과 같은
    안전 경계 안에 있다(아무것도 바꾸지 않음).
    """

    source: str  # fetcher_id (예: "aws_ec2_price", "ncp_server_price")
    status: Literal["success", "error"]
    summary: str
    data: dict = Field(default_factory=dict)
    fetched_at: datetime
