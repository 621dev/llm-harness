# 작업 규칙

Claude 내부(서브에이전트 프롬프트, 추론)는 영어로, 그 외(대화/코드/문서)는 한국어로 유지.

## Git

- 커밋/push/PR/merge는 매번 명시적 요청 시에만. "진행하자" ≠ "merge하자".
- `main` 직접 커밋 금지. 브랜치 → 커밋 → push → PR → squash merge → 브랜치 삭제 → `main` 동기화(fetch --prune → checkout main → pull).
- squash merge는 로컬 브랜치가 "merged"로 안 잡히니 `-D`로 삭제 가능 — 단 `gh pr view --json state,mergedAt`으로 MERGED 확인 후.
- 커밋 전 `git add -A -n`으로 `__pycache__`/`.pytest_cache`/`_workspace`/`.claude/settings.local.json` 미포함 확인.
- 커밋 메시지에 "왜"(리뷰로 찾은 버그면 어떻게 찾았는지) 포함.
- PR은 Summary + Test plan, `--body-file` 사용(헤어독은 특수문자에서 깨짐).

## 테스트

- 자동 테스트는 실제 API/CLI 절대 미호출(`subprocess.run`/`requests.post`/`shutil.which` 모킹).
- 실제 연동은 기능당 1회 수동 확인 + 결과를 문서에 기록. 이후는 모킹 테스트로 대체.
- 동작 변경 시 영향받는 기존 테스트도 갱신 + 이유 명시.
- phase/step 종료 시 전체 테스트 실행 + 캐시 정리.
- 문서의 테스트 개수/파일명은 항상 실제와 일치(grep으로 옛 숫자 확인).

## 리뷰

- phase/step 종료 시 전체 코드 재검토(실제로 여러 버그를 이렇게 찾음).
- 외부 시스템 첫 연동은 최소 1회 실제 호출 — mock만으론 못 잡는 버그가 있음(Windows `.cmd` 실행, 서비스 정책 변경 등).
- 발견한 건 버그/리팩터링/의도적 단순화로 구분해서 기록.

## 보안

- API 키/토큰은 화면에 원문 출력 금지(존재 여부는 `${VAR:+yes}`, 인증파일 통째로 cat 금지).
- API 키는 URL 쿼리스트링이 아니라 헤더로(예외 메시지에 URL 노출 위험).
- `cli_subscription` 사용 시 API 키 환경변수 미설정 확인(안 그러면 조용히 API 과금 전환).

## 문서

- phase/step 종료 시 4개 문서 동기화: `02_구현플랜/harness-implementation-plan-ko.md`(로드맵),
  `03_진행상황/harness-progress-checklist-ko.md`(요약)/`-detail-ko.md`(세부),
  `harness-mvp/README.md`(코드 요약).
- 구조 결정은 ADR로(`harness-mvp/docs/adr/NNNN-*.md`).
- 기존 스키마/패턴과 모양이 같으면 재사용, 새로 안 만듦.
- **push할 때마다 인수인계 문서 갱신**: `docs/03_진행상황/harness-handoff-summary-vN-ko.md`를
  최신 상태로 만들어(또는 새 버전 추가) 같은 커밋/PR에 포함한다. 다른 머신·다른
  세션(Cowork/Code 탭 전환 등)에서 이 문서 하나만 읽고 바로 이어갈 수 있어야
  한다 — 현재 상태, 이번에 한 것, 열려 있는 PR, 필요한 자격증명/환경 설정,
  다음 후보를 담는다. 옛 버전은 최신 버전에 전부 흡수되므로 보관하지 않고
  정리한다(2026-07-24 결정).

## 진행 방식

- 비용/API 키 안 얽히면 phase 단위로 진행 후 리뷰. 얽히면 step 단위로 확인받으며 진행.
  절대 규칙 아님 — 설계가 애매하면 phase여도 구현 전 방향만 먼저 확인.
- Agent Soup/과설계 방지 — 필요할 때만 만듦.
- 애매한 설계는 AskUserQuestion으로 먼저 확인 후 구현.

## 공개 저장소 미러링 (2026-07-24 결정)

