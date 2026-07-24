# 4개 레포 분석 요약 (기술자용)

대상 독자: 소프트웨어 개발 경험이 있고, LLM/에이전트 관련 개념에 어느 정도 익숙한 사람.
분석 대상: `revfactory/harness`, `affaan-m/ECC`, `gaebalai/claude-code-orchestrator`,
`jikime/harness-lab`

## 0. 전제: 하네스란

LLM 자체는 추론 엔진일 뿐이고, 하네스는 그걸 감싸는 실행/조율/평가/기록/복구 시스템이다.
"이 모델이 똑똑한가"가 아니라 "어떤 실행 구조·도구·평가 기준·복구 전략으로 감쌌을 때
더 안정적으로 좋은 결과를 내는가"를 다루는 레이어다.

## 1. revfactory/harness

**URL**: <https://github.com/revfactory/harness/tree/main>

**정체성**: 하네스를 실행하는 런타임이 아니라, 도메인 설명을 입력받아 그 도메인에 맞는
에이전트 팀·스킬·오케스트레이터를 설계·생성해주는 메타 스킬/팀 아키텍처 팩토리.

**핵심 개념**:

```text
Agent        = 누가 하는가 (역할을 맡은 실행 주체)
Skill        = 어떻게 하는가 (절차적 지식)
Orchestrator = 누가 언제 어떤 순서로 협업하는가 (흐름 제어)
```

**팀 아키텍처 패턴 (6종)**:

| 패턴 | 구조 | 적합한 상황 |
| --- | --- | --- |
| Pipeline | 분석→설계→구현→검증 순차 | 앞 단계 결과가 다음 단계 입력이 되는 작업 |
| Fan-out/Fan-in | 1개 입력 → N개 병렬 처리 → 통합 | 멀티 LLM 후보 비교 (우리 시스템의 핵심 패턴) |
| Expert Pool | Router가 필요한 전문가만 선택 | 작업 성격별 분기가 필요할 때 |
| Producer-Reviewer | 생성자 ↔ 검토자 분리 | 품질 검토가 중요한 생성 작업 |
| Supervisor | 중앙 감독자가 동적으로 작업 배분 | 런타임에 분배를 바꿔야 하는 큰 작업 |
| Hierarchical Delegation | 상위 에이전트 → 하위 에이전트 재위임 | 조직 구조형 위임 (깊어지면 컨텍스트 손실 위험) |

**가져온 것**: Agent/Skill/Orchestrator 분리, Fan-out/Fan-in, Producer-Reviewer,
Supervisor, progressive disclosure, 파일 기반 artifact 관리.

## 2. affaan-m/ECC

**URL**: <https://github.com/affaan-m/ECC>

**정체성**: 하네스 생성 도구가 아니라, 이미 만든 에이전트 하네스를 운영·평가·최적화하는
레이어. eval, model routing, action/observation contract, safety/recovery, 팀
오케스트레이션을 다룬다.

**agent-harness-construction 스킬의 품질 4축**:

1. **Action Space Quality**: 에이전트가 쓸 수 있는 행동이 명확히 타입화돼 있는가
   (`do_anything(input)`이 아니라 `run_model(model_id, prompt, temperature)`처럼).
2. **Observation Quality**: 도구 실행 결과가 다음 행동을 결정할 만큼 구조적인가.
   권장 스키마: `{status, summary, artifacts, next_actions}`.
3. **Recovery Quality**: 실패 시 행동이 정해져 있는가 (retry → error 기록 →
   min_candidates 이상이면 진행, 부족하면 ask_user).
4. **Context Budget Quality**: 컨텍스트는 제한 자원이므로 상시 필요한 규칙 + 필요시만
   읽는 스킬 + 상황별 reference로 나눈다 (progressive disclosure).

**eval-harness 스킬 핵심 지표**:

| 지표 | 의미 |
| --- | --- |
| pass@1 | 첫 실행 성공 확률 |
| pass@3 | 3번 중 1번 이상 성공 확률 |
| pass^3 | 3번 모두 성공 확률 |
| cost per success | 성공 1회당 비용 |
| latency per success | 성공 1회당 지연 |

deterministic grader를 우선 사용하고, 주관적 품질 평가만 model judge/human review로
보완한다는 원칙.

**가져온 것**: eval-driven development, pass@k, cost/latency 추적, action/observation
contract, 실패 복구 계약, model routing, safety evaluator, deterministic grader 우선
원칙.

## 3. gaebalai/claude-code-orchestrator

**URL**: <https://github.com/gaebalai/claude-code-orchestrator>

**정체성**: 실행 런타임이자 실전 dev workflow 구성 — Claude Code를 메인 오케스트레이터로
두고, Codex CLI(설계/추론/디버깅)와 Gemini CLI(리서치/멀티모달)를 서브에이전트를 경유해
호출하는 구조.

```text
Claude Code (Orchestrator)
  └─ Subagent (general-purpose)
       → 독립 컨텍스트 보유, 결과 요약 후 메인으로만 반환
       ├─ Codex CLI  (설계/추론/디버깅)
       └─ Gemini CLI (리서치/멀티모달)
```

