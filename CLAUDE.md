# Multi-LLM Harness

파일 기반 실행-평가 하네스. 여러 LLM으로 후보를 만들어 비교/합성하거나
(fan_out_judge), 역할별로 순차 위임하거나(hierarchical_delegation), 생성-평가
피드백 루프로 반복 개선하거나(iterative_refinement — ADR 0006), 자율 에이전트에게
도구를 열어주고 그 실행을 감싼다(agentic_task — ADR 0007). 뒤 두 패턴은 opt-in
전용이고, agentic_task는 사람 승인 필수. 코드는 `harness-mvp/`.

**시작할 때 읽을 순서**

- 기본: `docs/00_작업규칙/harness-project-conventions-ko.md`(작업 규칙) →
  `docs/03_진행상황/harness-progress-checklist-ko.md`(현재 상태) → 필요하면
  `harness-progress-detail-ko.md`(세부)/`harness-mvp/README.md`(코드 구조)
- 완전히 새 환경/새 머신이라면: `docs/03_진행상황/` 안 가장 최신
  `harness-handoff-summary-vN-ko.md`부터(자기완결적 인수인계 요약, push할 때마다
  갱신)
- Python/Node.js/claude·codex CLI 등 도구 자체가 없는 완전 초기 머신이라면:
  `docs/04_환경설정/harness-new-machine-setup-guide-ko.md`의 설치 절차부터
- **`docs/03_진행상황/`이 안 보인다면**: 이 저장소는 도메인별 실제 업무 내용과
  진행 이력을 뺀 공개 구조 미러(`621dev/llm-harness`)다. `docs/02_구현플랜/
  harness-implementation-plan-ko.md`(전체 스펙)와 `harness-mvp/README.md`(코드
  구조)부터 보고, `harness-mvp/scripts/new_domain.py`로 바로 도메인을 만들어볼
  수 있다
- **일단 손으로 한 번 돌려보고 싶다면**: `docs/04_환경설정/
  harness-getting-started-guide-ko.md`(일반인용 시작 가이드 — 설치 확인 →
  자격증명 없이 도메인 로직 확인 → 실제 LLM 첫 실행 순서)

**전체 스펙**: `docs/02_구현플랜/harness-implementation-plan-ko.md`.

## 빠른 명령어

```bash
cd harness-mvp && pip install -e .[dev]
python -m pytest tests/ -v
PYTHONPATH=src python -m harness.cli run --task examples/task.fan_out.json
```

## 핵심 규칙 (전체는 docs/00_작업규칙 참고)

- Claude 내부(서브에이전트 프롬프트/추론)는 영어, 그 외(대화/코드/문서)는 한국어.
- 커밋/PR/merge는 매번 명시적 요청 시에만. `main` 직접 커밋 금지 — 브랜치 →
  PR → squash merge.
- 자동 테스트는 실제 API/CLI 절대 미호출(모킹). 실제 연동은 기능당 1회 수동 확인.
- phase/step 종료 시 전체 코드 재검토 + `docs/03_진행상황/*`, `harness-mvp/README.md`
  갱신.
- GitHub: `621dev/multi-llm-harness`(private, 실제 개발). 구조만 공개한 미러는
  `621dev/llm-harness`(public, `domains/`·`docs/03_진행상황/` 제외 —
  `scripts/sync_to_public.py`로 주기적 동기화).
