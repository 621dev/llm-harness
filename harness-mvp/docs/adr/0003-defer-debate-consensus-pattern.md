# ADR 0003: 세 번째 팀 패턴(Debate/Consensus)은 지금 도입하지 않는다

- 상태: 확정
- 관련 플랜: `harness-implementation-plan-ko.md` Section 8 (Phase 5), Section 9,
  Section 12.4

## 배경

Phase 5 로드맵에 "세 번째 팀 패턴(예: Debate/Consensus) 검토"가 명시돼 있다.
Debate/Consensus는 `fan_out_judge`의 단발 병렬 생성 + 1회 심사 구조에, 모델들이
서로의 답을 보고 반박/수정하는 다회차(multi-round) 상호 비판 라운드를 추가하는
패턴이다. `revfactory/harness`의 6개 팀 아키텍처 패턴(Pipeline, Fan-out/Fan-in,
Expert Pool, Producer-Reviewer, Supervisor, Hierarchical Delegation)에도 없는
개념으로, Fan-out/Fan-in + Judge의 확장형에 가깝다.

## 결정

지금 시점에는 `team_pattern`에 세 번째 값을 추가하지 않는다. 대신 이 ADR에 "왜
지금 안 만드는지"와 "언제 재검토할지" 조건을 남긴다.

**재검토 트리거**(아래 중 하나라도 실제로 관측되면 재검토):

1. Phase 5의 "실패 로그 기반 프롬프트/스킬 개선" 작업 중, `judge.py`의 단발 심사가
   반복적으로 잘못된 winner를 선택했다는 근거가 로그/eval 리포트에 쌓이는 경우
2. 실제 LLM Judge(현재는 규칙 기반 mock)로 교체된 이후, 독립 병렬 후보만으로는
   교차 검증이 안 되는 실패 패턴(예: 여러 후보가 같은 방향으로 틀리는 correlated
   error)이 `evals/` 리포트에서 확인되는 경우
3. 사용자가 명시적으로 "다회차 비교가 필요한 고위험 작업"을 실제로 다루게 되는 경우

## 이유

- **Agent Soup 방지** (Section 9): 역할을 Planner/Runner(Subagent Runner
  포함)/Judge/Synthesizer/Safety 5개 이내로 제한하는 원칙이 있다. 패턴 자체를
  늘리는 것은 역할 수를 늘리는 것과 다른 문제지만, 같은 절제 원칙("필요한 경우에만
  해당 비용을 지불")이 적용된다.
- **비용**: 다회차 라운드는 fan_out_judge(단발 병렬 호출)보다 호출 수가 몇 배로
  늘어난다. `cli_subscription` 인증 모드는 이미 5시간/주간 롤링 한도 리스크가
  문서화돼 있어(Section 9), 가장 비용이 큰 패턴을 추가하면 이 리스크가 더 커진다.
- **근거 부재**: 로드맵의 "예: Debate/Consensus"는 예시 제안이었을 뿐, 이 프로젝트
  안에서 fan_out_judge의 단발 Judge가 실제로 오판했다는 실패 로그나 eval 실패
  사례가 아직 없다. 근거 없이 패턴부터 만들면 "구조화보다 에이전트 수를 늘려
  문제를 덮는" 것과 같은 실수가 된다(jikime/harness-lab 원칙, Section 12.4).
- Phase 5의 다른 항목인 "실패 로그 기반 프롬프트/스킬 개선"이 선행돼야 이 근거가
  쌓인다 — 순서상 그쪽을 먼저 진행하는 것이 합리적이다.

## 영향

- `schemas.py`: 변경 없음(`TeamPattern`은 여전히 `["fan_out_judge",
  "hierarchical_delegation"]` 2종)
- `orchestrator.py`: 변경 없음
- 문서: `docs/03_진행상황/harness-progress-checklist-ko.md`에 "검토 완료 → 보류
  결정"으로 기록. 재검토 트리거가 실제로 발생하면 새 ADR(0004)로 "도입한다"
  결정을 별도로 남긴다(이 ADR을 수정하지 않음).
