# ADR 0006: 세 번째 팀 패턴 `iterative_refinement`(생성-평가 반복 루프)를 도입한다

- 상태: 확정 (2026-07-27)
- 관련: ADR 0003(세 번째 팀 패턴 보류 — 그 ADR의 "도입 시 새 번호 ADR로 남긴다"
  지침에 따라 작성), ADR 0004(Judge 실제 LLM 승격 — evaluator가 그 인프라 재사용)

## 배경

사용자의 장기 목표가 "자율 에이전트를 감싸는 하네스"로 확정됐다(2026-07-27).
현재 두 패턴은 모두 `Provider.generate(prompt)` 단발 완성 호출의 조합일 뿐,
"이전 결과를 보고 판단해 다시 시도하는" 반복 구조가 없다 — 진행상황 문서의
"다음 작업"에도 "반복 피드백 루프 검토"(Anthropic 방식 Planner→Generator→
Evaluator 반복)가 남아 있었다.

방향을 정하는 과정에서 참고 레포 `revfactory/harness`의 v2 업데이트(PR #51,
2026-07-20~24)를 분석했다. v2는 실행 모드를 "제어 흐름의 결정성" 기준 3가지로
재편했는데, 우리의 fan_out_judge/hierarchical_delegation은 모드 A(결정적
워크플로우)/모드 C(단발 위임)에 대응하고, **모드 B("생성자↔검증자 반복 루프",
컨텍스트 유지)에 대응하는 것이 없다**는 걸 확인했다. 처음 검토했던 "Gemini
tool-calling으로 파일 쓰기" 설계(경로 B)는 이 분석 후 폐기 — 진짜 빠진 조각은
도구 호출이 아니라 반복 개선 루프였다.

## ADR 0003과의 관계

ADR 0003이 보류한 것은 **Debate/Consensus**(여러 후보가 서로의 답을 반박하는
다회차 상호 비판)이고, 이 패턴은 **단일 계보의 생성-평가 반복**이라 다른
패턴이다. 0003의 보류 결정 자체는 여전히 유효하다(Debate/Consensus는 계속
안 만듦). 다만 "근거 없이 고비용 패턴을 만들지 않는다"는 0003의 절제 원칙이
이 패턴에도 적용되므로, 이번 도입의 실제 트리거를 명시한다:

1. 사용자가 장기 목표("자율 에이전트를 감싸는 하네스")를 명시적으로 지정
2. "다음 작업" 백로그에 이미 있던 "반복 피드백 루프 검토" 항목과 정확히 일치
3. 참고 레포 v2 분석으로 기존 두 패턴이 못 덮는 실행 모드(반복 피드백)임을 확인

## 결정

- `TeamPattern`에 `"iterative_refinement"` 추가. 흐름: generator 1개가 생성 →
  evaluator(`JUDGE_PROVIDER_KEY` 재사용)가 `judge.check_pass()`로 rubric 합격
  판정 + 수정 피드백 → 불합격이면 (원본 요청 + 직전 시도 + 피드백)으로 재생성
  → 반복. 라운드별 기록은 `refinement.json`(`RefinementRound` 목록).
- **비용 상한**: `MAX_REFINEMENT_ROUNDS = 3`(라운드당 LLM 2회 호출이므로 최악
  6회 + 재시도). 0003이 지적한 "다회차 = 호출 수 몇 배" 리스크를 상한으로 관리.
- **opt-in 전용**: 키워드 자동 라우팅을 두지 않고 `constraints`의
  `"team_pattern:iterative_refinement"` 명시적 override로만 진입(planner의
  `risk_level:` override와 대칭). 실수로 고비용 패턴에 걸리는 일 방지.
- **partial 승격**: 상한 도달/중간 실패 시 마지막 생성물을 버리지 않고
  hierarchical_delegation과 같은 철학으로 `(partial)` 승격, 미통과 기록은
  errors.json에 남김. Safety 체크는 어떤 경로에서도 생략하지 않음(`_finalize()`
  재사용).

## 알려진 단순화 (의도적 — 문제가 관측되면 그때 확장)

- **피드백 히스토리 비누적**: N라운드 프롬프트에는 직전 시도/피드백만 들어간다.
  evaluator가 라운드마다 다른 결함을 지적하면 진동 가능 — 라운드 상한이 이를
  막고, 실측으로 문제가 보이면 누적 히스토리로 확장.
- **evaluator 실패 시 관측 공백**: evaluator가 JudgeError로 죽으면 그 라운드
  생성물은 final.md(partial)에는 남지만 refinement.json에는 안 남는다(판정
  완료된 라운드만 기록).
- **`MAX_REFINEMENT_ROUNDS` 하드코딩**: 비용 직결 상수인데 config.json에 아직
  안 뺐다 — 실사용에서 조정 필요가 생기면 `max_subscription_candidates` 선례대로
  이동.
- **구독 한도 관점**: generator로 구독 CLI를 쓰면 run당 최대 3회 구독 호출
  (fan_out의 `MAX_SUBSCRIPTION_CANDIDATES=1` 보호와 철학이 다름). 라운드 상한으로
  유계라 별도 보호는 두지 않음 — 실제 e2e는 gemini(종량제)로 수행.

## 검증 (2026-07-27)

- mock 통합 테스트 15개 추가(총 260개 통과) — 피드백 프롬프트 주입/상한 partial
  승격/비용 합산/실패 경로 포함.
- 실제 e2e 2회(gemini-2.5-flash): ① 1라운드 통과($0.0069) — 배선 확인.
  ② rubric(출처 신뢰성)과 충돌하는 프롬프트로 실제 2라운드 반복 유도($0.0018) —
  1라운드 fail 피드백("예산 세부화, 사전 설문/담당자 지정 추가")이 2라운드
  답변에 그대로 반영돼 통과함을 refinement.json에서 확인.
