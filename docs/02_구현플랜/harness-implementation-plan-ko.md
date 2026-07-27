# 멀티 LLM 하네스 구현 플랜

기반 문서: `../01_개념설명/harness-repo-summary-*-ko.md`(레포 분석). 최초 작성 당시의
인수인계 배경 문서(v2)는 2026-07-24 문서 정리로 삭제됐다 — 현재 상태는
`../03_진행상황/harness-progress-checklist-ko.md`, 이력은
`harness-progress-detail-ko.md`를 볼 것.

참고 레포: `revfactory/harness`, `affaan-m/ECC`, `gaebalai/claude-code-orchestrator`,
`jikime/harness-lab`(적합성 게이트/ADR/사람 승인 체크포인트 개념, Section 12 참고)
구현 언어: Python
범위: MVP (Reproducible Run) + 팀 패턴 분기(Team Pattern Dispatch) 구현 계획

## 1. 목표

Planner가 작업 성격에 따라 팀 패턴을 고르고, Orchestrator가 그 패턴에 맞는 flow를 실행하는
파일 기반 실행-평가 하네스의 MVP를 만든다. 최초 계획의 패턴은 두 가지였고,
MVP 완료 후 세 번째 패턴이 추가됐다(ADR 0006, 2026-07-27).

1. **Fan-out/Judge 패턴**: 여러 모델이 독립적으로 후보를 생성하고, Judge가 비교 평가하고,
   Synthesizer가 최종 답을 합성한다. 품질 비교가 중요한 작업(설계안 검토, 콘텐츠 생성)에 쓴다.
2. **Hierarchical Delegation 패턴**: 컨텍스트를 격리한 서브에이전트가 리서치·설계 리뷰 등
   역할별로 특화된 모델에 순차적으로 위임하고, 요약된 결과만 메인 Orchestrator로 반환한다.
   역할 분업과 컨텍스트/비용 절약이 중요한 작업(리서치, 순차 검토)에 쓴다.
3. **Iterative Refinement 패턴** (ADR 0006): generator 1개가 생성 → evaluator가 rubric
   합격 판정 + 수정 피드백 → 불합격이면 피드백을 반영해 재생성을 반복한다(라운드 상한
   `max_refinement_rounds`, 기본 3 — config.json). 단발 생성으로 rubric을 채우기 어려워
   반복 개선이 필요한 작업에 쓰며, 라운드마다 LLM 2회 호출이 발생하는 고비용 패턴이라
   키워드 자동 라우팅 없이 `constraints: ["team_pattern:iterative_refinement"]` opt-in으로만
   진입한다.

패턴들은 목적이 다르므로 하나로 합치지 않고, `team_pattern` 필드로 분기해서 필요한 경우에만
해당 비용을 지불하도록 설계한다. (완전 통합안은 모든 작업에 두 단계 비용을 강제해
cost per success 관점에서 비효율적이라 기각했다.)

실제 LLM API 연동 전에는 두 패턴 모두 mock provider로 파이프라인을 먼저 검증한다.

## 2. 디렉토리 구조

```text
src/
  harness/
    orchestrator.py       # team_pattern에 따라 flow를 분기 실행하는 dispatcher
    planner.py             # task -> plan.md/plan.json (team_pattern 결정 포함)
    router.py               # (선택) 규칙 기반 사전 분류 훅, Planner LLM 호출 전 저비용 필터
                             # + 적합성 게이트(check_fitness): 하네스화 자체가 필요한 작업인지 사전 판정 (Section 12.1)
    model_runner.py           # Fan-out/Judge 패턴: 독립 후보 생성
    subagent_runner.py         # Hierarchical Delegation 패턴: 컨텍스트 격리 + 위임 실행
    judge.py                     # 후보 비교 평가 (Fan-out/Judge 패턴 전용)
    synthesizer.py                 # 최종 답변 합성 (Fan-out/Judge 패턴 전용)
    safety.py                        # 체크리스트 기반 안전성 점검 (패턴 공통)
    run_store.py                       # run 디렉토리 입출력 (패턴 공통)
    schemas.py                           # pydantic 모델 정의
  providers/
    base.py                # Provider 인터페이스 (auth_mode 무관 공통)
    mock.py                  # 결정적 mock 응답
    api_provider.py            # API 키 기반 REST 호출 (openai/anthropic/gemini 공용 베이스, Phase 3 이후)
    cli_subscription_provider.py # 구독 로그인 기반 CLI subprocess 호출 (claude/codex/gemini CLI, Phase 3 이후)
  evals/
    graders.py               # deterministic / rule grader
    runner.py                  # pass@k 측정 (패턴 무관, run 단위로 평가)
  cli.py                        # 진입점 (run, replay)

docs/
  adr/                            # 구조 결정 기록 (Architecture Decision Record, Section 12.3)
    0001-team-pattern-dispatch.md  # 예: 두 패턴을 통합하지 않고 분기하기로 한 결정과 이유

_workspace/
  runs/<run_id>/
    input.json
    plan.md                  # team_pattern 명시
    approval.json             # risk_level="high"일 때만 생성 (사람 승인 체크포인트, Section 12.2)
    artifacts/
      candidates/             # Fan-out/Judge 패턴 전용
        model-a.md
        model-b.md
        model-c.md
      chain/                    # Hierarchical Delegation 패턴 전용
        step-1-research.md
        step-2-design-review.md
    judging.json                 # Fan-out/Judge 패턴에서만 생성
    refinement.json               # Iterative Refinement 패턴에서만 생성 (라운드별 기록, ADR 0006)
    final.md
    safety.md
    metrics.json
    errors.json
```

