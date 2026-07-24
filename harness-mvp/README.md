# harness-mvp — Phase 1~6 완료 (로드맵 전체 완료) + ADR 0005 (도메인 폴더 아키텍처)

전체 설계는 `../docs/02_구현플랜/harness-implementation-plan-ko.md` 참고. 이 디렉토리는
그 플랜의 Phase 1(Reproducible Run, Step 0~9), Phase 2(Eval Harness), Phase 3(Model
Routing + Provider 인증 모드), Phase 4(Safety and Policy Gate), Phase 5(Harness
Evolution), Phase 6(UI / Dashboard)를 구현한 코드다. Phase 3는 mock이 아니라 실제
claude/codex CLI(구독)와 Gemini REST API(API 키)로 검증했다. **`cli.py`의 기본 실행
경로도 2026-07-10부터 MockProvider가 아니라 이 실제 provider들을 쓴다** — 아래
"빠르게 써보기"의 `run` 명령을 그대로 실행하면 실제 LLM이 호출된다(자격증명 필요,
바로 아래 참고).

로드맵 6단계 완료 이후, 이 하네스를 클라우드 운영/장르 소설/일일 비서처럼 여러
역할로 나눠 쓸 수 있는지 검토하면서 ADR 0005(공유 엔진 + 독립 도메인 폴더)를
추가했다 — `../domains/cloud-ops/`가 그 1차 검증 도메인이다. 자세한 내용은 아래
"구성 요소"의 `src/fetchers/`·`domains/cloud-ops/` 항목과 맨 아래
"ADR 0005" 절 참고.

## 빠르게 써보기

```bash
cd harness-mvp
pip install -e .[dev]                                     # pydantic + pytest 설치

python -m pytest tests/ -v                                # 141개 테스트 실행 (전부 mock/모킹, 실제 CLI/API 호출 없음)

PYTHONPATH=src python -m harness.cli run --task examples/task.fan_out.json
PYTHONPATH=src python -m harness.cli run --task examples/task.fan_out.json --models claude,gemini  # 이 실행만 후보 모델 오버라이드(codex 제외)
PYTHONPATH=src python -m harness.cli run --task examples/task.delegation.json
PYTHONPATH=src python -m harness.cli run --task examples/task.trivial.json    # 적합성 게이트 예시
PYTHONPATH=src python -m harness.cli run --task examples/task.high_risk.json  # 승인 대기 예시
PYTHONPATH=src python -m harness.cli approve run-high-risk-demo               # (또는 reject)
PYTHONPATH=src python -m harness.cli replay run-fan-out-demo
PYTHONPATH=src python -m harness.cli safety-queue                            # Safety 검토 대기 목록
PYTHONPATH=src python -m harness.cli safety-approve <run_id>                  # (또는 safety-reject)
PYTHONPATH=src python -m harness.cli analyze-failures                        # 전체 run 실패 패턴 집계
PYTHONPATH=src python -m harness.cli dashboard                               # 패턴별 성공/경고/실패율 HTML 리포트
```

Windows PowerShell에서는 `$env:PYTHONPATH="src"; python -m harness.cli run --task ...` 형태로
실행한다. (`PYTHONPATH=src`가 필요한 이유: `cli.py`가 `harness` 패키지 안에 있어서
`python -m harness.cli`로 부르는데, `src/`가 파이썬 경로에 잡혀 있어야 `harness`와
`providers` 두 top-level 패키지를 찾을 수 있다.)

**`run`/`approve` 실행 전 필요한 자격증명** (`config.json`의 기본 모델 조합 기준):
`claude auth login`, `codex login`(둘 다 브라우저 구독 로그인), `GEMINI_API_KEY`
환경변수. 셋 중 하나라도 없으면 그 provider만 재시도 후 `status="error"`로
`errors.json`에 기록되고 나머지 provider로 계속 진행된다(별도 mock fallback
없음). `config.json`의 `max_subscription_candidates`(기본 1) 덕분에 claude
CLI와 codex CLI가 한 run에서 동시에 호출되진 않는다(구독 한도 보호,
아래 참고).

**운영 설정** (`harness-mvp/config.json`, 코드 수정 없이 편집 가능 — 파일이
없으면 아래와 동일한 기본값 사용):

```json
{
  "candidate_models": ["claude", "codex", "gemini"],
  "judge_model": "gemini",
  "delegation_model": "claude",
  "max_subscription_candidates": 1
}
```

`candidate_models`는 `run`/`approve`의 `--models` 플래그로 그 실행 한 번만
오버라이드할 수 있다. `judge_model`/`delegation_model`은 `--models`로 안
바뀐다(ADR 0004: 판단자를 후보 생성 모델과 분리해두는 편이 안전) — 바꾸려면
`config.json`을 직접 수정한다.

## 구성 요소

