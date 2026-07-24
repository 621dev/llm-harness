# 구현 플랜 요약 (기술자용)

대상 독자: 개발 경험이 있는 사람. 전체 세부 사양은 `harness-implementation-plan-ko.md`
(Section 1~12)를 참고하고, 이 문서는 그걸 빠르게 훑어볼 수 있는 요약이다.

## 1. 목표

Planner가 작업 성격에 따라 `team_pattern`을 고르고, Orchestrator가 그 패턴에 맞는
flow를 실행하는 파일 기반 실행-평가 하네스의 MVP.

| 패턴 | 목적 | 흐름 |
| --- | --- | --- |
| `fan_out_judge` | 품질 비교 | 여러 모델 독립 후보 생성 → Judge 비교 → Synthesizer 합성 |
| `hierarchical_delegation` | 역할 분업/비용 절약 | 컨텍스트 격리 서브에이전트가 역할별 모델에 순차 위임 |

완전 통합안(모든 작업에 두 단계 강제)은 cost per success 관점에서 기각, 분기 방식으로 결정.

## 2. 아키텍처 개요

```text
src/harness/
  orchestrator.py     # team_pattern 분기 dispatcher
  planner.py           # team_pattern 결정 포함 Plan 생성
  router.py             # 저비용 사전 분류 훅 (선택)
  model_runner.py         # fan_out_judge: 독립 후보 생성
  subagent_runner.py       # hierarchical_delegation: 컨텍스트 격리 위임
  judge.py / synthesizer.py  # fan_out_judge 전용
  safety.py                    # 공통
  run_store.py                  # 공통
  schemas.py                     # 스키마 정의
src/providers/
  api_provider.py         # API 키 기반
  cli_subscription_provider.py  # 구독 CLI 로그인 기반 (claude/codex/gemini CLI)
src/evals/
  graders.py / runner.py  # deterministic grader + pass@k
```

Run Store: `_workspace/runs/<run_id>/{input.json, plan.md, artifacts/{candidates,chain}/,
judging.json(fan_out_judge 전용), final.md, safety.md, metrics.json, errors.json}`

## 3. 핵심 스키마

`TaskInput`, `Plan`(team_pattern, num_candidates, delegation_chain 포함),
`DelegationStep`, `ProviderConfig`(auth_mode), `Candidate`, `Judging`, `RunMetrics`
(estimated_cost_usd/quota_usage_pct), `Observation`(status/summary/artifacts/next_actions).
모든 컴포넌트는 이 스키마로만 통신한다 (action/observation contract).

## 4. 복구 전략 요지

| 상황 | 전략 |
| --- | --- |
| 모델 호출 실패 | 1회 재시도 → error 기록 |
| fan_out_judge, min_candidates 미만 성공 | ask_user |
| hierarchical_delegation 체인 중단 | 마지막 성공 스텝을 partial final로 승격 |
| Safety fail | 출력 보류, ask_user |

## 5. 구현 순서 (Step 0~9)

Step 0~1(패턴 분기 골격 + Run Store) → Step 2(mock model_runner) → Step 3(mock
subagent_runner) → Step 4(planner) → Step 5(router, 선택) → Step 6(judge/synthesizer)
→ Step 7(safety) → Step 8(orchestrator 완성 + cli) → Step 9(두 패턴 통합 테스트).

## 6. Provider 인증 모드

`auth_mode: "api_key" | "cli_subscription"`. 구독 CLI는 5시간/주간 롤링 한도가 있어
`fan_out_judge`보다 `hierarchical_delegation`에 적합. API 키 환경변수가 설정돼 있으면
구독을 무시하고 API 과금으로 전환되는 함정이 있어 실행 환경 분리가 필요.

## 7. Phase 로드맵

**Phase 1(Reproducible Run) — 완료.** Phase 2(Eval Harness, pass@k) → Phase 3(Model
Routing + 실제 api/cli provider 연동) → Phase 4(Safety/Policy Gate) → Phase 5(Harness
Evolution, 세 번째 패턴 검토) → Phase 6(UI/Dashboard)

## 8. 현재 구현 상태

**Phase 1(Step 0~9) 전체 완료.** `schemas.py`는 pydantic 기반으로 전환 완료.
`planner.py`/`router.py`/`judge.py`/`synthesizer.py`/`safety.py`가 전부 구현됐고
`orchestrator.py`도 완성돼 `cli.py`(`run`/`replay`/`approve`/`reject`)로 실제 실행
가능하다. 49개 테스트 전부 통과. 최신 상태는
`../03_진행상황/harness-progress-checklist-ko.md`(요약)와
`harness-progress-detail-ko.md`(세부), `../../harness-mvp/README.md`를 참고할 것 —
이 섹션은 요약이라 자세한 숫자가 바뀔 때마다 갱신하지 않는다.

## 9. 보완 장치 (jikime/harness-lab, Section 12)

`jikime/harness-lab`(교육용 하네스 실습 레포)에서 4가지를 차용해 플랜에 반영했다.

| 장치 | 내용 | 관련 파일/필드 |
| --- | --- | --- |
| 적합성 게이트 | Planner보다 먼저 "하네스화할 가치가 있는가" 판정, 탈락 시 direct_call | `router.check_fitness`, `FitnessCheck`, `fitness_check.json` |
| 사람 승인 체크포인트 | `risk_level="high"` Plan은 실행 전 사람 승인 대기 | `Approval`, `approval.json`, `cli.py approve/reject` |
| ADR | 구조 결정(스키마/복구전략/패턴 변경)을 번호 매겨 기록 | `docs/adr/0001-team-pattern-dispatch.md`부터 |
| 정기적 정리 | Phase 5에서 저사용 필드/역할/폐기 ADR·과도한 승인 병목 재검토 | Phase 5 로드맵 항목 |

## 10. DoD (완료 기준)

두 패턴 모두: 필수 파일 전부 생성, 동일 task 재실행 시 재현, 강제 실패 주입 시
정의된 복구 전략대로 동작. 적합성 게이트 탈락 시 direct_call만 실행되고 패턴 분기
아티팩트는 생성되지 않음. risk_level="high"는 approval.json이 "pending"으로 먼저
생성됨. 상세 체크리스트는 원본 플랜 Section 11 참고.