## 3. 핵심 스키마 (schemas.py)

- `TaskInput`: task_id, prompt, constraints, created_at
- `Plan`: task_type, risk_level, rubric(list[str]),
  `team_pattern: Literal["fan_out_judge", "hierarchical_delegation", "iterative_refinement"]`,
  `num_candidates`(fan_out_judge 전용), `delegation_chain: list[DelegationStep]`(hierarchical_delegation 전용)
- `DelegationStep`: role(예: "research", "design_review"), provider_id, input_ref, output_ref, status
- `ProviderConfig`: provider_id, `auth_mode: Literal["api_key", "cli_subscription"]`, model_id
- `Candidate`: model_id, content, tokens, latency_ms, cost_usd(auth_mode="api_key"일 때만 채움), status(success/error)
- `Judging`: scores(candidate, score, strengths, weaknesses), recommended_strategy, winner
- `RefinementVerdict`/`RefinementRound` (iterative_refinement 전용, ADR 0006):
  verdict는 `judge.check_pass()`의 합격 판정(passed, feedback), round는 라운드 하나의
  기록(round_index, content, passed, feedback, latency_ms, cost_usd — `refinement.json`에
  목록으로 저장)
- `RunMetrics`: latency_ms, estimated_cost_usd(nullable), `quota_usage_pct`(auth_mode="cli_subscription"일 때만 채움),
  completed_candidates_or_steps, failed_candidates_or_steps
- `Observation` (공용 contract): status, summary, artifacts, next_actions
- `FitnessCheck` (jikime/harness-lab 차용, Section 12.1): passed(bool), reason(str),
  estimated_direct_cost_usd(nullable). `router.py`가 Plan 생성 전에 산출하며,
  `passed=False`면 Planner/Orchestrator 패턴 분기를 건너뛰고 direct call로 처리한다.
- `Approval` (jikime/harness-lab 차용, Section 12.2): status(`not_required`/`pending`/
  `approved`/`rejected`), note, decided_at(nullable). `Plan.risk_level == "high"`일 때만
  `pending`으로 생성되고, 사람이 승인/반려하기 전까지 candidate/chain 실행을 막는다.

모든 컴포넌트 간 입출력은 이 스키마를 통해서만 주고받는다. (action/observation contract)
`team_pattern`은 Plan 생성 시 한 번만 결정되고, 이후 흐름 전체가 이 값을 따른다.

## 4. Action / Observation Contract

에이전트가 호출하는 행동을 명시적으로 제한한다.

```text
# 공통
write_artifact(run_id, name, content) -> Observation

# Fan-out/Judge 패턴
run_model(model_id, prompt, temperature) -> Observation
judge_candidates(candidates, rubric) -> Observation
synthesize(candidates, judging) -> Observation

# Hierarchical Delegation 패턴
delegate_to_subagent(role, provider_id, input_ref) -> Observation
  # 서브에이전트 내부에서 대용량 출력을 소화하고, 요약된 결과만 반환한다.
  # 메인 Orchestrator 컨텍스트에는 summary와 output_ref(파일 경로)만 노출된다.
```