| 파일 | 역할 |
| --- | --- |
| `src/harness/schemas.py` | pydantic 모델 전체 (`TaskInput`, `Plan`, `DelegationStep`, `ProviderConfig`, `Candidate`, `Judging(Score)`, `RunMetrics`, `Observation`, `FitnessCheck`, `Approval`, `EvalCase`/`GradeResult`/`AttemptResult`/`EvalReport`, `FailureCategory`/`FailureReport`, `PatternStats`/`DashboardReport`) |
| `src/harness/run_store.py` | run 디렉토리 입출력 (생성/조회, JSON/Markdown 저장·로드) |
| `src/providers/base.py`, `mock.py` | `Provider` 인터페이스 + 결정적 `MockProvider` (3가지 프로필, 실패 주입 가능) |
| `src/harness/model_runner.py` | fan_out_judge 독립 후보 생성(`run_all`), 적합성 게이트 탈락 시 단일 호출(`direct_call`), 공통 재시도 로직(`generate_with_retry`) |
| `src/harness/subagent_runner.py` | hierarchical_delegation 체인 실행(`delegate`/`run_chain`), 컨텍스트 격리 시뮬레이션 |
| `src/harness/router.py` | 적합성 게이트(`check_fitness`) + team_pattern 사전 분류(`classify_team_pattern`) |
| `src/harness/planner.py` | task → Plan (task_type/risk_level/rubric/team_pattern/delegation_chain 규칙 산출) |
| `src/harness/judge.py` | `judge_provider`로 실제 LLM 판단 호출 1회(reject-first + blind A/B 익명화 프롬프트, JSON 응답 파싱) — ADR 0004로 규칙 기반에서 승격, 실제 claude/codex/gemini로 길이 편향 해소 확인(fan_out_judge 전용) |
| `src/harness/synthesizer.py` | winner 채택 또는 상위 두 후보 병합 (규칙 기반, fan_out_judge 전용) |
| `src/harness/safety.py` | 비밀정보/프롬프트 인젝션/고위험 키워드 규칙 기반 스캔 (패턴 공통) |
| `src/harness/orchestrator.py` | 전체 dispatch: 적합성 게이트 → Planner → (risk_level=high면 승인 대기) → 패턴 실행 → Safety(실패 시 사람 검토 대기) → 기록. `resolve_safety_review()`/`list_safety_review_queue()` 포함. fan_out_judge candidate 선택 시 구독(cli_subscription) provider를 `MAX_SUBSCRIPTION_CANDIDATES`개까지만 호출해 구독 한도 소모를 제한(`_limit_subscription_candidates`) |
| `src/harness/cli.py` | `run` / `replay` / `approve` / `reject` / `safety-queue` / `safety-approve` / `safety-reject` / `analyze-failures` / `dashboard` / `status` / `worktree-sync` / `worktree-check-cleanup` 진입점 (`python -m harness.cli`). `run`/`approve`는 `--models`로 fan_out_judge 후보 모델(claude/codex/gemini 중 선택)을 그 실행만 오버라이드 가능. `status`는 `live_status.py` 문단 참고, `worktree-sync`/`worktree-check-cleanup`은 `scripts/setup_worktree.py` 문단 참고(도입 배경/이력은 `docs/03_진행상황/harness-progress-detail-ko.md`) |
| `src/harness/config.py`, `config.json` | 운영 설정(후보/judge/delegation 모델, 구독 한도 상한)을 코드 밖 파일로 분리. `HarnessConfig`(pydantic) + `load_config()` — 파일 없으면 기본값(기존 하드코딩과 동일) |
| `src/harness/failure_analysis.py` | `analyze_failures()` — 전체 run의 errors.json stage / safety_review.json finding을 집계해 반복되는 실패 패턴을 사람이 볼 수 있게 요약 (Phase 5, 규칙을 자동으로 고치지는 않음) |
| `src/harness/dashboard.py` | `build_dashboard()`/`render_html()` — 저장된 run 산출물(plan.json/metrics.json/errors.json/safety_review.json/approval.json)만으로 team_pattern별 성공/경고/실패율·평균 latency/cost를 정적 HTML로 렌더링 (Phase 6, 재실행 없음, eval pass@k 미포함) |
| `src/harness/live_status.py` | dashboard.py(회고적 집계)와 달리 `cli.py status`로 "지금 뭐가 돌고 있나"를 실시간 판정. `describe_run()` — `run_meta.json`의 pid 생존 여부로 실행중/중단됨을 구분, errors.json이 final.md 없이 단독 존재하면 크래시가 아니라 "출력 없이 정상 종료(done_error)"로 판정. `input.json`의 prompt/task_id도 결과에 포함. `describe_estimate_output()`/`list_domain_activity()` — cloud-ops처럼 LLM 없이 결정론적으로 파일만 생성하는 도메인 작업도 team_pattern=`direct_output`으로 같은 목록에 포함. `list_live_status_multi()`/`_domain_label()` — `--root`(반복 지정) 또는 `--all-domains`(`git worktree list`로 모든 worktree의 모든 도메인 자동 탐색)로 여러 도메인 workspace를 한 번에 조회. `render_html()`/`render_guide_html()` — dashboard.py와 같은 자기완결형 정적 HTML 원칙으로 `--output` 스냅샷 생성(자동 새로고침 없음), "요청 내용" 열은 `<details>`로 접어서 보기, 도메인/team_pattern/상태 드롭다운 필터, "이 표를 보는 법" 가이드는 대시보드 본문과 분리된 별도 페이지(`guide.html`, 서로 실제 파일명을 주고받아 양방향 링크 유지)로 제공. 도입 배경/발견한 버그/실제 검증 이력은 `docs/03_진행상황/harness-progress-detail-ko.md` 참고(2026-07-16~07-24 관련 섹션들) |
| `harness-mvp/docs/adr/0001-*.md` ~ `0005-*.md` | 구조 결정 기록 (Section 12.3). 0003은 "세 번째 팀 패턴 도입 보류", 0004는 "Judge를 규칙 기반에서 단일 실제 LLM 판단으로 승격", 0005는 "역할별 확장은 공유 엔진 + 독립 도메인 폴더" 결정 |
| `examples/task.*.json` | fan_out/delegation/high_risk/trivial 4가지 예시 task |
| `src/evals/graders.py` | deterministic grader — run_status/final.md 존재 여부/필수·금지 문구로 채점 |
| `src/evals/runner.py` | `run_case_k_times(case, providers_factory, k)` — 동일 케이스 k번 실행, pass_rate(pass@1 근사)/pass_at_k/pass_pow_k, cost·latency per success 계산 |
| `src/providers/cli_subscription_provider.py` | `ClaudeCliProvider`/`CodexCliProvider` — claude/codex CLI를 subprocess로 호출, 구독 로그인 세션 사용 (실제 CLI로 검증 완료). 둘 다 프롬프트를 커맨드라인 인자가 아니라 stdin(`input=`)으로 전달(Windows `.CMD` 긴 인자 손상 버그 수정 — claude는 2026-07-13 ADR 0005 작업 중, codex는 같은 날 다른 환경에서 별도 재현/수정) |
| `src/providers/api_provider.py` | `GeminiApiProvider` — Gemini REST(`generateContent`)를 API 키로 직접 호출, `x-goog-api-key` 헤더 사용 (실제 API로 검증 완료) |
| `src/fetchers/base.py` | `Fetcher` ABC(`fetch(**params) -> FetchResult`) — 외부 데이터를 읽기만 하는(생성하지 않는) 조회 전용 컴포넌트, `Provider`와 역할이 달라 별도 top-level 패키지로 분리 (ADR 0005) |
| `src/fetchers/aws_price_fetcher.py` | `AwsEc2PriceFetcher` — AWS Price List Bulk API(인증 불필요)로 EC2 온디맨드 요금 조회, 24시간 캐시. 실제 계정 없이도 검증 완료 |
| `src/fetchers/ncp_price_fetcher.py` | `NcpServerPriceFetcher` — NCP Billing API(`getProductPriceList`, HMAC-SHA256 서명)로 서버 상품 시간당 요금 조회. 실제 계정으로 검증 완료 |
| `domains/cloud-ops/` | 도메인 폴더 1호(ADR 0005 검증용) — 독립 `config.json`/`examples/`/`_workspace/`, `run_estimate.py`(서버 스펙 JSON `examples/spec.*.json`을 받아 Fetcher로 실측 가격을 task 프롬프트에 주입 후 fan_out_judge 실행 — 2026-07-14 시나리오별 스크립트 3개를 이 하나로 통합) |
| `domains/ncp-snapshot-drill/` | 도메인 폴더 2호(2026-07-16) — NCP 스냅샷 생성·복구 훈련 절차서를 생성/검토(Fetcher 없이 일반 지식 기반, 실제 API 자동화 아님). 커스텀 스크립트 없이 독립 `config.json`(hierarchical_delegation 역할별 모델 지정)만으로 harness-mvp CLI를 그대로 사용 — `examples/task.*.json`의 프롬프트가 "조사" 키워드를 포함해 router가 task_type=research로 분류, delegation_chain=[research, design_review]로 자동 라우팅됨(계획만 로컬 검증, 실제 LLM run은 아직 미실행) |
| `domains/centos-eol-migration/` | 도메인 폴더 3호(2026-07-16) — 지원종료된 CentOS 7 서버 9대를 Rocky Linux로 마이그레이션하는 계획을 생성/검토(ncp-snapshot-drill과 동일 구조: Fetcher 없음, 커스텀 스크립트 없음, hierarchical_delegation). 아직 계획 다듬는 단계라 실제 LLM run은 미실행 |
| `domains/cloud-ops-consulting/` | 도메인 폴더 4호(2026-07-22) — 아직 특정 주제로 좁혀지지 않은 클라우드 운영 전반(서버/네트워크/백업/모니터링/장애대응 등)을 조사·논의하는 "가벼운" 상담용 도메인(ncp-snapshot-drill/centos-eol-migration과 동일 구조: Fetcher 없음, `scripts/new_domain.py`로 스캐폴딩). 이 도메인에서 논의하다 특정 주제가 깊어지면 그 주제로 별도 도메인을 새로 분리하는 전제로 만듦. 아직 실제 LLM run은 미실행 |

