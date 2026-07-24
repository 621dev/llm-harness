# ADR 0004: Judge를 규칙 기반에서 단일 실제 LLM 판단으로 승격한다 (Debate/Consensus는 계속 보류)

- 상태: 확정
- 관련 플랜: `harness-implementation-plan-ko.md` Section 7 Step 6(Judge/Synthesizer),
  ADR 0001(팀 패턴 분기), ADR 0003(Debate/Consensus 보류)

## 배경

`judge.py`는 원래부터 "진짜 LLM Judge가 아니라 rubric 기반 규칙 점수화"라고
docstring에 명시된 의도된 MVP 범위였다 — rubric 문구가 candidate.content에
리터럴로 등장하는지와 응답 길이로 점수를 매긴다.

2026-07-10에 `cli.py`의 기본 provider를 MockProvider에서 실제 Claude
CLI/Codex CLI/Gemini API로 배선한 뒤(별도 커밋, PR #9) `task.fan_out.json`을
실제로 돌려보니, 실제 LLM은 산문으로 답하기 때문에 rubric 키워드("구조 명확성"
등)가 리터럴로 거의 안 걸리고, 사실상 **응답 길이로만 승자가 갈리는 것을
실측으로 확인했다**(`run-fan-out-demo/judging.json`). fan_out_judge의 핵심
가치(여러 모델 답을 비교해서 제일 좋은 걸 고른다)가 지금 상태로는 실질적으로
작동하지 않는다.

이 문제를 계기로 원래 4개 참고 레포(`revfactory/harness`, `affaan-m/ECC`,
`gaebalai/claude-code-orchestrator`, `jikime/harness-lab`)를 "Judge 품질"이라는
구체적 질문으로 재분석했다. 결과:

- `revfactory/harness`, `gaebalai/claude-code-orchestrator`: 순수 프롬프트
  템플릿/오케스트레이션 스캐폴딩이라 Judge나 비용 인식 로직 자체가 없음.
  원래의 얕은 분석이 이미 충분했다는 게 재확인됨.
- `affaan-m/ECC`: 자체 grader(`evaluate.py`)도 우리와 동일한 키워드 휴리스틱
  버그를 갖고 있고, docstring에 "production엔 LLM judge를 붙이라"고 스스로
  인정하고 있음 — 그대로 베낄 게 못 됨. 다만 `orch-review.workflow.js`의
  **적대적 검증자**(1차 판정을 2차 독립 모델 호출이 confidence-gated로
  반증 시도, 실패하면 fail-closed) 패턴은 참고할 만함.
- `jikime/harness-lab`: 가장 구체적인 처방을 줌. 우리 Judge를 "죽은
  검토"(dead review — 검사가 무뎌서 뭘 넣어도 통과하는 상태)로 정확히
  진단하는 fault-injection 개념, 그리고 (1) 결정론적 체크와 LLM 판단의 분리,
  (2) reject-first 프롬프트(증거 기반으로 결함을 찾게 강제, "문제 없음"을
  기본값으로 주지 않음), (3) blind A/B(모델명 대신 익명 레이블 + 순서
  무작위화로 verbosity/position/identity bias 완화), (4) Self-Consistency
  (동일 판단을 N회 반복 후 다수결)가 Debate/Consensus보다 훨씬 싼 중간
  단계라는 것을 제시. **"Debate/Consensus로 바로 가지 말라"는 판단도 명확히
  뒷받침됨** — Nx 비용에 "핵심 판정에만 제한적으로" 쓰라는 원칙과, 이미
  ADR 0003이 강조한 Agent Soup 방지/비용 원칙이 일치.

ADR 0003의 재검토 트리거 2번("실제 LLM Judge로 교체된 이후... 교차 검증이 안
되는 실패 패턴이 확인되는 경우")과 정확히 같지는 않다 — 우리가 실측한 건 그
이전 단계, 즉 **Judge가 여전히 규칙 기반인 채로 실제 LLM 후보에 대해 이미
무의미한 판정을 내리고 있다는 것**이다. 그래도 "Judge에 실제 근거가 필요하다"는
같은 방향의 신호로 보고 이 ADR을 쓴다. ADR 0003이 예고했던 것과 달리, 이
ADR은 Debate/Consensus를 "도입한다"가 아니라 **Judge 자체를 먼저 실제 LLM
판단으로 고치는 결정**이다 — 트리거는 촉발됐지만 그 대응이 곧바로
Debate/Consensus일 필요는 없다는 걸 이번 재분석에서 확인했기 때문이다.

## 결정

1. `judge.py`의 `evaluate()`를 규칙 기반 키워드/길이 채점에서 "결정론적 사전
   체크 + 단일 실제 LLM 판단 호출"로 승격한다.
   - 결정론적 체크(빈 응답, 명백한 에러 문자열 등)는 여전히 LLM 호출 없이
     걸러낸다 — model_runner가 이미 `status="error"`로 분리한 candidate는
     애초에 채점 대상에서 제외되므로 이 부분은 큰 변경이 아니다.
   - 남은 성공 candidate들에 대해 **1회의 실제 LLM 호출**로 판단을 받는다
     (다회차 아님).
2. 판단 호출은 다음 두 가지 bias 완화 장치를 적용한다.
   - **Reject-first 프레이밍**: "문제가 없으면 PASS"가 아니라 "각 후보의
     결함을 근거와 함께 찾아라"를 기본 지시로 준다.
   - **Blind 익명화**: judge 프롬프트에는 실제 model_id 대신 A/B/C 같은
     레이블을 무작위 순서로 부여한다(길이 자체를 지우진 않되, "길이로
     판단하지 말라"는 명시적 지시를 추가).
3. Debate/Consensus(다회차 상호 비판)는 계속 도입하지 않는다(ADR 0003의
   핵심 결정 유지). 대신 다음 단계로의 격상 조건을 아래처럼 구체화한다.
4. Judge 호출에 쓰는 모델은 비용/속도를 고려해 가벼운 모델을 기본값으로
   한다(예: Gemini Flash 또는 Claude Haiku) — 후보 생성에 쓴 모델과 겹치지
   않는 편이 self-preference bias 회피에도 유리하다.
5. fault-injection 회귀 테스트를 추가한다 — 의도적으로 결함 있는(또는 아주
   짧지만 정확한) 답을 후보 하나에 섞어 넣고, 개선된 Judge가 이걸 정확히
   잡아내는지(=길이가 아니라 내용으로 판단하는지) 확인하는 테스트.

## 재검토 트리거 (다음 단계로 격상할 조건)

1. **1차 검증**: 위 단일 LLM 판단이 fault-injection 회귀 테스트를 통과하는지
   확인한다. 통과하면 여기서 멈춘다 — 더 비싼 패턴으로 갈 근거가 없다.
2. **2차 (Self-Consistency)**: fault-injection 테스트를 통과하지 못하면(=
   여전히 죽은 검토), 같은 판단 프롬프트를 N=3회 반복해 다수결로 정하는
   Self-Consistency로 격상한다. Debate보다 명백히 싸다.
3. **3차 (Debate/Consensus, ADR 0005 별도 문서화)**: Self-Consistency로도
   잡히지 않는 실패가 실제 운영(evals 리포트/`analyze-failures` 집계)에서
   반복 관측되면, 그때 비로소 Debate/Consensus 도입을 새 ADR로 논의한다.
   이 ADR을 수정하지 않는다(ADR 0003과 동일한 원칙).

## 이유

- 4개 레포 재분석에서 유일하게 구체적 처방을 준 jikime/harness-lab의 진단과
  권고를 따른다 — "Judge가 죽은 검토인지 fault injection으로 확인하고,
  Debate 전에 먼저 단일 judge를 제대로 만들라."
- Self-Consistency는 Debate/Consensus보다 명백히 저렴하고, ADR 0003이 이미
  강조한 Agent Soup 방지/cost per success 원칙(ADR 0001에서 인용한
  affaan-m/ECC 원칙)과 일치한다 — 무조건 가장 비싼 패턴으로 바로 가지 않고
  단계적으로 격상한다.
- ECC의 적대적 검증자 패턴은 참고 가치는 있지만, 지금 단계에서 바로 도입하면
  과설계 위험이 있다 — fault-injection 테스트로 1차 개선(reject-first + blind
  A/B)이 부족하다는 게 실제로 확인된 뒤에 고려하는 게 Section 12.4 정기적
  정리 원칙과 같은 절제다.

## 영향 (예상 — 구현 단계에서 확정)

- `judge.py`: `evaluate()`가 rubric 키워드 매칭 대신 judge용 provider를
  호출하도록 변경(model_runner와 유사하게 실패 시 재시도 계약을 따를지는
  구현 시 결정).
- `schemas.py`: `JudgingScore`에 reject-first 결과(결함 목록/근거)를 담을
  필드가 필요할 수 있음 — `strengths`/`weaknesses`를 재활용할지 새로 추가할지
  구현 단계에서 확정.
- `orchestrator.py`: fan_out_judge 플로우에서 judge 호출에 provider를
  넘기도록 변경.
- `cli.py`: judge 전용 provider(기본값) 추가.
- **비용/한도**: fan_out_judge run마다 실제 LLM 호출이 (후보 수) + 1(judge)로
  늘어난다 — 기존엔 후보 생성만 과금/구독을 소모했다.
- 테스트: `test_step6_judge_synthesizer.py`가 judge에 mock provider를
  주입하는 방식으로 갱신 필요(자동 테스트는 실제 LLM 미호출 원칙 유지). 새
  fault-injection 회귀 테스트 추가.