각 Observation은 `status`, `summary`, `artifacts`, `next_actions` 필드를 반드시 포함한다.

## 5. 패턴 분기 (Orchestrator Dispatch)

Orchestrator는 고정된 한 줄 파이프라인이 아니라 `team_pattern` 값에 따라 flow를 선택하는
dispatcher로 구현한다.

```text
def run(task):
    fitness = router.check_fitness(task)          # Section 12.1: 하네스화 가치 사전 판정
    write_artifact(run_id, "fitness_check.json", fitness)
    if not fitness.passed:
        final = model_runner.direct_call(task)     # 패턴 분기 없이 단일 모델 호출
        safety_result = safety.check(final)         # Safety는 절대 생략하지 않음
        write_artifact(run_id, "final.md", final)
        write_artifact(run_id, "safety.md", safety_result)
        write_metrics_and_errors(run_id)
        return

    plan = planner.create_plan(task)   # team_pattern 결정
    write_artifact(run_id, "plan.md", plan)

    if plan.risk_level == "high":                  # Section 12.2: 사람 승인 체크포인트
        approval = request_approval(plan)           # ask_user, 승인 전까지 아래 실행 차단
        write_artifact(run_id, "approval.json", approval)
        if approval.status != "approved":
            write_metrics_and_errors(run_id)
            return

    if plan.team_pattern == "fan_out_judge":
        candidates = model_runner.run_all(plan)
        judging = judge.evaluate(candidates, plan.rubric)
        final = synthesizer.synthesize(candidates, judging)
    elif plan.team_pattern == "hierarchical_delegation":
        chain_results = []
        for step in plan.delegation_chain:
            obs = subagent_runner.delegate(step)
            chain_results.append(obs)
        final = chain_results[-1]   # 마지막 위임 결과가 최종안
        judging = None               # 이 패턴에서는 Judge 단계 자체가 없음
    elif plan.team_pattern == "iterative_refinement":   # ADR 0006 (2026-07-27 추가)
        prompt = task.prompt
        for round_index in range(max_refinement_rounds):
            content = model_runner.generate(prompt)
            verdict = judge.check_pass(content, plan.rubric)   # 합격 판정 + 수정 피드백
            record_round(run_id, content, verdict)              # refinement.json 누적
            if verdict.passed:
                break
            prompt = build_refinement_prompt(task.prompt, content, verdict.feedback)
        final = content   # 상한까지 미통과면 마지막 시도를 partial 승격(Section 6과 동일 철학)

    safety_result = safety.check(final)
    write_artifact(run_id, "final.md", final)
    write_artifact(run_id, "safety.md", safety_result)
    write_metrics_and_errors(run_id)
```

Planner가 `team_pattern`을 정하는 기본 규칙(초기엔 LLM 호출 없이 규칙 기반으로 시작):

| task_type | team_pattern |
| --- | --- |
| research, investigation | hierarchical_delegation |
| sequential_review (설계 리뷰 → 구현 리뷰 등 단계적 검토) | hierarchical_delegation |
| architecture design, content generation, 비교가 필요한 작업 | fan_out_judge |
| 분류 애매 | 기본값 fan_out_judge + ask_user로 확인 |

`iterative_refinement`는 이 표(키워드 자동 라우팅)에 없다 — 라운드마다 LLM 2회 호출이
발생하는 고비용 패턴이라 실수로 걸리지 않게, `TaskInput.constraints`의
`"team_pattern:<pattern>"` 명시적 override(planner의 `risk_level:` override와 대칭)로만
진입한다(ADR 0006). 이 override는 어느 패턴에나 쓸 수 있고 router 분류보다 우선한다.

`router.py`는 이 규칙 판단을 Planner LLM 호출 이전에 저비용으로 먼저 걸러내는 선택적 훅이다.
명백한 케이스(예: "리서치해줘")는 LLM 호출 없이 바로 라우팅하고, 애매한 경우만 Planner에 위임한다.

`router.check_fitness(task)`는 team_pattern 분류보다 앞서 실행되는 더 저렴한 사전 필터다
(jikime/harness-lab의 적합성 게이트 개념, Section 12.1). 단순 사실 확인, 한 줄 요약처럼
"여러 관점 비교"나 "역할 분업"의 이득이 비용을 못 넘는 작업은 `passed=False`로 판정해
패턴 분기 자체를 건너뛰고 단일 모델 direct call로 처리한다. 애매하면 기본값 `passed=True`
(과소적용보다 과대적용이 안전).