- 이 저장소는 private이고, 개인적인 파일/도메인의 실제 업무 내용을 제외한
  "구조"만 별도의 public 저장소에 주기적으로 공유하기로 함(사용자 요청).
  화이트리스트: `CLAUDE.md`, `docs/00_작업규칙`, `docs/01_개념설명`,
  `docs/02_구현플랜`, `docs/04_환경설정`, `docs/README.md`, `harness-mvp/`
  전체.
- **제외: `domains/`(도메인별 실제 업무 내용) + `docs/03_진행상황/`**(체크리스트/
  detail/핸드오프). 진행 이력 서술 안에 실제 고객사명·견적 금액이 그대로
  남아있는 걸 문서 감사 중 발견 — 실제 데이터 파일은 이미 `_workspace/`로
  분리해뒀지만 "그 작업을 왜 했는지" 서술 텍스트 자체가 새는 경로였음.
- **이 문서(`docs/00_작업규칙`)는 화이트리스트에 포함되므로 여기엔 실제
  고객사명을 절대 적지 않는다** — 세부 사례는 비공개 문서인
  `docs/03_진행상황/harness-progress-detail-ko.md`에만 기록.
- `scripts/sync_to_public.py <공개 저장소 clone 경로>` — `git ls-files`로 이
  저장소가 실제로 추적(커밋) 중인 파일만 화이트리스트 경로에서 골라 대상
  디렉터리에 복사한다. 작업 디렉터리를 그대로 복사하지 않는 이유: `_workspace/`/
  `.env` 같은 로컬 전용 파일이 실수로 딸려나갈 위험을 원천 차단하기 위함
- **히스토리 없는 스냅샷 방식**(매번 화이트리스트 파일만 새로 복사, 커밋 안 함)을
  택함 — `git filter-repo`류로 커밋 히스토리를 그대로 이식하면 과거 어느
  시점에 그 경로에 있었을 민감정보까지 함께 새어나갈 위험이 있어서, 그보다
  "지금 이 순간의 상태만" 반영하는 쪽이 안전하다고 판단(사용자 확인)
- 스크립트는 파일 복사만 하고 **커밋/push는 하지 않는다** — 공개 저장소
  쪽에서 내용을 확인한 뒤 직접 커밋/push하거나 별도로 요청해서 진행한다(공개
  콘텐츠 발행은 매번 명시적 확인 필요 원칙과 동일)

## 도메인 세션 관리 (2026-07-16 결정, 2026-07-16 worktree 방식 정정)

- 도메인별 진행상황/의사결정 서술은 공용 `docs/03_진행상황/harness-progress-checklist-ko.md`가
  아니라 각 `domains/<이름>/references/`에 기록한다. 공용 체크리스트에는
  하네스 엔진 자체를 바꾸는 변경(shared tooling/config/fetcher 등)만 남긴다
  — 여러 도메인 세션이 동시에 같은 공용 파일을 건드리면 merge 충돌 위험이 큼.
- 여러 도메인 세션을 **동시에** 띄울 때는 worktree로 세션마다 별도 작업
  디렉터리를 분리한다. **Claude Code 앱 자체가 worktree를 네이티브로
  지원한다** — 화면 하단의 프로젝트 칩(폴더명·브랜치명·"워크트리" 표시)을
  클릭하면 기존 worktree 목록(`claude/<설명>-<해시>` 형식 브랜치, 실제 폴더는
  `<저장소>/.claude/worktrees/<설명>-<해시>/`)이 뜨고 새로 만들 수도 있다.
  **`git worktree add`를 터미널에서 직접 실행해 만들지 않는다** — 앱이 이미
  자체적으로 관리하므로 수동으로 만들면 중복된 빈 worktree가 생긴다(2026-07-16
  실제로 발생 → 정리함, 아래 참고).
  - 각 worktree는 main에서 분기한 독립 브랜치이므로, 완료 후 기존과 동일하게
    커밋→push→PR→squash merge로 main에 반영한다. worktree/브랜치 정리(삭제)는
    앱 UI에서 하거나 `git worktree remove`/`git branch -d`로 한다.
  - **untracked/gitignored 파일(`_workspace/` 등)은 worktree 간 자동 공유되지
    않는다** — 각 worktree는 물리적으로 분리된 디렉터리라, 실제 호스트명 같은
    로컬 전용 메모를 새 worktree에서도 쓰려면 수동으로 복사해야 한다.
  - **여러 worktree가 같은 파일을 동시에 독립적으로 수정하면 내용이 갈라질 수
    있다**(2026-07-16 실제로 ncp-snapshot-drill의 절차서 초안이 main과 worktree
    양쪽에서 다르게 수정된 걸 발견 → 수동으로 병합함). 같은 도메인 파일을 여러
    곳에서 동시에 건드릴 가능성이 있으면 작업 전 다른 worktree의 최신 내용을
    먼저 확인할 것.