Planner/Router/Synthesizer/Safety는 실제 LLM을 호출하지 않는 규칙 기반
구현이다 — 목적은 채점/합성/검사의 "품질"이 아니라 파이프라인(파일 기록,
복구 전략, 재현성)이 제대로 도는지 검증하는 것. `evals`의 pass@k도 마찬가지로,
지금은 결정적 mock 위주라 숫자 자체보다 계산 로직(pass_rate/pass_at_k/pass_pow_k
정의, 실패 시도를 cost/latency 평균에서 제외하는 것)이 맞는지 검증하는 게
목적이다. `judge.py`/`cli_subscription_provider.py`/`api_provider.py`는
예외 — 이건 실제 claude/codex CLI와 Gemini API를 호출하는 진짜 연동이고,
실제로 구독 로그인/API 키 상태에서 호출해서 확인했다(자동 테스트는
`subprocess.run`/`requests.post`/judge_provider를 모킹해서 구독 사용량이나
API 과금을 소모하지 않는다).

## 아직 안 한 것

로드맵(Phase 1~6) + `cli.py` 실제 provider 배선 + ADR 0004(Judge 재설계 +
fault-injection 검증) + 구독 한도 보호 + Safety/Approval 실제 provider e2e
재검증 + 운영 설정 파일(`config.json`) + ADR 0005(도메인 폴더 아키텍처 +
Fetcher + `domains/cloud-ops` 실제 검증) + Codex CLI stdin 전달 수정 전부
완료(2026-07-13). 남은 후보(미착수):