## 6. 복구 전략 (recovery contract)

모든 재시도는 상한이 명시된 종료 계약(jikime/harness-lab의 exit contract 개념,
Section 12.1 연장선)을 따른다. 즉 "무한정 다시 시도"는 없고, 실패 시 최종적으로
`ask_user`나 `partial` 승격처럼 정해진 종결 상태로 반드시 수렴한다.

| 실패 상황 | 전략 |
| --- | --- |
| (공통) 모델 호출 실패 | 1회 재시도(고정 상한, 무한 재시도 금지) → 재실패 시 해당 후보/스텝 error 기록 |
| (공통) risk_level="high" 승인 대기 중 반려(rejected) | run을 "rejected" 상태로 종료, candidate/chain 실행 없이 metrics.json/errors.json만 기록 (Section 12.2) |
| (fan_out_judge) min_candidates(예: 2개) 이상 성공 | 계속 진행 |
| (fan_out_judge) min_candidates 미만 성공 | ask_user, errors.json에 사유 기록 |
| (fan_out_judge) Judge 결과 파싱 실패 | 1회 재시도 → 실패 시 rule-based fallback(최다 rubric 충족 후보 선택) |
| (hierarchical_delegation) 체인 중간 스텝 실패 | 1회 재시도 → 재실패 시 해당 스텝 error 기록 후 체인 중단, errors.json에 어느 스텝에서 끊겼는지 기록 |
| (hierarchical_delegation) 체인 중단 시 | 마지막으로 성공한 스텝 결과를 final.md로 승격하고 상태를 "partial"로 표시, ask_user |
| (공통) Safety fail | 최종 출력 보류, safety.md에 사유 기록 후 ask_user |

## 7. 구현 순서 (작업 목록)

### Step 0. 패턴 분기 골격 설계
- [ ] `schemas.py`에 `team_pattern`, `DelegationStep` 필드 반영
- [ ] `orchestrator.py`를 dispatcher 구조로 스캐폴딩 (두 flow 함수는 빈 껍데기로 우선 생성)
- [ ] Run Store 디렉토리 규칙에 `artifacts/candidates/`, `artifacts/chain/` 반영
- [ ] `docs/adr/0001-team-pattern-dispatch.md` 작성 — "왜 두 패턴을 통합하지 않고 분기했는가"를
      첫 ADR로 기록 (jikime/harness-lab 관행 차용, Section 12.3). 이후 스키마/복구전략/패턴이
      바뀔 때마다 새 ADR 번호로 추가

### Step 1. 스캐폴딩
- [ ] 리포지토리/디렉토리 구조 생성
- [ ] `run_store.py`: run_id 생성, 디렉토리 생성, JSON/MD 저장·로드 함수 (패턴 무관 공통)

### Step 2. Mock Provider 파이프라인 (Fan-out/Judge) — 완료
- [x] `providers/base.py` Provider 인터페이스 정의 (generate(prompt) -> Candidate)
- [x] `providers/mock.py`: 결정적 응답 3종 생성 (모델별 다른 강점 시뮬레이션)
- [x] `model_runner.py`: 3개 mock provider를 순차 호출, 실패 주입 테스트 포함
      (1회 재시도 후 복구, 재시도까지 실패 시 error 후보 기록하고 나머지는 계속 진행)

### Step 3. Mock Subagent 체인 (Hierarchical Delegation) — 완료
- [x] `subagent_runner.py`: 역할별 mock provider 호출 + 컨텍스트 격리 시뮬레이션(대용량 mock 출력을
      내부에서만 소화하고 요약된 Observation만 반환)
- [x] 2단계 체인 mock 시나리오 구성 (research mock → design_review mock)
- [x] 체인 중간 실패 주입 테스트 포함 (3단계 체인에서 2번째 스텝이 재시도까지 실패하면
      3번째 스텝은 아예 실행되지 않고 체인이 중단됨을 확인)

### Step 4. Planner — 완료
- [x] `planner.py`: task.json을 받아 task_type/risk_level/rubric 규칙 기반 산출
- [x] `team_pattern` 결정 규칙(Section 5 표) 반영 (router.classify_team_pattern에 위임,
      애매하면 fan_out_judge 기본값)
- [x] plan.md / plan.json 저장 (orchestrator._write_plan, Step 8과 함께 완성)