- 도메인 `references/`에 실제 운영 서버 정보(호스트명/IP 등)를 쓰게 되는 경우,
  커밋 전 항상 사용자에게 처리 방침을 확인한다(gitignore 대상 `_workspace/`로
  이동 / 익명화 후 커밋 / 그대로 커밋 중 선택 — 지금까지 세 차례 반복된 패턴).
- **새 worktree를 만들면 그 도메인 전용으로 `git sparse-checkout` 설정한다**
  (2026-07-20 결정). git worktree는 기본적으로 저장소 전체(다른 도메인 폴더
  포함)를 통째로 체크아웃해서, 기술적으로는 어느 worktree에서든 다른 도메인
  파일을 수정할 수 있는 상태다 — sparse-checkout으로 그 worktree엔 자기
  도메인만 실제로 존재하게 만들어 이 위험을 없앤다(디스크 사용량도 줄어드는
  부수 효과). **2026-07-24부터 이 절차를 `harness-mvp/scripts/setup_worktree.py
  <도메인 이름>`로 자동화했다** — 새 worktree 디렉터리 안에서 실행하면 아래
  두 명령을 대신 실행해준다(사람이 매번 손으로 칠 필요 없음, 메인 체크아웃에서
  실행하면 자동으로 거부됨):
  ```
  git sparse-checkout init --cone
  git sparse-checkout set harness-mvp docs domains/<이 worktree의 도메인 이름>
  ```
  - `harness-mvp`/`docs`는 모든 세션이 공통으로 참고해야 하니 항상 포함,
    `domains/<이름>` 하나만 그 worktree의 담당 도메인으로 지정한다.
  - **커밋 안 된 변경사항이 있는 파일은 git이 자동으로 보호**한다 — 제외 대상
    경로에 uncommitted 변경이 있으면 경고만 띄우고 그 파일은 그대로 둔다(삭제/
    유실 없음, 2026-07-20 실제로 검증). 그래도 적용 후 `git status`로 원래
    있던 변경사항이 그대로인지 한 번 확인하는 습관을 들일 것.
  - 기존 worktree에 나중에 적용해도 안전(사용자 요청으로 2026-07-20 기존
    worktree 4개에 소급 적용, 데이터 유실 없음 확인).
- **여러 worktree를 main과 동기화/정리하는 것도 명령어로 자동화했다**
  (2026-07-24, "워크트리 관리 자동화" 사용자 요청 — 그 전까지는 "각 도메인
  worktree도 main이랑 동기화해줘"를 요청받을 때마다 worktree마다 손으로
  `git merge main`을 실행해왔음):
  - `python -m harness.cli worktree-sync` — 존재하는 모든 worktree(메인 포함)에
    `origin/main`을 한 번에 merge. 충돌은 자동으로 안 풀고 보고만 하니 해당
    worktree 디렉터리에서 직접 확인해서 풀 것.
  - `python -m harness.cli worktree-check-cleanup` — main과 트리 내용이 완전히
    같고 커밋 안 된 변경사항도 없는 worktree를 찾아 보고(**삭제는 자동으로
    안 함** — 계속 쓸 계획이면 그냥 두면 됨). 주의: "이 브랜치로 PR이 merge된
    적 있나"가 아니라 "지금 이 순간 main과 내용이 같은가"로 판단한다 — 이
    프로젝트는 PR merge 후에도 같은 브랜치에서 도메인 작업을 계속 이어가는
    패턴이라, 과거 merge 이력만으로는 지금 지워도 되는지 판단할 수 없다(실제로
    처음엔 `gh pr list` 기반으로 만들었다가 4개 도메인 worktree 전부가 잘못
    걸리는 걸 실제 CLI로 확인하고 재구현함).