**핵심 설계 원칙 — 컨텍스트 경제**: 메인 오케스트레이터의 컨텍스트가 최우선 보호
자원. 출력이 클 것으로 예상되는 작업은 반드시 서브에이전트를 경유시켜 요약된 결과만
반환받는다. 짧은 Q&A만 메인이 직접 처리.

**구조적 특징**: 스킬을 슬래시 커맨드로 노출(`/startproject`, `/plan`, `/tdd`,
`/codex-system`, `/gemini-system`), 훅(`agent-router.py`, `check-codex-before-write.py`)이
사용자 입력/파일 저장 시점에 자동으로 라우팅을 제안, 모든 CLI I/O를
`logs/cli-tools.jsonl`에 기록.

**중요한 차이점**: Judge, Synthesizer, Safety Evaluator, Eval Harness 개념이 없다.
병렬 후보 비교가 목적이 아니라 작업 성격별 순차 위임(Expert Pool + Hierarchical
Delegation 조합)이 목적이기 때문.

**가져온 것**: Hierarchical Delegation + Expert Pool 조합의 실전 레퍼런스, 서브에이전트
컨텍스트 격리 원칙, 구독 로그인 기반 CLI 호출 방식(API 키 없이 Pro/Max/Plus 구독으로
동작), 훅 기반 저비용 사전 라우팅.

## 4. jikime/harness-lab

**URL**: <https://github.com/jikime/harness-lab>

**정체성**: 실행 런타임이나 운영 레이어가 아니라, "하네스 엔지니어링"이라는 사고방식
자체를 Claude Code와 Codex CLI 양쪽에서 실습하도록 만든 교육용 스킬 프로젝트
(`claude/`, `codex/` 폴더로 환경 분리, `/harness-lab` / `$harness-lab`으로 호출).
연구원·편집자·PM·분석가·채용담당·운영리더·CFO 등 7개 교육용 Subagent 예시를 포함한다.

**핵심 워크플로우**: 청사진 제시 → 사용자 승인 → 실행 가능한 하네스 구성 → 산출물
계약(입력 요약, 조사노트, 초안, 검토표, 최종결과)으로 추적 → 자연어 요청을
Orchestrator Skill이 라우팅.

**설계 철학 — 우리 프로젝트와 맞닿는 지점**:

- "효율은 에이전트 수가 아니라 구조화·검증·사람 승인에서 나온다"는 원칙은
  Section 9의 Agent Soup 방지(5개 역할 제한) 결정과 같은 방향이다.
- 생성과 평가를 분리하고 외부 신호(테스트·체크리스트·사람 승인)로 검증한다는 점은
  우리 시스템의 Judge/Safety 분리와 대응된다.
- 적합성 게이트(하네스화 가치를 미리 판정), 결함 주입 검증, 종료 계약(횟수 상한·수렴
  조건)이 있는 반복 루프 설계는 이후 플랜 Section 12(적합성 게이트/사람 승인
  체크포인트/ADR/정기적 정리)에 실제로 반영됐다.
- 구조 결정을 ADR(Architecture Decision Record)로 남기는 습관은 우리도 차용할 만한
  실천 방법이다.

**가져올 것(향후 검토)**: 적합성 게이트, 결함 주입 검증, ADR 기반 구조 결정 기록,
"불필요한 조각은 정기적으로 제거" 원칙.

## 5. 네 레포의 관계

```text
Team Architecture Layer     Operational Harness Layer    Execution Reference          Practice/Teaching Layer
revfactory/harness      +   affaan-m/ECC             +    gaebalai/claude-code-        +   jikime/harness-lab
= 팀을 어떻게              = 어떻게 운영·평가·               orchestrator                  = 하네스 사고방식을
  설계·생성할까               개선할까                    = 실제로 어떻게 여러 CLI를          어떻게 실습·체화할까
                                                          오케스트레이션 하는가
```

경쟁 관계가 아니라 서로 다른 레이어다. 우리 시스템은 앞의 세 레이어를 직접 결합하고,
네 번째 레이어는 설계 철학 검증 및 향후 안전장치 설계의 참고 자료로 쓴다.

| 우리 시스템 요소 | 주 출처 레포 |
| --- | --- |
| Agent/Skill/Orchestrator 분리, Fan-out/Judge 패턴 | revfactory/harness |
| Run Store, eval, cost/latency, action/observation contract, 복구 전략 | affaan-m/ECC |
| Hierarchical Delegation 패턴, 서브에이전트 컨텍스트 격리, cli_subscription provider | gaebalai/claude-code-orchestrator |
| 적합성 게이트·결함 주입 검증·ADR 기반 구조 기록 (향후 검토) | jikime/harness-lab |

## 6. 결론

`revfactory/harness`의 팀 아키텍처 패턴 + `affaan-m/ECC`의 평가/운영/복구 레이어 +
`gaebalai/claude-code-orchestrator`의 실전 위임 구조를 결합해서, `team_pattern`으로
분기하는 멀티 LLM 실행-평가 하네스를 설계했다. `jikime/harness-lab`은 이 설계 철학을
검증하고, 적합성 게이트·결함 주입 검증 같은 개념을 향후 안전장치 설계에 참고하는
용도로 추가했다. 자세한 스키마와 구현 순서는 `harness-implementation-plan-ko.md` 참고.