### Step 5. Router 훅 — 완료
- [x] `router.py`: 명백한 키워드 패턴(예: "리서치", "조사")은 Planner LLM 호출 없이 바로
      `team_pattern`을 결정하는 규칙 기반 사전 필터 (`classify_team_pattern`)
- [x] `router.check_fitness(task)`: 적합성 게이트 구현 (Section 12.1). 단순 사실 질문류는
      `FitnessCheck(passed=False, reason=...)` 반환
- [x] `model_runner.direct_call(task)`: 적합성 게이트 탈락 시 쓰는 단일 모델 mock 호출 경로

### Step 6. Judge / Synthesizer (Fan-out/Judge 전용) — 완료
- [x] `judge.py`: rubric 기반 점수화 로직 (규칙 기반 mock judge — rubric 키워드 매칭 +
      응답 길이 보조 지표)
- [x] judging.json 저장, winner/strategy(`adopt_winner`/`merge_top_candidates`) 결정
- [x] `synthesizer.py`: winner 후보 기반 + 상위 2개 후보 병합 템플릿, final.md 저장

### Step 7. Safety Evaluator (공통) — 완료
- [x] `safety.py`: 체크리스트(비밀정보 패턴, 프롬프트 인젝션 문구, 고위험 키워드) 규칙 기반 스캔
- [x] safety.md 저장 (두 패턴 + direct_call + partial 승격 경로 모두에서 동일하게 호출)

### Step 8. Orchestrator Dispatcher + CLI — 완료
- [x] `orchestrator.py`: Section 5의 dispatch 로직 완성 (fan_out_judge / hierarchical_delegation
      두 flow 모두 연결)
- [x] `orchestrator.py`: 적합성 게이트(Step 5) 통과 여부로 direct_call 분기, `plan.risk_level`
      "high" 승인 체크포인트 반영 (Section 12.1, 12.2) — `run()`이 pending 상태에서 멈추고,
      `resume(run_id, "approved"/"rejected", providers)`가 이어받는 2단계 구조로 구현
- [x] errors.json/metrics.json 기록 (두 패턴 공통 포맷, fitness_check.json/approval.json 포함).
      개별 후보/스텝이 재시도까지 실패하면 run 전체가 성공해도 errors.json에 남긴다
- [x] `cli.py`(`src/harness/cli.py`, `python -m harness.cli` 형태로 호출): `run --task`,
      `replay <run_id>`, `approve <run_id>`, `reject <run_id>` 진입점
- [x] `examples/task.{fan_out,delegation,high_risk,trivial}.json` 예시 4종 추가

### Step 9. 통합 테스트 (두 패턴 모두 + 보완 장치) — 완료
- [x] **fan_out_judge**: run_id 생성, input.json 저장, candidate 3개 생성(실패 케이스 1개 주입 포함),
      judging.json, final.md, metrics.json/errors.json 생성 확인
- [x] **hierarchical_delegation**: run_id 생성, input.json 저장, chain 2단계 실행(중간 실패 주입 포함),
      judging.json이 생성되지 않음을 확인(None), final.md, metrics.json/errors.json 생성 확인
- [x] 동일 task.json 재실행 시 두 패턴 모두 동일 구조로 재현되는지 확인
- [x] **적합성 게이트**: 단순 사실 질문 task로 실행 시 `fitness_check.json.passed=False`,
      direct_call만 실행되고 candidates/chain/judging/plan.md는 생성되지 않음을 확인 (Section 12.1)
- [x] **승인 체크포인트**: risk_level="high" task 실행 시 approval.json이 "pending"으로 생성되고,
      승인 전에는 candidate/chain이 실행되지 않음, reject 시 run이 "rejected"로 종료됨을 확인
      (Section 12.2)
- [x] **회귀 테스트**: partial로 승격되는 hierarchical_delegation 결과도 Safety 체크를 반드시
      거치는지 확인 (구현 중 발견한 버그의 재발 방지)

Phase 1 종료 리뷰에서 발견/수정한 버그 3건(Safety 누락, falsy-zero, 콘솔 인코딩)의
세부 내용은 `docs/03_진행상황/harness-progress-detail-ko.md` 참고.

## 8. Phase 로드맵

