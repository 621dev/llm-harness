# 문서 인덱스

멀티 LLM 하네스 프로젝트 문서, 성격별 분류.

- **지금 진행 상황만 빠르게**: `03_진행상황/harness-progress-checklist-ko.md`
- **완전히 새 환경/새 머신에서 자기완결적으로 이어가기**: `03_진행상황/` 안
  가장 최신 `harness-handoff-summary-vN-ko.md`(번호 최대 = 최신 — push할
  때마다 새 버전 추가, 옛 버전 정리, 2026-07-27 기준 v15)
- **진행 규칙**(git 워크플로우, 테스트/리뷰 원칙, 언어 정책 등):
  `00_작업규칙/harness-project-conventions-ko.md`

**`03_진행상황/`이 안 보인다면** — `621dev/llm-harness`(구조만 공개한 미러,
`domains/`+이 폴더는 도메인 실제 업무 내용/진행 이력이 섞여 있어 제외). 이
경우 `02_구현플랜/harness-implementation-plan-ko.md`(전체 스펙)와
`../harness-mvp/README.md`(코드 구조)부터 볼 것.

## 00_작업규칙

- `harness-project-conventions-ko.md` — git/테스트/리뷰/보안/문서/진행방식
  규칙 요약(CLAUDE.md 스타일 축약, 규칙 변경 시 동기화)

## 01_개념설명

프로젝트 초기 분석 4개 레포(revfactory/harness, affaan-m/ECC,
gaebalai/claude-code-orchestrator, jikime/harness-lab) + 하네스 기본 개념.

- `harness-repo-summary-beginner-ko.md` — 비개발자용
- `harness-repo-summary-technical-ko.md` — 개발 경험자용

## 02_구현플랜

무엇을 어떤 순서로 만들지에 대한 설계/구현 계획.

- `harness-implementation-plan-ko.md` — 전체 플랜 원본(Section 1~12: 목표,
  디렉토리 구조, 스키마, Action/Observation Contract, 패턴 분기, 복구 전략,
  구현 순서 Step 0~9, Phase 로드맵, 리스크, Provider 인증 모드, DoD,
  jikime/harness-lab 보완 장치 — 적합성 게이트/사람 승인 체크포인트/ADR/정기적
  정리)
- `harness-implementation-plan-summary-beginner-ko.md` — 비개발자용 요약
- `harness-implementation-plan-summary-technical-ko.md` — 개발 경험자용 요약

## 03_진행상황

시점별 진행 상황 + 인수인계 기록. 최신 문서 우선.

- `harness-progress-checklist-ko.md` — **현재 상태 체크리스트만.** Step 0~9,
  Phase 로드맵, Section 12 보완 장치, 테스트 현황을 서술 없이 상태 표시(완료/
  미착수)로만 나열. Step 종료 시마다 갱신.
- `harness-progress-detail-ko.md` — 위 체크리스트의 세부 버전. Step별 구현
  내용, 이유(스코프 결정 포함), 의도적으로 안 한 것, 알려진 갭까지 기록.
  "왜 이렇게 했는지" 참고용. Step 종료 시마다 갱신.
- `harness-handoff-summary-vN-ko.md` — 자기완결적 인수인계 요약. push할 때마다
  최신 상태로 새 버전 추가(예: v15). **옛 버전은 최신 버전에 전부 흡수 —
  보관 안 함**(2026-07-24 정리 — v2~v12가 detail 문서와 중복 내용이라 삭제,
  v2를 인용하던 `02_구현플랜/harness-implementation-plan-ko.md`도 함께 갱신).

## 04_환경설정

- `harness-new-machine-setup-guide-ko.md` — 완전히 새 머신(Python/Node.js/
  claude·codex CLI 전부 미설치) 세팅 절차. 2026-07-14 실제 처음부터 실행
  검증. 겪은 문제(PATH 갱신 안 됨, 셸 변수 노출 실수, 에이전트 도구 환경
  분리 등)도 함께 기록.
- `harness-getting-started-guide-ko.md` — **일반인용 시작 가이드(CLI).** 도구
  설치는 이미 됐다는 전제 — 설치 확인(무료) → 자격증명 없이 도메인 로직만
  확인(무료) → Gemini 키 하나로 실제 LLM 첫 실행 → 내 도메인 만들기 순서,
  실행 가능한 명령어만(2026-07-24, 각 단계 실제 실행 검증).
- `harness-getting-started-guide-claude-desktop-ko.md` — **일반인용 시작
  가이드(Claude Desktop 전용).** 터미널 직접 조작 없이 Claude Desktop과의
  대화로 위 가이드와 동일한 순서를 진행. MCP(Filesystem + 터미널 실행 서버)
  연결이 먼저 필요 — 이 부분은 공식 문서 조사 기반으로 작성, Claude Desktop
  자체를 직접 조작해 검증하지는 못함(2026-07-24).

## 코드

실제 구현 코드: 이 문서 폴더가 아니라 프로젝트 루트 `../harness-mvp/`
(`src/harness/`, `tests/`, `pyproject.toml`, `README.md`).
