# 문서 인덱스

멀티 LLM 하네스 프로젝트 문서를 성격별로 분류했다. **지금 어디까지 진행됐는지**만
빠르게 확인하려면 `03_진행상황/harness-progress-checklist-ko.md`부터 보고, 완전히
새 환경/새 머신에서 자기완결적으로 이어가려면 `03_진행상황/` 안의 가장 최신
`harness-handoff-summary-vN-ko.md`부터 읽을 것(번호가 가장 큰 파일이 최신 —
push할 때마다 새 버전이 추가되고 옛 버전은 정리됨, 2026-07-24 기준 v13).
**이 프로젝트를 어떤 규칙으로 진행하는지**(git 워크플로우, 테스트/리뷰 원칙, 언어
정책 등)가 궁금하면 `00_작업규칙/harness-project-conventions-ko.md`를 볼 것.

## 00_작업규칙

- `harness-project-conventions-ko.md` — git/테스트/리뷰/보안/문서/진행방식 규칙 요약
  (CLAUDE.md 스타일로 축약, 규칙이 바뀌면 같이 갱신).

## 01_개념설명

프로젝트를 시작하며 분석한 4개 레포(revfactory/harness, affaan-m/ECC,
gaebalai/claude-code-orchestrator, jikime/harness-lab)와 하네스 관련 기본 개념 설명.

- `harness-repo-summary-beginner-ko.md` — 비개발자용 쉬운 버전
- `harness-repo-summary-technical-ko.md` — 개발 경험자용 기술 버전

## 02_구현플랜

실제로 무엇을, 어떤 순서로 만들 것인지에 대한 설계/구현 계획.

- `harness-implementation-plan-ko.md` — 전체 플랜 원본 (Section 1~12: 목표, 디렉토리
  구조, 스키마, Action/Observation Contract, 패턴 분기, 복구 전략, 구현 순서
  Step 0~9, Phase 로드맵, 리스크, Provider 인증 모드, DoD, jikime/harness-lab에서
  가져온 보완 장치 — 적합성 게이트/사람 승인 체크포인트/ADR/정기적 정리)
- `harness-implementation-plan-summary-beginner-ko.md` — 비개발자용 쉬운 요약
- `harness-implementation-plan-summary-technical-ko.md` — 개발 경험자용 기술 요약

## 03_진행상황

시점별 진행 상황과 인수인계 기록. 최신 문서가 항상 우선한다.

- `harness-progress-checklist-ko.md` — **현재 진행 상황을 한눈에 보는 체크리스트만
  담은 문서.** Step 0~9, Phase 로드맵, Section 12 보완 장치, 테스트 현황을 서술 없이
  상태 표시(완료/미착수)로만 나열한다. Step이 끝날 때마다 갱신되는 살아있는 문서.
- `harness-progress-detail-ko.md` — 위 체크리스트의 세부 버전. Step별로 무엇을
  구현했고, 왜 그렇게 했고(스코프 결정 포함), 무엇을 의도적으로 안 했는지, 알려진
  갭까지 기록한다. "왜 이렇게 했는지"가 궁금할 때 참고. 이 문서도 Step이 끝날 때마다
  갱신된다.
- `harness-handoff-summary-vN-ko.md` — 자기완결적 인수인계 요약. push할 때마다
  최신 상태로 새 버전을 추가한다(예: v13). **옛 버전은 최신 버전에 전부 흡수되므로
  보관하지 않고 정리한다**(2026-07-24 문서 정리 — v2~v12가 이미 detail 문서에
  흡수된 내용을 그대로 중복해서 갖고 있던 것을 발견해 삭제, v2를 인용하던
  `02_구현플랜/harness-implementation-plan-ko.md`도 함께 갱신).

## 04_환경설정

- `harness-new-machine-setup-guide-ko.md` — 완전히 새 머신(Python/Node.js/
  claude·codex CLI 전부 미설치)에서 처음부터 세팅하는 절차. 2026-07-14에 실제로
  한 번 처음부터 실행하며 검증. 겪은 문제(PATH 갱신 안 됨, 셸 변수 노출 실수,
  에이전트 도구 환경 분리 등)도 함께 기록.

## 코드

실제 구현 코드는 이 문서 폴더가 아니라 프로젝트 루트의 `../harness-mvp/`에 있다
(`src/harness/`, `tests/`, `pyproject.toml`, `README.md`).