| Phase | 내용 |
| --- | --- |
| Phase 1 | **완료.** Reproducible Run: 두 패턴(fan_out_judge, hierarchical_delegation) 모두 mock으로 검증. CLI(`run`/`replay`/`approve`/`reject`)와 적합성 게이트/승인 체크포인트까지 포함해서 49개 테스트로 검증됨 |
| Phase 2 | **완료.** Eval Harness: evals/graders.py, evals/runner.py로 deterministic grader + pass@k 측정 (패턴 무관 run 단위 평가). pass_rate(pass@1 근사)/pass_at_k/pass_pow_k와 cost/latency per success까지 9개 테스트로 검증됨 |
| Phase 3 | **완료.** Model Routing + Provider 인증 모드: `cli_subscription_provider.py`(claude/codex CLI, 구독 로그인)와 `api_provider.py`(Gemini REST API, API 키) 둘 다 실제로 검증됨(24개 테스트 + 수동 e2e). Gemini는 개인 Google 계정의 Code Assist CLI 구독 로그인이 Google 정책으로 막혀 있어(`IneligibleTierError`) `cli_subscription_provider.py`가 아니라 `api_provider.py`의 api_key 모드로 지원 |
| Phase 4 | **완료.** Safety and Policy Gate: Safety 실패 시 즉시 차단 대신 사람 검토 대기(pending)로 승격, 승인 체크포인트(`Approval`)를 재사용해 release(오탐 공개)/block(계속 보류) 결정. `orchestrator.resolve_safety_review()`/`list_safety_review_queue()` + `cli.py safety-queue/safety-approve/safety-reject`. ADR 0002 참고. 7개 테스트로 검증 |
| Phase 5 | **완료.** Harness Evolution: 정기적 정리(pruning)로 죽은 필드 2개 제거, 세 번째 팀 패턴(Debate/Consensus)은 근거(실패 로그) 부재로 도입 보류 + 재검토 트리거 문서화(ADR 0003), 실패 로그 기반 개선은 규칙 자동 수정 대신 `failure_analysis.py`(+ `cli.py analyze-failures`)로 여러 run의 실패 패턴을 집계하는 인프라만 구축. 6개 테스트로 검증 |
| Phase 6 | **완료.** UI / Dashboard: `dashboard.py`(+ `cli.py dashboard`)가 저장된 run 산출물(plan.json/metrics.json/errors.json/safety_review.json/approval.json)만으로 team_pattern별 성공/경고/실패율과 평균 latency/cost를 정적 HTML로 렌더링. eval pass@k는 미포함(EvalReport가 디스크에 저장된 적 없음, "승률"은 "성공률"로 재정의 — 패턴끼리 경쟁하는 구조가 아님). 13개 테스트로 검증 |

## 9. 리스크 및 유의사항

- Agent Soup 방지: 패턴이 2개로 늘었지만 역할은 여전히 Planner/Runner(or Subagent Runner)/Judge/Synthesizer/Safety 5개 이내로 제한
- 패턴 간 목적 혼동 방지: fan_out_judge는 "품질 비교", hierarchical_delegation은 "역할 분업/비용 절약"이라는 목적을 코드 주석과 문서에 명시해 두 패턴이 서로의 역할을 침범하지 않도록 함
- Judge Overtrust 방지: Judge rationale도 반드시 judging.json에 저장 (fan_out_judge에만 해당)
- No Artifact 방지: 패턴별 필수 파일 저장 실패 시 run 자체를 실패 처리 (fan_out_judge: input/plan/candidates/judging/final/metrics, hierarchical_delegation: input/plan/chain/final/metrics)
- Cost Blindness 방지: mock 단계에서도 latency_ms/estimated_cost_usd 필드는 항상 채워서 두 패턴 간 비용 차이를 미리 비교 가능한 구조로 검증
- 오분류 리스크: Planner/Router의 team_pattern 판단이 틀리면 비효율적인 패턴이 선택될 수 있음 → 애매한 경우 기본값 + ask_user로 완화
- 적합성 게이트 오탈락 리스크: `check_fitness`가 실제로는 비교/분업이 필요한 작업을 direct_call로 잘못 분류하면 품질 저하로 이어짐 → 애매하면 `passed=True`(과소적용보다 과대적용) 기본값으로 완화 (Section 12.1)
- 승인 체크포인트 병목 리스크: risk_level="high" 남발 시 매번 사람 승인을 기다리게 되어 hierarchical_delegation/cli_subscription의 순차 처리 이점이 죽음 → risk_level 판정 기준을 Planner 규칙에 명시하고 남용 여부를 Phase 5 정리 대상에 포함 (Section 12.2, 12.4)
- 실제 API/CLI 연동은 두 패턴 모두 Step 9 통합 테스트 통과 후에만 진행
- 구독 한도 초과 방지: cli_subscription_provider는 5시간/주간 롤링 한도가 있으므로, auth_mode="cli_subscription"일 때는
  fan_out_judge의 num_candidates를 낮추거나 hierarchical_delegation을 기본 패턴으로 유도
