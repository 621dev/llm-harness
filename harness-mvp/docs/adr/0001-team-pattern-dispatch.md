# ADR 0001: 팀 패턴을 통합하지 않고 분기한다

- 상태: 확정
- 관련 플랜: `harness-implementation-plan-ko.md` Section 1, 5

## 배경

`fan_out_judge`(여러 모델 독립 후보 생성 → Judge 비교 → Synthesizer 합성)와
`hierarchical_delegation`(컨텍스트 격리 서브에이전트가 역할별로 순차 위임)은
목적이 다르다. 모든 작업에 두 단계를 강제하는 완전 통합안도 검토했다.

## 결정

두 패턴을 하나의 고정 파이프라인으로 합치지 않고, `Plan.team_pattern` 필드로
`Orchestrator`가 분기하는 구조로 만든다. `fan_out_judge`에서만 Judge/Synthesizer가
실행되고, `hierarchical_delegation`에는 비교할 병렬 후보가 없으므로 Judge 자체가 없다.

## 이유

완전 통합안은 간단한 일에도 항상 리서치 → 비교 → 심사 단계를 다 거치게 만들어
`affaan-m/ECC`가 강조하는 cost per success 관점에서 비효율적이다. 두 패턴은
필요한 경우에만 해당 비용을 지불하도록 분기하는 편이 낫다.

## 영향

- `schemas.py`: `Plan.team_pattern: Literal["fan_out_judge", "hierarchical_delegation"]`
- `orchestrator.py`: `team_pattern` 값으로 flow를 선택하는 dispatcher 구조
  (`_FLOW_DISPATCH`)
- Run Store: `artifacts/candidates/`(fan_out_judge 전용), `artifacts/chain/`
  (hierarchical_delegation 전용), `judging.json`은 fan_out_judge에서만 생성