1. 대시보드 라이브 진행상황 뷰(사용자 요청, 방향만 기록 — 지금
   `dashboard.py`는 완전히 회고적이라 "현재 실행 중인 작업" 표시가 안 됨,
   run 진행 상태를 기록/갱신하는 장치가 새로 필요).
2. `domains/cloud-ops`를 `--claude-only` 임시 조치 없이 원래 취지(서로 다른
   모델 비교)대로 재검증 — 이 환경엔 `GEMINI_API_KEY`/Codex CLI가 둘 다 준비돼
   있어 가능(NCP 키는 없어서 그쪽만 추정 폴백).

추가 요청 시 우선순위를 정한다.

`scripts/verify_judge_fault_injection.py` — ADR 0004 재검토 트리거 1단계
검증 스크립트. 실제 judge_provider(Gemini API)를 호출하므로 의도적으로
`pytest tests/` 밖에 둔다(작업 규칙 "자동 테스트는 실제 API/CLI 절대
미호출"). 길이와 무관하게 정확성으로 판단하는지 양방향 케이스로 확인 —
`GEMINI_API_KEY` 필요, `PYTHONPATH=src python scripts/verify_judge_fault_injection.py`로
실행. 2026-07-10 실행 결과 2회 연속 전부 PASS(2단계 Self-Consistency로
격상할 근거 없음).

`scripts/new_domain.py` — 도메인 폴더 스캐폴딩 자동화(2026-07-16, ncp-snapshot-drill/
centos-eol-migration 두 도메인을 손으로 만들며 반복한 절차를 스크립트화). Fetcher/
커스텀 실행 스크립트 없이 `config.json`+`examples/task.*.json`만 쓰는 "가벼운"
hierarchical_delegation 도메인 전용(cloud-ops처럼 Fetcher/xlsx 파이프라인이 필요한
도메인은 대상 아님). 실제 API/CLI를 호출하지 않는 순수 로컬 로직(planner/router
규칙 기반)이라 `tests/test_new_domain_script.py`로 정상적으로 pytest 커버 —
`PYTHONPATH=src python scripts/new_domain.py <이름> --task-id <id> --prompt "..."`로
실행하면 config.json/task json 생성 후 `planner.create_plan()`으로 기대한
team_pattern으로 분류되는지 즉시 검증(라우팅 키워드 누락 시 경고). README 표/진행상황
문서 갱신은 의도적으로 자동화하지 않고 체크리스트만 출력한다.

`scripts/setup_worktree.py` + `cli.py worktree-sync`/`worktree-check-cleanup` —
"워크트리 관리 자동화"(2026-07-24 사용자 요청, 지금까지 새 worktree마다 sparse-checkout을
손으로 치고 "각 도메인 worktree도 main이랑 동기화해줘"를 매번 요청해온 걸 대체).
- `setup_worktree.py <도메인 이름>` — 새 worktree 디렉터리 안에서 실행하면
  `git sparse-checkout init --cone` + `set harness-mvp docs domains/<이름>`을 대신
  실행한다. domains/<이름> 폴더가 없으면(오타) 거부, **메인 체크아웃에서 실행하면
  거부**(`git worktree list --porcelain`의 첫 항목과 cwd를 비교) — 메인은 전체
  저장소를 봐야 하므로 실수로 sparse하게 만들면 안 됨. 실제 throwaway worktree에
  적용해 sparse-checkout이 정확히 걸리는 것과 메인 체크아웃 거부를 확인하다가,
  Windows 콘솔(cp949)에서 오류 메시지의 em-dash(—) 때문에 `UnicodeEncodeError`로
  죽는 실제 버그 발견 → `cli.py main()`과 같은 UTF-8 강제 처리 추가로 수정.
- `worktree-sync` — `_discover_git_worktrees()`(기존 `--all-domains` 탐색 로직 재사용)로
  찾은 모든 worktree에 `origin/main`을 merge(브랜치가 main 자신이면 `--ff-only`
  pull). 충돌은 자동으로 안 풀고 보고만 함(실제로 PR #31에서 한 번 발생했던 것처럼
  사람이 봐야 하는 판단). 실제로 4개 도메인 worktree 전부에 실행해 "이미 최신"으로
  정확히 나오는 것 확인.
- `worktree-check-cleanup` — main과 트리 내용이 완전히 같고(`git diff main HEAD`
  없음) 커밋 안 된 변경사항도 없는 worktree를 찾아 보고(**삭제는 자동으로 안 함**).
  처음엔 `gh pr list --state merged`로 "이 브랜치로 PR이 merge된 적 있나"를 물어봤는데,
  실제로 돌려보니 이 프로젝트는 PR merge 후에도 같은 브랜치에서 도메인 작업을 계속
  이어가는 패턴이라(예: `centos-eol-migration-plan-49a2d3`가 PR #33 이후로도 계속
  커밋됨) 4개 도메인 worktree 전부가 잘못 걸리는 실제 버그를 발견 → "지금 이 순간
  main과 내용이 같은가"로 판단 기준을 바꿔서 재구현.

`tests/test_architecture_layers.py` — 아키텍처 불변량 강제(2026-07-24 사용자 요청,
naver 블로그의 "하네스 엔지니어링" 글에서 다룬 "린터/CI로 레이어 의존 방향을
강제한다"는 개념을 검토하다가 도입). `harness/*.py` 전체의 실제 import 그래프를
조사해보니 이미 역방향 의존이 하나도 없는 깨끗한 계층 구조였다(schemas → run_store/
config → router → model_runner/planner → judge/synthesizer/safety/subagent_runner
→ orchestrator → dashboard/failure_analysis/live_status → cli). 이 프로젝트엔
CI가 없어서 "린터"가 아니라 **pytest 테스트**로 구현 — `extract_internal_imports()`가
stdlib `ast`로 각 모듈의 상대 import(`from . import x` / `from .x import ...`)만
뽑고, `_ALLOWED_INTERNAL_IMPORTS`(현재 계층을 그대로 인코딩한 허용 목록)를 벗어나면
테스트가 즉시 실패한다. 새 의존성(import-linter 등) 추가 없이 stdlib만으로 구현해
기존 "phase/step 끝날 때 전체 테스트 실행" 관행과 그대로 맞물린다. **실제 검증**:
`schemas.py`에 일부러 `from . import orchestrator`(역방향 의존)를 임시로 추가해
테스트가 정확히 잡아내는 것을 확인한 뒤 되돌림.

## 테스트 (239개, 전부 통과)

새 테스트 파일을 추가하거나 파일별 테스트 개수가 바뀌면 이 표도 같이 갱신한다
(2026-07-24 문서 감사에서 이 표가 실제(239개)와 다른 숫자(141개)로 오래 방치돼
있던 걸 발견 — `PYTHONPATH=src python -m pytest tests/<파일> --collect-only -q`로
파일별 개수를 확인해 갱신할 것).

| 파일 | 개수 | 대상 |
| --- | --- | --- |
| `test_cli.py` | 30 | `--models` 파싱(기본값/콤마 구분/공백 제거/알 수 없는 모델 거부), 후보 provider 부분 선택 시 나머지 제외, judge/delegation provider는 선택과 무관하게 항상 포함, config.json의 judge_model/delegation_model이 실제 provider 선택에 반영되는지, config.json 없음/일부 필드만 있음 처리, 기본 config 경로가 cwd 기준 상대경로로 해석되는지(ADR 0005, 도메인 폴더 전제 조건), `git worktree list --porcelain` 파싱/탐색(모킹), `worktree-sync`/`worktree-check-cleanup`의 main 동기화·정리 대상 판정 로직 |
| `test_step0_smoke.py` | 2 | pydantic 생성 시점 검증, dispatcher의 unknown team_pattern 방어 |
| `test_step2_model_runner.py` | 5 | fan_out_judge 후보 생성, 재시도/복구, auth_mode별 cost_usd |
| `test_step3_subagent_runner.py` | 5 | 체인 실행, 컨텍스트 격리, 재시도/복구, 체인 중단 |
| `test_step4_planner.py` | 7 | task_type/team_pattern/risk_level/rubric 산출 규칙 |
| `test_step5_router.py` | 9 | 적합성 게이트, team_pattern 사전 분류, direct_call |
| `test_step6_judge_synthesizer.py` | 9 | judge_provider 호출/응답 파싱, 레이블↔model_id 매핑, JudgeError 2종(호출 실패/JSON 파싱 실패), latency/cost 기록, winner/전략 결정, 합성 |
| `test_step7_safety.py` | 5 | 비밀정보/인젝션/고위험 키워드 탐지 |
| `test_step9_integration.py` | 13 | 두 패턴 전체 실행, 재현성, 적합성 게이트, 승인 체크포인트, partial 승격 경로의 Safety 체크 회귀 테스트, 구독 provider 한도 보호(호출 스킵/안전장치) 2종, resume 시 run_meta pid 갱신 |
| `test_phase2_eval_harness.py` | 10 | grader 채점 규칙 5종, pass@k 러너(전부 성공/혼합 결과/성공만 평균/k<1 예외/hierarchical_delegation 케이스) |
| `test_phase3_cli_subscription_provider.py` | 20 | claude/codex CLI 응답 파싱, 에러(비정상 종료/JSON 파싱 실패/CLI 미설치/타임아웃), 토큰 추출, 프롬프트가 커맨드라인 인자가 아니라 stdin으로 전달되는지(둘 다, Windows `.CMD` 긴 인자 손상 버그 회귀 테스트), 격리된 cwd로 실행되는지(저장소 정보 유출 버그 회귀) — `subprocess.run` 모킹, 실제 CLI 미호출 |
| `test_phase3_api_provider.py` | 9 | Gemini 응답 파싱(멀티 파트 포함), API 키 미설정/비정상 상태코드/네트워크 오류(URL 비노출 확인)/JSON 아닌 200 응답/빈 응답, 키가 헤더로만 전달되는지 확인 — `requests.post` 모킹, 실제 API 미호출 |
| `test_phase4_safety_gate.py` | 7 | Safety 실패 시 검토 대기 진입, 승인(release)/반려(block) 처리, 중복 처리 방지, 잘못된 decision 거부, 검토 큐 목록/해소 후 제외 |
| `test_phase5_failure_analysis.py` | 6 | 빈 워크스페이스, errors.json stage별 집계, safety_review.json note를 finding 단위로 분리 집계, 예시 run_id 중복제거·3개 제한, 사유 없음 fallback |
| `test_phase6_dashboard.py` | 13 | run 상태 판정 6종(final.md+에러없음/있음, safety_review pending/rejected, approval pending/rejected), plan.json 없을 때 direct_call 귀속, 평균 latency/cost 계산, 패턴별 분리·정렬, HTML 렌더링 2종 |
| `test_fetchers.py` | 26 | AWS EC2(컴퓨트/EBS/Windows 라이선스 BYOL 구분, 캐시 재사용, 미지원 instance_type/네트워크 실패), AWS EFS(One Zone/Standard 구분), NCP 서버(서명 알고리즘·서명 대상 회귀, 캐시, Windows 라이선스 버전 매칭/폴백/Bare Metal 제외, 시간당(MTRAT)만 추출·가격순 정렬, 월정액(FXSUM) 제외), NCP 스토리지(블록/NAS 구분) |
| `test_live_status.py` | 47 | pid 생존 판정(OS 무관), `describe_run()` 상태 판정 전종(errors.json 단독 존재 시 done_error로 판정하는 회귀 포함), 여러 workspace 합산, direct_output 산출물 판정, `render_html()`/`render_guide_html()`(필터 바, 접어보기, 가이드 페이지 분리·양방향 링크) |
| `test_new_domain_script.py` | 7 | config.json/task json 생성, 이미 존재하는 도메인 재생성 시 에러, 라우팅 키워드 있음/없음에 따른 team_pattern 분류·불일치 경고, provider 레지스트리 정상 구성 |
| `test_setup_worktree_script.py` | 3 | 새 worktree에 sparse-checkout 적용, 메인 체크아웃에서 실행 시 거부, domains/ 폴더 없을 때 거부(git 호출 전에 막히는지) |
| `test_architecture_layers.py` | 6 | `harness/*.py` 상대 import 추출(순수 함수), 전체 모듈이 허용된 계층만 import하는지 |

```bash
python -m pytest tests/ -v
# 또는
python -m unittest discover -s tests -v
```

Local 환경(Python 3.11.9 / 3.12.1, pydantic 2.13.4, pytest 9.1.1)에서 실제로
`pip install` + 테스트 실행 + CLI 6개 시나리오(run/replay/approve/safety-queue/
analyze-failures/dashboard) 수동 실행까지 전부 검증 완료.

## Phase 1 종료 리뷰에서 발견/수정한 것

Step 9까지 끝낸 뒤 전체 코드를 다시 검토하면서 아래 3가지를 발견해 고쳤다 (세부 내용은
`../docs/03_진행상황/harness-progress-detail-ko.md` 참고).

1. **Safety 누락 버그**: hierarchical_delegation 체인이 중단돼 마지막 성공 스텝을
   partial로 승격하는 경로(`_finalize_partial_chain`)가 Safety 체크 없이 바로 final.md를
   쓰고 있었다. Section 12.1이 "Safety는 어떤 경로에서도 생략하지 않는다"고 명시한
   원칙을 위반한 것이라 수정했고, 재발 방지용 회귀 테스트를 추가했다.
2. **falsy-zero 버그**: `plan.num_candidates or len(providers)`가 `num_candidates=0`일
   때 의도와 다르게 `len(providers)`로 평가되는 문제 — `is not None` 체크로 수정
   (현재 Planner는 항상 3을 주므로 실제로는 발생하지 않지만 잠재 결함이라 함께 고침).
3. **콘솔 인코딩 버그**: Windows 기본 코드페이지(cp949)에서 요약 메시지의 em-dash(—)를
   출력하지 못해 CLI가 `UnicodeEncodeError`로 죽었다. `cli.py`에서 stdout/stderr를
   UTF-8로 강제 재설정해서 해결.

## Phase 3 — cli_subscription_provider.py 구현 중 실제로 겪은 것

- **Windows에서 CLI subprocess 호출 버그**: npm이 설치하는 claude/codex CLI는
  `.cmd` 배치 파일이라 `subprocess.run(["claude", ...])`처럼 이름만 주면
  `shell=False`(기본값)에서 `FileNotFoundError`가 났다. `shutil.which()`로 `.cmd`
  확장자까지 포함한 실제 경로를 미리 찾아서 넘기는 걸로 해결했다(`shell=True`를 쓰면
  프롬프트에 셸 인젝션 위험이 생겨서 피했다). 실제로 두 CLI 모두 호출해보고서야
  발견한 문제 — mock provider만 테스트했다면 안 잡혔을 것이다.
- **Gemini Code Assist 개인 구독 지원 종료**: 개인 Google 계정으로 Gemini CLI 구독
  로그인(`GOOGLE_GENAI_USE_GCA`)을 시도했더니 Google이 `IneligibleTierError`로
  막았다("개인용 Code Assist는 이 클라이언트에서 더 이상 지원 안 함, Antigravity로
  이전하라"). 유료 구독으로 바꿔도 동일 — 재시도로 풀리는 문제가 아니라 Google의
  제품 정책 변경이었다. Antigravity는 텍스트 prompt/response를 주고받는 headless
  CLI가 아니라 VS Code 계열 GUI IDE라 이 provider 인터페이스에 안 맞아서, Gemini는
  `cli_subscription_provider.py`가 아니라 `api_provider.py`의 `api_key` 모드로
  지원하기로 결정했다 (API 키 인증 자체는 `gemini --skip-trust -p "..."`로 실제
  확인 완료).
- **claude/codex 구독 인증 실제 확인**: `claude auth status`가
  `{"authMethod":"claude.ai","subscriptionType":"pro"}`, codex의
  `~/.codex/auth.json`이 `{"auth_mode":"chatgpt"}`로 나오는 걸 직접 확인해서, API
  키 환경변수 없이 진짜 구독 세션으로 호출되고 있음을 검증했다(Section 9 "인증 모드
  혼선 방지" 리스크가 실제로는 발생하지 않았음을 확인).

## Phase 3 — api_provider.py(Gemini) 구현 중 실제로 겪은 것

- **API 키 노출 경로 점검**: Gemini REST API는 API 키를 URL 쿼리스트링(`?key=...`)으로
  받는 방식도 지원하는데, 이렇게 하면 `requests`의 연결 예외(`ConnectionError` 등)
  메시지에 요청 URL이 그대로 포함되면서 키가 에러 로그나 `errors.json`에 새어나갈
  위험이 있었다. 실제 API 호출 테스트 중에 이 위험을 인지하고, 키를 `x-goog-api-key`
  HTTP 헤더로 보내는 방식으로 설계했다(실제로 헤더 방식이 정상 동작하는 것도
  확인). 회귀 테스트(`test_api_key_sent_as_header_not_query_string`,
  `test_network_error_raises_without_leaking_url`)로 앞으로도 키가 URL/예외 메시지에
  안 섞이는지 고정해뒀다.
- **리뷰 중 발견한 보완**: 상태 코드 200인데 응답 몸통이 JSON이 아닌 경우(프록시 개입
  등)를 처음엔 처리 안 하고 있었다 — `response.json()` 파싱 실패가 그대로
  전파돼서 `ProviderError`가 아니라 알 수 없는 예외로 죽을 뻔했다. 발견하고
  `_extract_error_message`와 같은 방식으로 감싸서 고쳤고 테스트도 추가했다.
- **비용 추정은 러프한 근사치**: 실제 응답에 `usageMetadata.candidatesTokenCount`
  (출력 토큰)만 쉽게 뽑아 쓸 수 있어서, `cost_usd`는 출력 토큰 단가만으로 추정한다
  (입력 토큰 비용 미포함). 정확한 청구 금액은 Google 콘솔에서 확인해야 한다 — 이건
  버그가 아니라 의도적으로 문서화해둔 단순화다.
- **실제 API로 검증**: `GeminiApiProvider.generate("1+1은? 숫자만 답해")`를 진짜
  API 키로 호출해서 `content='2', tokens=1, cost_usd≈$0.0000025`를 직접 확인했다.

## Phase 4 — Safety Release Gate 설계

플랜에 "safety.py를 release gate로 승격, human review 큐 연결"이라고만 짧게 적혀
있어서, 구현 전에 설계 방향을 확인받았다(ADR 0002 참고).

- **핵심 아이디어**: Safety 실패 = 영구 차단이 아니라 "사람이 검토해야 한다"는
  신호. 실패한 내용을 `pending_review_content.md`에 그대로 보관하고
  `safety_review.json`을 `"pending"`으로 써서 멈춘다 — 이미 있던 승인
  체크포인트(`Approval` 스키마, pending/approved/rejected)를 그대로 재사용해서 새
  스키마를 안 만들었다.
- **리팩터링**: `_finalize_partial_chain()`이 따로 갖고 있던 Safety 처리 코드를
  제거하고 `_finalize()`에 위임하도록 통합했다(`content_prefix`/`success_summary`
  파라미터 추가) — 두 곳에서 Safety 관련 로직을 중복 구현하지 않게 하기 위해서다.
- **기존 테스트 갱신**: `test_step9_integration.py`의
  `test_partial_promotion_still_runs_safety_check`가 "즉시 차단됨"을 검증하고
  있었는데, 동작이 "검토 대기로 멈춤"으로 바뀌어서 테스트도 함께 갱신했다(Safety가
  실행된다는 핵심 취지는 그대로 유지, 결과만 다르게 확인).
- **CLI**: `safety-queue`(검토 대기 목록), `safety-approve`(오탐 판단, 공개),
  `safety-reject`(위험 확정, 계속 보류) 3개 명령 추가. 전부 실제로 실행해서
  end-to-end 확인(주민등록번호가 섞인 프롬프트 → 검토 대기 → 승인 시 공개 / 반려
  시 final.md 끝내 생성 안 됨).

## Phase 5 — Harness Evolution

세 항목 다 설계가 열려 있어 항목별로 AskUserQuestion으로 방향/스코프를 먼저
확인한 뒤 진행했다(자세한 근거는 `../docs/03_진행상황/harness-progress-detail-ko.md`
참고).

- **정기적 정리(pruning)**: 코드베이스 감사 → 죽은 필드 2개
  (`FitnessCheck.estimated_direct_cost_usd`, `RunMetrics.quota_usage_pct` — 둘 다
  선언만 되고 채워진 적 없음) 제거. ADR 0001/0002는 여전히 유효, risk_level="high"
  남용 징후 없음을 확인.
- **세 번째 팀 패턴(Debate/Consensus) 검토 → 도입 보류**: 근거(실패 로그) 없이
  가장 비용이 큰 패턴부터 만드는 건 Agent Soup 방지/"필요할 때만 만듦" 원칙과 안
  맞아서, 지금은 만들지 않고 재검토 트리거 조건만 `docs/adr/
  0003-defer-debate-consensus-pattern.md`에 남겼다.
- **실패 로그 기반 개선 → 집계/분석 장치만 구축**: 운영 데이터가 아직 거의 없어
  규칙을 바로 고치는 대신, `failure_analysis.analyze_failures()`로 여러 run의
  `errors.json`/`safety_review.json`을 모아 반복 패턴을 사람이 볼 수 있게
  집계하는 인프라부터 만들었다(`cli.py analyze-failures`). 실제 PII 트리거
  태스크로 end-to-end 확인(`고위험 키워드 발견 (주민등록번호)` 1회 집계됨).

## Phase 6 — UI / Dashboard

로드맵 한 줄("패턴별 승률, 비용, 지연, 실패율 비교 시각화")만 있어 형태/지표 범위를
먼저 확인받았다.

- **형태**: 정적 HTML 리포트 CLI 명령(`cli.py dashboard`)을 선택했다. 상시 실행되는
  로컬 웹 서버는 새 의존성과 복잡도가 늘어서, CSV/JSON 내보내기는 이 레포와 무관한
  외부 도구에 종속돼서 각각 기각 — 기존 CLI/파일 기반 아키텍처와 가장 잘 맞는 선택.
- **지표 범위 — 설계 중 발견한 것**: `evals/runner.py`의 `EvalReport`가 디스크에
  저장된 적이 없어서(Phase 2), 로드맵이 말하는 "승률"을 만들 데이터 자체가 없었다.
  게다가 run 하나가 패턴 하나만 쓰는 구조라 패턴끼리 "경쟁"하지 않으므로 "승률"
  개념이 애초에 안 맞는다 — 지표를 저장된 run 산출물(plan.json/metrics.json/
  errors.json/safety_review.json/approval.json)만으로 좁히고 "성공률"로 재정의했다.
- **`dashboard.py`**: `build_dashboard()`가 run을 재실행하지 않고, 파일 존재
  여부만으로 `orchestrator.py`의 실제 종료 지점과 대응하는 success/warning/error
  상태를 재구성한다. `render_html()`은 외부 CSS/JS/CDN 없는 자기완결형 HTML.
- **실제 CLI 검증**: Phase 5의 run 3개(PII 트리거 1개 + fan_out/delegation 정상
  2개)로 `dashboard` 실행 → fan_out_judge 성공 1/경고 1(성공률 50%),
  hierarchical_delegation 성공 1(성공률 100%)로 정확히 집계되는 것을 HTML 출력에서
  직접 확인.

## ADR 0005 — 역할별 확장: 공유 엔진 + 독립 도메인 폴더

로드맵 완료 후 "역할별로 나눠 쓸 수 있는가"를 검토하다가, 원래 참고했던
`revfactory/harness`/`revfactory/harness-100`을 "역할별 분리를 어떻게
하는가"로 다시 분석했다. 두 레포 다 하나의 런타임이 내부에서 도메인을
구분하지 않고, 도메인마다 완전히 독립된 프로젝트 폴더를 두는 패턴이었다 —
`harness-100`은 이 패턴을 100개 규모로 실증했다. 그래서 처음 검토했던
`TaskInput.domain` 필드 추가 안(하네스 내부에 도메인 라우팅 축을 추가하는
방향)을 폐기하고, 스키마 변경 없이 `harness/config.py`의
`DEFAULT_CONFIG_PATH`를 cwd 기준 상대경로로 바꾸는 것만으로 성립하는
"공유 엔진 + 독립 도메인 폴더" 구조로 다시 설계했다(`../domains/<name>/`,
`run_store`가 이미 cwd 기준이라 그쪽은 손댈 필요 없었음). 자세한 배경/이유는
`docs/adr/0005-domain-folder-architecture.md` 참고.

- **1차 검증 도메인 `domains/cloud-ops`**: "요청 서버 비용 견적"을 실제
  업무로 골라 AWS/NCP 공개 가격 API를 조회하는 `Fetcher` 추상화
  (`src/fetchers/`, 기존 `Provider`와 역할이 달라 별도 패키지)를 새로
  만들었다. AWS Price List Bulk API(인증 불필요)와 NCP Billing API(HMAC-SHA256
  서명)로 실제 계정 데이터를 조회해 task 프롬프트에 주입하고, 그 위에서
  실제 fan_out_judge 파이프라인을 끝까지(candidate 생성 → judge → synthesis
  → final.md) 돌려서 실측 가격이 최종 산출물까지 정확히 반영되는 걸 확인했다
  (당시 파일명 `run_cost_estimate.py` — 2026-07-14에 다른 시나리오 스크립트들과
  `run_estimate.py`로 통합됨).
- **실제 검증 중 발견한 버그 2건**: (1) NCP 서명 대상 URI에서 `/billing/v1`
  접두사가 빠져 401이 나던 문제 — 호스트 기준 전체 경로로 서명하도록 수정.
  (2) Windows에서 claude CLI가 `.CMD` 배치 파일로 설치되는데, judge
  프롬프트처럼 긴(8KB대) 멀티바이트 프롬프트를 커맨드라인 인자로 넘기면
  인코딩이 깨지는 버그 — `ClaudeCliProvider._invoke()`를 stdin 전달로
  바꿔 해결. Codex CLI는 이 환경에 없어 같은 위험을 검증하지 못했다(후속
  확인 필요, "아직 안 한 것" 참고).
- **`--claude-only` 임시 조치**: 이 환경에 유효한 Gemini API 키와 Codex CLI가
  없어서, `run_cost_estimate.py --claude-only`로 claude 하나만으로
  candidate 2개 + judge를 구성해 배선 자체를 끝까지 검증했다 — 서로 다른
  모델을 비교한다는 fan_out_judge 취지와는 다르므로 코드에 임시 조치임을
  명시해뒀고, 정상 흐름 재검증은 남은 후속 작업이다.