- 인증 모드 혼선 방지: claude/codex/gemini CLI는 API 키 환경변수(ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY 등)가
  설정돼 있으면 구독을 무시하고 조용히 API 과금으로 전환되므로, cli_subscription_provider 실행 시 해당 환경변수를
  명시적으로 unset하거나 격리된 서브프로세스 환경에서 실행
- Windows에서 CLI subprocess 호출 시 주의: npm이 설치하는 claude/codex CLI는 `.cmd` 배치 파일이라, Python
  `subprocess.run(["claude", ...])`처럼 이름만 주면 `shell=False`(기본값)에서 `FileNotFoundError`가 난다.
  `shutil.which()`로 `.cmd` 확장자까지 포함한 실제 경로를 미리 찾아서 넘기면 `shell=True` 없이(=셸 인젝션
  위험 없이) 정상 동작한다 — `cli_subscription_provider.py`의 `_resolve_executable()` 참고. 발견 경위는
  `docs/03_진행상황/harness-progress-detail-ko.md`(Phase 3) 참고

## 10. Provider 인증 모드 (API 키 vs 구독 CLI)

API 연동은 두 가지 인증 모드를 모두 지원하도록 설계한다. 구독(ChatGPT Plus, Claude Pro/Max, Gemini 구독)만
쓰는 유저를 배제하지 않기 위함이다.

| 항목 | api_key 모드 | cli_subscription 모드 |
| --- | --- | --- |
| 호출 방식 | REST API 직접 호출 | claude / codex / gemini CLI subprocess 호출 |
| 과금 방식 | 토큰당 pay-as-you-go | 월 정액 구독, 토큰 예산 롤링 리필 |
| 사용량 상한 | 사실상 무제한(예산 내) | 5시간/주간 롤링 한도 |
| 비용 지표 | `cost_usd` | `quota_usage_pct` (추정치) |
| 병렬 호출 적합성 | 좋음 (fan_out_judge에 적합) | 제한적 (hierarchical_delegation처럼 순차 호출 선호) |

**Phase 3 구현 후 확정**: `cli_subscription_provider.py`는 claude/codex CLI만
지원한다. Gemini 개인 계정은 Code Assist CLI 구독 로그인이 Google 정책으로 막혀 있어
(`IneligibleTierError`, Antigravity로의 유도는 headless 모드가 없어 부적합) 대신
`api_provider.py`의 `api_key` 모드로 지원한다 — REST(`generateContent`) 직접 호출,
API 키는 URL 쿼리스트링이 아니라 `x-goog-api-key` 헤더로 전송(쿼리스트링은 네트워크
예외 메시지에 URL이 노출돼 키 유출 위험). `cost_usd`는 `candidatesTokenCount`(출력
토큰)만 반영한 근사치이며 입력 토큰 비용은 미포함(정확한 청구액은 서비스 콘솔 확인
필요). 조사 경위는 `docs/03_진행상황/harness-progress-detail-ko.md`(Phase 3) 참고.

`ProviderConfig.auth_mode`로 어떤 모드를 쓸지 task/유저 단위로 선택 가능하게 하고, Planner가
`team_pattern`을 정할 때 auth_mode도 함께 참고해서 cli_subscription 모드에서는 병렬 후보 수를
자동으로 낮추는 규칙을 둔다.

## 11. 완료 기준 (Definition of Done for MVP)

**모든 항목 검증 완료 (2026-07-07).** CLI로 직접 실행해서 확인했고(수동 검증), 아래 각
항목에 대응하는 자동화 테스트는 `tests/test_step9_integration.py`에 있다.

- `python -m harness.cli run --task examples/task.fan_out.json` 실행 시 `_workspace/runs/<run_id>/`
  하위에 fan_out_judge 패턴의 필수 파일(input, plan, candidates 3개, judging, final, metrics, errors)이
  모두 생성됨
- `python -m harness.cli run --task examples/task.delegation.json` 실행 시 hierarchical_delegation
  패턴의 필수 파일(input, plan, chain 2개, final, metrics, errors)이 모두 생성되고 judging.json은
  생성되지 않음
- 두 패턴 모두 동일 task.json으로 재실행 시 동일한 구조의 run이 재생성됨 (재현성)
- 각 패턴에서 후보/스텝 1개를 강제 실패시켰을 때 errors.json에 기록되고 정의된 복구 전략대로
  동작함 (fan_out_judge: 나머지 후보로 진행, hierarchical_delegation: 마지막 성공 스텝을 partial
  final로 승격)
- 단순 사실 질문 task 실행 시 `fitness_check.json.passed=False`로 판정되어 direct_call만
  실행되고, 패턴 분기(candidates/chain/judging)는 생성되지 않음 (Section 12.1)
- risk_level="high" task 실행 시 approval.json이 "pending"으로 생성되어 승인 전까지
  candidate/chain 실행이 차단되고, 승인/반려 각각에 대해 정의된 대로 진행/종료됨 (Section 12.2)
- 첫 ADR(`docs/adr/0001-team-pattern-dispatch.md`)이 존재하고, 이후 구조 결정 변경 시 새 ADR이
  추가되는 관행이 Step 0 체크리스트에 반영돼 있음 (Section 12.3)

## 12. jikime/harness-lab에서 가져온 보완 장치

`jikime/harness-lab`은 실행 런타임이 아니라 "하네스 사고방식을 어떻게 실습·체화할 것인가"를
다루는 교육용 레포다(자세한 내용은 `docs/01_개념설명/harness-repo-summary-technical-ko.md`
Section 4 참고). 여기서 가져온 네 가지 보완 장치를 이 플랜에 실제로 반영했다.

### 12.1 적합성 게이트 (Fitness Gate)

모든 작업에 멀티 LLM 하네스를 걸치면 간단한 질문에도 비교/합성 비용을 강제하게 된다
(affaan-m/ECC의 cost per success 원칙과 충돌). `router.py`에 `check_fitness(task)`를 추가해서
Planner보다도 먼저, 더 저렴하게 "이 작업이 하네스화할 가치가 있는가"를 판정한다. 탈락하면
`team_pattern` 분기 자체를 건너뛰고 `model_runner.direct_call()`로 단일 모델 호출만 수행한다.
Safety 체크는 direct_call 경로에서도 절대 생략하지 않는다.

### 12.2 사람 승인 체크포인트 (Human Approval Checkpoint)

"청사진 제시 → 사용자 승인 → 실행"이라는 jikime/harness-lab의 핵심 워크플로우를, 이미 스키마에
있던 `Plan.risk_level`과 연결했다. `risk_level == "high"`인 Plan은 candidate 생성/체인 실행 전에
`approval.json`을 "pending"으로 써서 사람이 승인/반려하기 전까지 실행을 막는다. 낮은/중간
위험도 작업까지 매번 승인을 받게 하면 hierarchical_delegation/cli_subscription의 순차 처리
이점이 사라지므로, 이 체크포인트는 high risk_level에만 적용한다.

### 12.3 ADR (Architecture Decision Record)

스키마·복구 전략·팀 패턴처럼 구조에 영향을 주는 결정을 `docs/adr/NNNN-제목.md` 형식으로
남긴다. 첫 ADR은 Step 0에서 "왜 두 팀 패턴을 통합하지 않고 분기했는가"를 기록하고, 이후
새로운 결정(예: 세 번째 팀 패턴 도입, 복구 전략 변경)이 생길 때마다 번호를 이어서 추가한다.
목적은 "왜 이렇게 설계했는지"를 나중에 합류하는 사람이 대화 맥락 없이도 파악하게 하는 것이다.

### 12.4 정기적 정리 (Pruning)

"효율은 에이전트 수가 아니라 구조화·검증·사람 승인에서 나온다"는 jikime/harness-lab의
원칙에 따라, Phase 5(Harness Evolution)에 정기적 정리 활동을 명시했다. 실사용률이 낮은
필드·역할·이미 폐기된 ADR 결정을 주기적으로 점검해서 제거하고, risk_level="high" 남용으로
승인 체크포인트가 병목이 되고 있지는 않은지도 이 시점에 함께 재검토한다.
