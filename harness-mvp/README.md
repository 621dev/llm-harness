# harness-mvp — Phase 1~6 완료 + ADR 0005 (도메인 폴더 아키텍처)

전체 설계: `../docs/02_구현플랜/harness-implementation-plan-ko.md`.

구현 범위: Phase 1(Reproducible Run, Step 0~9) ~ Phase 6(UI / Dashboard) 전체.
Phase 3 검증: mock 아님 — 실제 claude/codex CLI(구독) + Gemini REST API(API 키).
`cli.py` 기본 실행 경로: 2026-07-10부터 실제 provider 사용(MockProvider 아님)
— 아래 "빠르게 써보기"의 `run` 명령 그대로 실행 시 실제 LLM 호출(자격증명
필요, 바로 아래).

로드맵 완료 후 ADR 0005(공유 엔진 + 독립 도메인 폴더) 추가 — 클라우드 운영/
장르 소설/일일 비서 등 역할별 확장 검토 결과. 1차 검증 도메인:
`../domains/cloud-ops/`. 세부: 아래 "구성 요소"의 `src/fetchers/`·
`domains/cloud-ops/` 항목, 맨 아래 "ADR 0005" 절.

## 빠르게 써보기

```bash
cd harness-mvp
pip install -e .[dev]                                     # pydantic + pytest 설치

python -m pytest tests/ -v                                # 294개, 전부 mock — 실제 CLI/API 미호출

PYTHONPATH=src python -m harness.cli run --task examples/task.fan_out.json
PYTHONPATH=src python -m harness.cli run --task examples/task.fan_out.json --models claude,gemini  # 이 실행만 후보 모델 오버라이드(codex 제외)
PYTHONPATH=src python -m harness.cli run --task examples/task.delegation.json
PYTHONPATH=src python -m harness.cli run --task examples/task.iterative_refinement.json --models gemini  # 생성-평가 반복 루프(opt-in 전용)
PYTHONPATH=src python -m harness.cli run --task examples/task.agentic.json                    # 자율 에이전트가 실제 파일 생성(opt-in + 사람 승인 필수)
PYTHONPATH=src python -m harness.cli run --task examples/task.trivial.json    # 적합성 게이트 예시
PYTHONPATH=src python -m harness.cli run --task examples/task.high_risk.json  # 승인 대기 예시
PYTHONPATH=src python -m harness.cli approve run-high-risk-demo               # (또는 reject)
PYTHONPATH=src python -m harness.cli replay run-fan-out-demo
PYTHONPATH=src python -m harness.cli safety-queue                            # Safety 검토 대기 목록
PYTHONPATH=src python -m harness.cli safety-approve <run_id>                  # (또는 safety-reject)
PYTHONPATH=src python -m harness.cli analyze-failures                        # 전체 run 실패 패턴 집계
PYTHONPATH=src python -m harness.cli dashboard                               # 패턴별 성공/경고/실패율 HTML 리포트
PYTHONPATH=src python -m harness.cli status --all-domains --output _workspace/overview.html  # 실시간 상태 + 필터/가이드
PYTHONPATH=src python -m harness.cli worktree-sync                          # 모든 도메인 worktree를 origin/main과 동기화
```

Windows PowerShell: `$env:PYTHONPATH="src"; python -m harness.cli run --task ...`
형태. (`PYTHONPATH=src` 이유: `cli.py`는 `harness` 패키지 안 — `src/`가 경로에
잡혀야 `harness`/`providers` 두 top-level 패키지 인식 가능.)

**`run`/`approve` 전 필요 자격증명** (`config.json` 기본 모델 조합 기준):
`claude auth login`, `codex login`(브라우저 구독 로그인), `GEMINI_API_KEY`
환경변수. 하나라도 없으면 그 provider만 재시도 후 `status="error"`로
`errors.json` 기록 — 나머지 provider로 계속 진행(별도 mock fallback 없음).
`config.json`의 `max_subscription_candidates`(기본 1) — claude/codex CLI
동시 호출 방지(구독 한도 보호, 아래 참고).

**운영 설정** (`harness-mvp/config.json`, 코드 수정 없이 편집 가능 — 파일
없으면 아래 기본값):

```json
{
  "candidate_models": ["claude", "codex", "gemini"],
  "judge_model": "gemini",
  "delegation_model": "claude",
  "max_subscription_candidates": 1,
  "max_refinement_rounds": 3,
  "max_agent_turns": 8
}
```

`candidate_models`: `run`/`approve`의 `--models` 플래그로 그 실행만 오버라이드
가능. `judge_model`/`delegation_model`: `--models`로 변경 불가(ADR 0004 —
판단자/후보 생성 모델 분리가 안전) — 바꾸려면 `config.json` 직접 수정.

## 구성 요소

| 파일 | 역할 |
| --- | --- |
| `src/harness/schemas.py` | pydantic 모델 전체 (`TaskInput`, `Plan`, `DelegationStep`, `ProviderConfig`, `Candidate`, `Judging(Score)`, `RefinementVerdict`/`RefinementRound`, `AgentToolUse`/`AgentTurn`/`AgentRunResult`, `RunMetrics`, `Observation`, `FitnessCheck`, `Approval`, `EvalCase`/`GradeResult`/`AttemptResult`/`EvalReport`, `FailureCategory`/`FailureReport`, `PatternStats`/`DashboardReport`) |
| `src/harness/run_store.py` | run 디렉토리 입출력 — 생성/조회, JSON/Markdown 저장·로드 |
| `src/providers/base.py`, `mock.py` | `Provider` 인터페이스 + 결정적 `MockProvider`(프로필 3종, 실패 주입 가능) |
| `src/harness/model_runner.py` | fan_out_judge 독립 후보 생성(`run_all`), 적합성 게이트 탈락 시 단일 호출(`direct_call`), 공통 재시도(`generate_with_retry`) |
| `src/harness/subagent_runner.py` | hierarchical_delegation 체인 실행(`delegate`/`run_chain`), 컨텍스트 격리 시뮬레이션, 역할별 지시문 스코핑(`_apply_role_instruction` — 첫 스텝/이어받는 스텝을 구분해 "당신의 역할은 X" 문구를 입력에 덧붙임, 2026-07-27 server-engineering-learning 도메인 실제 e2e에서 codex 타임아웃 원인 발견 후 추가) |
| `src/harness/agent_runner.py` | agentic_task 전용 — 자율 에이전트 실행을 감싸는 층(ADR 0007). 격리된 `artifacts/agent_workspace/` 준비, 실행 후 **실제 파일 시스템 스캔**으로 산출물 판정(에이전트 자기 보고 불신), 턴별 도구 호출을 `agent_turns.json`으로 기록. 도구 허용목록/턴 상한 같은 실행 시점 제약은 provider가 CLI 인자로 강제 |
| `src/harness/router.py` | 적합성 게이트(`check_fitness`) + team_pattern 사전 분류(`classify_team_pattern`) |
| `src/harness/planner.py` | task → Plan(task_type/risk_level/rubric/team_pattern/delegation_chain 규칙 산출). `constraints`의 `"team_pattern:<pattern>"` 명시적 override 지원(`risk_level:` override와 대칭) — iterative_refinement/agentic_task는 키워드 자동 라우팅 없이 이 opt-in으로만 진입. agentic_task는 실제 파일을 만드는 부수 효과가 있어 `risk_level="high"`를 강제(사람 승인 필수, ADR 0007) |
| `src/harness/judge.py` | `judge_provider`로 실제 LLM 판단(reject-first + JSON 응답 파싱). `evaluate()` — N개 후보 비교(blind A/B 익명화, ADR 0004로 규칙 기반에서 승격, fan_out_judge 전용) + `check_pass()` — 단일 콘텐츠 rubric 합격 판정 + 수정 피드백(iterative_refinement 전용) |
| `src/harness/synthesizer.py` | winner 채택 또는 상위 두 후보 병합(규칙 기반, fan_out_judge 전용) |
| `src/harness/safety.py` | 비밀정보/프롬프트 인젝션/고위험 키워드 규칙 기반 스캔(패턴 공통) |
| `src/harness/orchestrator.py` | 전체 dispatch: 적합성 게이트 → Planner → (risk_level=high면 승인 대기) → 패턴 실행 → Safety(실패 시 사람 검토 대기) → 기록. `resolve_safety_review()`/`list_safety_review_queue()` 포함. fan_out_judge candidate 선택 시 구독 provider를 `MAX_SUBSCRIPTION_CANDIDATES`개까지만 호출(`_limit_subscription_candidates`, 구독 한도 보호). `_run_iterative_refinement()` — 생성→합격 판정→피드백 반영 재생성 반복(`MAX_REFINEMENT_ROUNDS=3` 상한, 라운드별 `refinement.json` 기록, 상한 도달/중간 실패 시 마지막 생성물 partial 승격, ADR 0006). `_run_agentic_task()` — 자율 에이전트 실행을 감싼다(`AGENT_PROVIDER_KEY`로 등록된 provider, `MAX_AGENT_TURNS` 상한(기본 8), 턴 상한 도달 시 partial 승격, 안전 경계가 차단한 시도(`blocked_tool_uses`)는 errors.json/보고서에 기록, ADR 0007). `_finalize()`의 `extra_scan_texts`로 에이전트가 만든 **파일 내용까지 Safety 스캔** |
| `src/harness/cli.py` | `run`/`replay`/`approve`/`reject`/`safety-queue`/`safety-approve`/`safety-reject`/`analyze-failures`/`dashboard`/`status`/`worktree-sync`/`worktree-check-cleanup` 진입점(`python -m harness.cli`). `run`/`approve`는 `--models`로 fan_out_judge 후보 모델(claude/codex/gemini 중 선택)을 그 실행만 오버라이드. `status`는 `live_status.py` 문단, `worktree-sync`/`worktree-check-cleanup`은 `scripts/setup_worktree.py` 문단 참고(도입 배경은 `docs/03_진행상황/harness-progress-detail-ko.md`) |
| `src/harness/config.py`, `config.json` | 운영 설정(후보/judge/delegation 모델, 구독 한도 상한, iterative_refinement 라운드 상한, agentic_task 턴 상한)을 코드 밖으로 분리. `HarnessConfig`(pydantic) + `load_config()` — 파일 없으면 기본값(기존 하드코딩과 동일) |
| `src/harness/failure_analysis.py` | `analyze_failures()` — 전체 run의 errors.json stage / safety_review.json finding 집계, 반복 실패 패턴 요약(Phase 5, 규칙 자동 수정은 아님) |
| `src/harness/dashboard.py` | `build_dashboard()`/`render_html()` — 저장된 run 산출물(plan.json/metrics.json/errors.json/safety_review.json/approval.json)만으로 team_pattern별 성공/경고/실패율·평균 latency/cost를 정적 HTML로 렌더링(Phase 6, 재실행 없음, eval pass@k 미포함) |
| `src/harness/live_status.py` | dashboard.py(회고적 집계)와 달리 `cli.py status`로 실시간 상태 판정. `describe_run()` — `run_meta.json` pid 생존 여부로 실행중/중단됨 구분, errors.json이 final.md 없이 단독 존재 시 크래시 아닌 "출력 없이 정상 종료(done_error)"로 판정. prompt/task_id도 결과 포함. `describe_estimate_output()`/`list_domain_activity()` — LLM 없이 파일만 생성하는 도메인 작업도 team_pattern=`direct_output`으로 같은 목록에 포함. `list_live_status_multi()`/`_domain_label()` — `--root`(반복 지정) 또는 `--all-domains`(`git worktree list`로 전체 도메인 자동 탐색)로 여러 도메인 workspace 한 번에 조회. `render_html()`/`render_guide_html()` — 자기완결형 정적 HTML(`--output` 스냅샷, 자동 새로고침 없음), "요청 내용"은 `<details>` 접기, 도메인/team_pattern/상태 드롭다운 필터, "이 표를 보는 법" 가이드는 별도 페이지(`guide.html`, 양방향 링크). 도입 배경/버그 이력: `docs/03_진행상황/harness-progress-detail-ko.md`(2026-07-16~07-24) |
| `harness-mvp/docs/adr/0001-*.md` ~ `0007-*.md` | 구조 결정 기록(Section 12.3). 0003: 세 번째 팀 패턴(Debate/Consensus) 도입 보류. 0004: Judge 규칙 기반 → 단일 실제 LLM 판단 승격. 0005: 역할별 확장은 공유 엔진 + 독립 도메인 폴더. 0006: 세 번째 팀 패턴 `iterative_refinement`(생성-평가 반복 루프) 도입. 0007: 네 번째 팀 패턴 `agentic_task` — 자율 에이전트(claude CLI)를 안전 경계와 함께 감쌈 |
| `examples/task.*.json` | fan_out/delegation/high_risk/trivial/iterative_refinement/agentic 6가지 예시 task |
| `src/evals/graders.py` | deterministic grader — run_status/final.md 존재/필수·금지 문구 채점 |
| `src/evals/runner.py` | `run_case_k_times(case, providers_factory, k)` — 동일 케이스 k회 실행, pass_rate(pass@1 근사)/pass_at_k/pass_pow_k, cost·latency per success 계산 |
| `src/providers/cli_subscription_provider.py` | `ClaudeCliProvider`/`CodexCliProvider` — claude/codex CLI subprocess 호출, 구독 세션(실제 CLI 검증 완료). 프롬프트는 커맨드라인 인자 아닌 stdin(`input=`) 전달(Windows `.CMD` 긴 인자 손상 버그 수정 — claude는 2026-07-13 ADR 0005 작업 중, codex는 같은 날 별도 환경에서 재현/수정). **`ClaudeAgentProvider`**(ADR 0007) — 같은 claude 바이너리를 에이전트 모드로 여는 서브클래스: `--output-format stream-json`으로 턴별 도구 호출을 관측하고, 안전 경계를 CLI 인자로 강제한다. **경계는 세 인자가 세트**(`--permission-mode dontAsk` + 경로 스코프 allow `Read(./**)` + `--disallowedTools "Bash,Glob,..."`) — 2026-07-27 첫 e2e에서 `--allowedTools`만으로는 전혀 안 막히고 에이전트가 실제 저장소를 탐색한 걸 확인하고 수정했다(ADR 0007 "경계가 뚫린 것을 발견" 절). 차단된 시도는 `permission_denials`→`blocked_tool_uses`로 기록. 턴 상한 도달은 예외가 아니라 `stop_reason="max_turns"` |
| `src/providers/api_provider.py` | `GeminiApiProvider` — Gemini REST(`generateContent`) API 키 직접 호출, `x-goog-api-key` 헤더(실제 API 검증 완료) |
| `src/fetchers/base.py` | `Fetcher` ABC(`fetch(**params) -> FetchResult`) — 읽기 전용 외부 데이터 조회, `Provider`와 역할 구분해 별도 top-level 패키지(ADR 0005) |
| `src/fetchers/aws_price_fetcher.py` | `AwsEc2PriceFetcher` — AWS Price List Bulk API(인증 불필요)로 EC2 온디맨드 요금 조회, 24시간 캐시. 실제 계정 없이 검증 완료 |
| `src/fetchers/ncp_price_fetcher.py` | `NcpServerPriceFetcher` — NCP Billing API(`getProductPriceList`, HMAC-SHA256 서명)로 서버 상품 시간당 요금 조회. 실제 계정 검증 완료 |
| `domains/cloud-ops/` | 도메인 폴더 1호(ADR 0005 검증용) — 독립 `config.json`/`examples/`/`_workspace/`, `run_estimate.py`(서버 스펙 JSON을 받아 Fetcher 실측 가격을 프롬프트에 주입 후 fan_out_judge 실행 — 2026-07-14 시나리오별 스크립트 3개를 이 하나로 통합) |
| `domains/ncp-snapshot-drill/` | 도메인 폴더 2호(2026-07-16) — NCP 스냅샷 생성·복구 훈련 절차서 생성/검토(Fetcher 없음, 일반 지식 기반, 실제 API 자동화 아님). 커스텀 스크립트 없이 독립 `config.json`(역할별 모델 지정)만으로 harness-mvp CLI 그대로 사용, "조사" 키워드로 research→design_review 자동 라우팅(계획만 로컬 검증, 실제 LLM run 미실행) |
| `domains/centos-eol-migration/` | 도메인 폴더 3호(2026-07-16) — 지원종료 CentOS 7 서버 9대 → Rocky Linux 마이그레이션 계획 생성/검토(ncp-snapshot-drill과 동일 구조). 계획 다듬는 단계, 실제 LLM run 미실행 |
| `domains/cloud-ops-consulting/` | 도메인 폴더 4호(2026-07-22) — 주제 미확정 클라우드 운영 전반 상담용 "가벼운" 도메인(동일 구조, `scripts/new_domain.py`로 스캐폴딩). 논의 중 특정 주제가 깊어지면 별도 도메인으로 분리 예정. 실제 LLM run 미실행 |
| `domains/server-engineering-learning/` | 도메인 폴더 5호(2026-07-25) — 초급 엔지니어의 서버 엔지니어링 학습을 돕는 "가벼운" 도메인(Fetcher 없음, `scripts/new_domain.py`로 스캐폴딩). 첫 예시 task는 리눅스 서버 운영 기초(프로세스/네트워크/권한/로그 관리) 리서치 → 학습 자료 검토(research→design_review 체인). 실제 LLM run 미실행 |

Planner/Router/Synthesizer/Safety: 규칙 기반, LLM 미호출 — 목적은 채점/합성/
검사 "품질"이 아니라 파이프라인(파일 기록, 복구 전략, 재현성) 검증. `evals`
pass@k도 동일 — 지금은 결정적 mock 위주라 숫자보다 계산 로직(pass_rate/
pass_at_k/pass_pow_k 정의, 실패 시도를 cost/latency 평균에서 제외) 검증이
목적. 예외: `judge.py`/`cli_subscription_provider.py`/`api_provider.py` —
실제 claude/codex CLI + Gemini API 연동, 실제 구독/API 키 상태로 호출 확인
(자동 테스트는 `subprocess.run`/`requests.post`/judge_provider 모킹 — 구독
사용량·API 과금 미소모).

## 아직 안 한 것

로드맵(Phase 1~6) + `cli.py` 실제 provider 배선 + ADR 0004(Judge 재설계 +
fault-injection 검증) + 구독 한도 보호 + Safety/Approval 실제 provider e2e
재검증 + 운영 설정 파일(`config.json`) + ADR 0005(도메인 폴더 아키텍처 +
Fetcher + `domains/cloud-ops` 검증) + Codex CLI stdin 전달 수정 — 전부
완료(2026-07-13). 남은 후보(미착수):

1. 대시보드 라이브 진행상황 뷰(사용자 요청, 방향만 기록 — `dashboard.py`는
   완전히 회고적, "현재 실행 중" 표시 안 됨, 진행 상태 기록/갱신 장치 필요).
2. `domains/cloud-ops`를 `--claude-only` 임시 조치 없이 원래 취지(모델 비교)로
   재검증 — 이 환경엔 `GEMINI_API_KEY`/Codex CLI 모두 준비됨(NCP 키는 없어
   그쪽만 추정 폴백).

추가 요청 시 우선순위 결정.

`scripts/verify_judge_fault_injection.py` — ADR 0004 재검토 트리거 1단계
검증. 실제 judge_provider(Gemini API) 호출이라 의도적으로 `pytest tests/`
밖(작업 규칙: 자동 테스트는 실제 API/CLI 미호출). 길이 무관 정확성 판단을
양방향 케이스로 확인 — `GEMINI_API_KEY` 필요,
`PYTHONPATH=src python scripts/verify_judge_fault_injection.py` 실행.
2026-07-10 결과: 2회 연속 전부 PASS(2단계 Self-Consistency 격상 근거 없음).

`scripts/new_domain.py` — 도메인 폴더 스캐폴딩 자동화(2026-07-16, ncp-snapshot-drill/
centos-eol-migration 반복 절차 스크립트화). Fetcher/커스텀 실행 스크립트 없이
`config.json`+`examples/task.*.json`만 쓰는 "가벼운" hierarchical_delegation
도메인 전용(cloud-ops류 Fetcher/xlsx 도메인은 대상 아님). LLM/CLI 미호출 순수
로컬 로직 — `tests/test_new_domain_script.py`로 pytest 커버.
`PYTHONPATH=src python scripts/new_domain.py <이름> --task-id <id> --prompt "..."`
실행 시 config.json/task json 생성 + `planner.create_plan()`으로 기대
team_pattern 분류 즉시 검증(라우팅 키워드 누락 시 경고). README 표/진행상황
문서 갱신은 의도적으로 비자동화, 체크리스트만 출력. `render_followup_checklist()`가
`docs/03_진행상황/` 존재 여부로 그 항목을 조건부 표시(공개 미러
`621dev/llm-harness`에는 없음, 2026-07-24 실제 clone에서 발견/수정). Windows
콘솔(cp949) 인코딩도 `cli.py`와 동일하게 UTF-8 강제.

`scripts/setup_worktree.py` + `cli.py worktree-sync`/`worktree-check-cleanup` —
워크트리 관리 자동화(2026-07-24, 매번 sparse-checkout 수동 설정 + "main
동기화해줘" 수동 요청을 대체).
- `setup_worktree.py <도메인 이름>` — 새 worktree 안에서 실행 시
  `git sparse-checkout init --cone` + `set harness-mvp docs domains/<이름>`
  대신 실행. domains/<이름> 없으면 거부, **메인 체크아웃에서 실행하면 거부**
  (`git worktree list --porcelain` 첫 항목과 cwd 비교). throwaway worktree
  실검증 중 Windows 콘솔(cp949) em-dash `UnicodeEncodeError` 발견 → UTF-8
  강제로 수정.
- `worktree-sync` — `_discover_git_worktrees()`(기존 탐색 로직 재사용)로 찾은
  모든 worktree에 `origin/main` merge(브랜치가 main 자신이면 `--ff-only`
  pull). 충돌은 보고만(자동 해결 안 함 — PR #31 실제 사례처럼 사람 판단
  필요). 4개 도메인 worktree 실행 → "이미 최신" 정확히 확인.
- `worktree-check-cleanup` — main과 트리 내용 완전 동일(`git diff main HEAD`
  없음) + 커밋 안 된 변경사항 없는 worktree 탐지(**삭제는 자동으로 안 함**).
  처음엔 `gh pr list --state merged` 기반 — 실제로는 PR merge 후에도 같은
  브랜치로 도메인 작업을 계속하는 패턴이라(예: `centos-eol-migration-plan-49a2d3`
  PR #33 이후에도 커밋 계속) 4개 전부 오탐 → "지금 이 순간 main과 내용이
  같은가" 기준으로 재구현.

`tests/test_architecture_layers.py` — 아키텍처 불변량 강제(2026-07-24, naver
블로그 "하네스 엔지니어링"의 "린터/CI로 레이어 의존 방향 강제" 개념 검토 후
도입). `harness/*.py` 실제 import 그래프 조사 결과 역방향 의존 없는 깨끗한
계층(schemas → run_store/config → router → model_runner/planner →
judge/synthesizer/safety/subagent_runner → orchestrator →
dashboard/failure_analysis/live_status → cli). CI 없는 프로젝트라 "린터" 대신
**pytest 테스트**로 구현 — `extract_internal_imports()`가 stdlib `ast`로 각
모듈 상대 import(`from . import x`/`from .x import ...`)만 추출,
`_ALLOWED_INTERNAL_IMPORTS`(현재 계층 인코딩) 이탈 시 즉시 실패. 새 의존성
추가 없음, 기존 "phase/step 종료 시 전체 테스트" 관행과 그대로 맞물림. **실제
검증**: `schemas.py`에 `from . import orchestrator`(역방향) 임시 추가 →
테스트가 정확히 잡아내는 것 확인 후 원복.

## 테스트 (294개, 전부 통과)

새 테스트 파일 추가/파일별 개수 변경 시 이 표도 같이 갱신(2026-07-24 문서
감사에서 실제(239개)와 다른 옛 숫자(141개)로 오래 방치된 것 발견 —
`PYTHONPATH=src python -m pytest tests/<파일> --collect-only -q`로 개수 확인
후 갱신).

| 파일 | 개수 | 대상 |
| --- | --- | --- |
| `test_cli.py` | 35 | `--models` 파싱(기본값/콤마 구분/공백 제거/알 수 없는 모델 거부), 후보 provider 부분 선택 시 나머지 제외, judge/delegation/agent provider는 선택 무관 항상 포함, config.json의 judge_model/delegation_model/max_refinement_rounds/max_agent_turns 반영 여부, config.json 없음/일부 필드만 처리, 기본 config 경로가 cwd 기준 상대경로 해석(ADR 0005), `git worktree list --porcelain` 파싱/탐색(모킹), `worktree-sync`/`worktree-check-cleanup`의 동기화·정리 판정 로직(up_to_date/merged 판정은 stdout 문구가 아니라 merge 전후 HEAD SHA 비교로 함) |
| `test_step0_smoke.py` | 2 | pydantic 생성 시점 검증, dispatcher unknown team_pattern 방어 |
| `test_step2_model_runner.py` | 5 | fan_out_judge 후보 생성, 재시도/복구, auth_mode별 cost_usd |
| `test_step3_subagent_runner.py` | 8 | 체인 실행, 컨텍스트 격리, 재시도/복구, 체인 중단, 역할별 지시문 스코핑(첫 스텝/이어받는 스텝 문구 구분, input_ref는 원본 내용 유지) |
| `test_step4_planner.py` | 11 | task_type/team_pattern/risk_level/rubric 산출 규칙, `team_pattern:` override(정상/알 수 없는 값 무시), agentic_task의 risk_level=high 강제 및 명시적 override 우선 |
| `test_step5_router.py` | 9 | 적합성 게이트, team_pattern 사전 분류, direct_call |
| `test_step6_judge_synthesizer.py` | 15 | judge_provider 호출/응답 파싱, 레이블↔model_id 매핑, JudgeError 2종(호출/JSON 파싱 실패), latency/cost 기록, winner/전략 결정, 합성, `check_pass()` 6종(pass/fail 파싱, rubric·콘텐츠 프롬프트 포함, JSON 아님/passed 비bool/호출 실패 시 JudgeError) |
| `test_step7_safety.py` | 5 | 비밀정보/인젝션/고위험 키워드 탐지 |
| `test_step9_integration.py` | 13 | 두 패턴 전체 실행, 재현성, 적합성 게이트, 승인 체크포인트, partial 승격 경로 Safety 회귀, 구독 provider 한도 보호 2종, resume 시 run_meta pid 갱신 |
| `test_iterative_refinement.py` | 7 | 반복 루프 통합(1라운드 통과/피드백이 다음 라운드 프롬프트에 주입/상한 도달 시 partial 승격/비용·지연 라운드 합산/생성 영구 실패/evaluator 실패 시 partial/judge provider 미등록 방어) |
| `test_agentic_task.py` | 18 | 자율 에이전트를 감싸는 하네스 검증(ADR 0007) — 승인 전 에이전트 미실행(워크스페이스조차 안 생김)/반려 시 파일 없음/격리 워크스페이스에만 생성/`agent_turns.json` 행동 기록/산출물은 파일 시스템 스캔으로 판정/**생성 파일 비밀정보 → safety review + final.md 차단**(회귀 방지 핵심)/정상 파일 오탐 없음/max_turns partial 승격/에이전트 오류·provider 실패 처리/턴 상한 전달/차단된 도구 사용이 run을 실패시키지 않되 기록은 남는지/에이전트 provider가 후보 목록에서 제외/워크스페이스 스캔 유틸 4종 |
| `test_phase2_eval_harness.py` | 10 | grader 채점 규칙 5종, pass@k 러너(전부 성공/혼합/성공만 평균/k<1 예외/hierarchical_delegation) |
| `test_phase3_cli_subscription_provider.py` | 30 | claude/codex CLI 응답 파싱, 에러(비정상 종료/JSON 파싱 실패/CLI 미설치/타임아웃), 토큰 추출, stdin 전달 확인(Windows `.CMD` 인자 손상 회귀), 격리된 cwd 실행(저장소 정보 유출 회귀). `ClaudeAgentProvider` 10종: stream-json 턴/도구 호출 파싱, 도구 대상만 기록(파일 본문 제외), max_turns는 예외 아닌 `stop_reason`, 에이전트 오류, result 메시지 없으면 실패, **안전 경계 3종이 전부 CLI 인자로 전달되는지**(`--permission-mode dontAsk`/경로 스코프 allow 규칙/`--disallowedTools` — 2026-07-27 실제로 뚫린 것의 회귀 방지), 차단 기록(`permission_denials`) 파싱 — `subprocess.run` 모킹 |
| `test_phase3_api_provider.py` | 9 | Gemini 응답 파싱(멀티 파트), API 키 미설정/비정상 상태코드/네트워크 오류(URL 비노출)/JSON 아닌 200/빈 응답, 키 헤더 전달 확인 — `requests.post` 모킹 |
| `test_phase4_safety_gate.py` | 7 | Safety 실패 시 검토 대기 진입, 승인(release)/반려(block), 중복 처리 방지, 잘못된 decision 거부, 검토 큐 목록/해소 후 제외 |
| `test_phase5_failure_analysis.py` | 6 | 빈 워크스페이스, errors.json stage별 집계, safety_review.json finding 단위 집계, 예시 run_id 중복제거·3개 제한, 사유 없음 fallback |
| `test_phase6_dashboard.py` | 13 | run 상태 판정 6종, plan.json 없을 때 direct_call 귀속, 평균 latency/cost, 패턴별 분리·정렬, HTML 렌더링 2종 |
| `test_fetchers.py` | 26 | AWS EC2(컴퓨트/EBS/Windows 라이선스 BYOL 구분, 캐시, 미지원 instance_type/네트워크 실패), AWS EFS(One Zone/Standard), NCP 서버(서명 알고리즘/대상 회귀, 캐시, Windows 라이선스 매칭/폴백/Bare Metal 제외, 시간당만 추출·정렬, 월정액 제외), NCP 스토리지(블록/NAS) |
| `test_live_status.py` | 47 | pid 생존 판정(OS 무관), `describe_run()` 상태 판정 전종(errors.json 단독 존재 시 done_error 회귀 포함), 여러 workspace 합산, direct_output 판정, `render_html()`/`render_guide_html()`(필터, 접기, 가이드 분리·양방향 링크) |
| `test_new_domain_script.py` | 7 | config.json/task json 생성, 기존 도메인 재생성 에러, 라우팅 키워드 유무별 분류·경고, provider 레지스트리 구성 |
| `test_setup_worktree_script.py` | 3 | 새 worktree sparse-checkout 적용, 메인 체크아웃 실행 시 거부, domains/ 없을 때 거부(git 호출 전 차단) |
| `test_architecture_layers.py` | 6 | `harness/*.py` 상대 import 추출(순수 함수), 전체 모듈의 허용 계층 준수 여부(`agent_runner` 포함) |

```bash
python -m pytest tests/ -v
# 또는
python -m unittest discover -s tests -v
```

Local 환경(Python 3.11.9 / 3.12.1, pydantic 2.13.4, pytest 9.1.1) 검증: `pip
install` + 테스트 실행 + CLI 6개 시나리오(run/replay/approve/safety-queue/
analyze-failures/dashboard) 수동 실행 전부 완료.

## Phase 1 종료 리뷰에서 발견/수정한 것

Step 9 완료 후 전체 코드 재검토, 아래 3건 발견/수정(세부:
`../docs/03_진행상황/harness-progress-detail-ko.md`).

1. **Safety 누락 버그**: hierarchical_delegation 체인 중단 시 마지막 성공
   스텝을 partial 승격하는 경로(`_finalize_partial_chain`)가 Safety 체크 없이
   final.md를 바로 기록 — Section 12.1 "Safety는 어떤 경로에서도 생략 안 함"
   위반. 수정 + 회귀 테스트 추가.
2. **falsy-zero 버그**: `plan.num_candidates or len(providers)`가
   `num_candidates=0`일 때 의도와 다르게 `len(providers)`로 평가 — `is not
   None` 체크로 수정(현재 Planner는 항상 3 지정이라 실발생은 없었으나 잠재
   결함이라 함께 수정).
3. **콘솔 인코딩 버그**: Windows 기본 코드페이지(cp949)에서 요약 메시지
   em-dash(—) 출력 실패 → CLI가 `UnicodeEncodeError`로 종료. `cli.py`에서
   stdout/stderr UTF-8 강제 재설정으로 해결.

## Phase 3 — cli_subscription_provider.py 실제로 겪은 것

- **Windows CLI subprocess 호출 버그**: npm 설치 claude/codex CLI는 `.cmd`
  배치 파일 — `subprocess.run(["claude", ...])`처럼 이름만 주면
  `shell=False`(기본값)에서 `FileNotFoundError`. `shutil.which()`로 `.cmd`
  포함 실제 경로를 미리 찾아 전달(`shell=True`는 셸 인젝션 위험이라 배제).
  실제 두 CLI 호출 중 발견 — mock provider만으로는 안 잡히는 버그.
- **Gemini Code Assist 개인 구독 지원 종료**: 개인 Google 계정 Gemini CLI
  구독 로그인(`GOOGLE_GENAI_USE_GCA`) 시도 → `IneligibleTierError`("개인용
  Code Assist는 이 클라이언트 미지원, Antigravity로 이전"). 유료 구독 전환도
  동일 — 재시도로 해결 불가한 Google 제품 정책 변경. Antigravity는 headless
  CLI 아닌 GUI IDE라 provider 인터페이스 부적합 — Gemini는
  `cli_subscription_provider.py` 대신 `api_provider.py`의 `api_key` 모드로
  지원 결정(API 키 인증은 `gemini --skip-trust -p "..."`로 실제 확인).
- **claude/codex 구독 인증 실제 확인**: `claude auth status` →
  `{"authMethod":"claude.ai","subscriptionType":"pro"}`, codex
  `~/.codex/auth.json` → `{"auth_mode":"chatgpt"}` 직접 확인 — API 키 없이
  구독 세션으로 호출됨을 검증(Section 9 "인증 모드 혼선" 리스크 미발생 확인).

## Phase 3 — api_provider.py(Gemini) 실제로 겪은 것

- **API 키 노출 경로 점검**: Gemini REST API의 URL 쿼리스트링(`?key=...`) 방식
  사용 시 `requests` 연결 예외 메시지에 요청 URL이 그대로 노출 → 키 유출
  위험. 키를 `x-goog-api-key` HTTP 헤더로 전송하도록 설계(헤더 방식 정상 동작
  확인). 회귀 테스트(`test_api_key_sent_as_header_not_query_string`,
  `test_network_error_raises_without_leaking_url`)로 고정.
- **리뷰 중 발견**: 상태 코드 200 + 응답 몸통이 JSON 아닌 경우(프록시 개입
  등) 미처리 — `response.json()` 파싱 실패가 그대로 전파돼 알 수 없는 예외로
  종료될 위험. `_extract_error_message`와 동일 방식으로 감싸 수정, 테스트
  추가.
- **비용 추정은 근사치**: `usageMetadata.candidatesTokenCount`(출력 토큰)만
  사용 — `cost_usd`는 출력 토큰 단가 추정치(입력 토큰 비용 미포함). 정확한
  청구는 Google 콘솔 확인 필요 — 버그 아닌 의도적 단순화.
- **실제 API 검증**: `GeminiApiProvider.generate("1+1은? 숫자만 답해")` 실제
  키 호출 → `content='2', tokens=1, cost_usd≈$0.0000025` 확인.

## Phase 4 — Safety Release Gate 설계

플랜 원문은 "safety.py를 release gate로 승격, human review 큐 연결" 한
줄뿐 — 구현 전 설계 방향 확인(ADR 0002).

- **핵심 아이디어**: Safety 실패 = 영구 차단 아닌 "사람 검토 필요" 신호.
  실패 내용을 `pending_review_content.md`에 보관, `safety_review.json`을
  `"pending"`으로 기록 후 정지 — 기존 승인 체크포인트(`Approval` 스키마,
  pending/approved/rejected) 재사용, 새 스키마 없음.
- **리팩터링**: `_finalize_partial_chain()`의 독자 Safety 처리 코드 제거,
  `_finalize()`로 통합(`content_prefix`/`success_summary` 파라미터 추가) —
  두 곳 중복 구현 방지.
- **기존 테스트 갱신**: `test_partial_promotion_still_runs_safety_check`가
  "즉시 차단"을 검증 중이었으나 동작이 "검토 대기로 정지"로 변경 — 테스트도
  함께 갱신(Safety 실행 여부 검증이라는 핵심 취지는 유지).
- **CLI**: `safety-queue`(검토 대기 목록)/`safety-approve`(오탐 판단,
  공개)/`safety-reject`(위험 확정, 계속 보류) 3개 추가. 실제 end-to-end
  확인(주민등록번호 포함 프롬프트 → 검토 대기 → 승인 시 공개 / 반려 시
  final.md 끝내 미생성).

## Phase 5 — Harness Evolution

세 항목 모두 설계 열려있어 AskUserQuestion으로 방향/스코프 확인 후 진행(근거:
`../docs/03_진행상황/harness-progress-detail-ko.md`).

- **정기적 정리(pruning)**: 코드베이스 감사 → 죽은 필드 2개
  (`FitnessCheck.estimated_direct_cost_usd`, `RunMetrics.quota_usage_pct` —
  선언만 되고 미사용) 제거. ADR 0001/0002 여전히 유효, risk_level="high" 남용
  징후 없음 확인.
- **세 번째 팀 패턴(Debate/Consensus) 검토 → 도입 보류**: 근거(실패 로그)
  없이 고비용 패턴부터 추가는 Agent Soup 방지/"필요할 때만 만듦" 원칙 위반 —
  재검토 트리거 조건만 `docs/adr/0003-defer-debate-consensus-pattern.md`에
  기록.
- **실패 로그 기반 개선 → 집계/분석 장치만 구축**: 운영 데이터 부족 — 규칙
  즉시 수정 대신 `failure_analysis.analyze_failures()`로 여러 run의
  `errors.json`/`safety_review.json` 집계 인프라 구축(`cli.py
  analyze-failures`). 실제 PII 트리거 태스크로 end-to-end 확인(`고위험 키워드
  발견 (주민등록번호)` 1회 집계).

## Phase 6 — UI / Dashboard

로드맵 원문 "패턴별 승률, 비용, 지연, 실패율 비교 시각화" 한 줄 — 형태/지표
범위 사전 확인.

- **형태**: 정적 HTML 리포트 CLI 명령(`cli.py dashboard`) 채택. 상시 로컬
  웹 서버는 의존성/복잡도 증가, CSV/JSON 내보내기는 외부 도구 종속 — 둘 다
  기각, 기존 CLI/파일 기반 아키텍처와 가장 부합.
- **지표 범위 — 설계 중 발견**: `evals/runner.py`의 `EvalReport`가 디스크
  미저장(Phase 2) — "승률" 데이터 자체가 없음. run 하나가 패턴 하나만 써서
  패턴 간 "경쟁" 구조도 아님 — "승률" 개념 자체가 안 맞아, 저장된 run
  산출물(plan.json/metrics.json/errors.json/safety_review.json/approval.json)
  기반 "성공률"로 재정의.
- **`dashboard.py`**: `build_dashboard()`는 run 재실행 없이 파일 존재
  여부만으로 `orchestrator.py` 실제 종료 지점과 대응하는 success/warning/error
  재구성. `render_html()`은 외부 CSS/JS/CDN 없는 자기완결형 HTML.
- **실제 CLI 검증**: Phase 5의 run 3개(PII 트리거 1개 + fan_out/delegation
  정상 2개)로 `dashboard` 실행 → fan_out_judge 성공 1/경고 1(성공률 50%),
  hierarchical_delegation 성공 1(성공률 100%) 정확 집계 확인.

## ADR 0005 — 역할별 확장: 공유 엔진 + 독립 도메인 폴더

로드맵 완료 후 "역할별 확장 가능성" 검토 — 참고 레포
`revfactory/harness`/`revfactory/harness-100`을 "역할별 분리 방식"으로 재분석.
두 레포 모두 런타임 내부 도메인 구분 없이 도메인마다 완전히 독립된 프로젝트
폴더 사용 — `harness-100`이 이 패턴을 100개 규모로 실증. 처음 검토한
`TaskInput.domain` 필드 추가안(하네스 내부 도메인 라우팅 축) 폐기, 스키마
변경 없이 `harness/config.py`의 `DEFAULT_CONFIG_PATH`를 cwd 기준 상대경로로
바꾸는 것만으로 성립하는 "공유 엔진 + 독립 도메인 폴더" 구조 채택
(`../domains/<name>/`, `run_store`는 이미 cwd 기준이라 무수정). 배경/근거:
`docs/adr/0005-domain-folder-architecture.md`.

- **1차 검증 도메인 `domains/cloud-ops`**: "서버 비용 견적" 실제 업무로
  AWS/NCP 공개 가격 API 조회 `Fetcher` 추상화(`src/fetchers/`, `Provider`와
  역할 구분해 별도 패키지) 신규. AWS Price List Bulk API(인증 불필요)/NCP
  Billing API(HMAC-SHA256 서명)로 실제 계정 데이터 조회 → task 프롬프트 주입
  → 실제 fan_out_judge 파이프라인 끝까지(candidate 생성 → judge → synthesis
  → final.md) 실행, 실측 가격이 최종 산출물까지 정확히 반영됨을 확인(당시
  파일명 `run_cost_estimate.py` — 2026-07-14에 `run_estimate.py`로 통합).
- **실제 검증 중 버그 2건**: (1) NCP 서명 대상 URI에서 `/billing/v1` 접두사
  누락 → 401 — 호스트 기준 전체 경로 서명으로 수정. (2) Windows claude CLI
  `.CMD` 배치 파일에서 judge 프롬프트급 긴(8KB대) 멀티바이트 프롬프트를
  커맨드라인 인자로 전달 시 인코딩 손상 — `ClaudeCliProvider._invoke()`를
  stdin 전달로 수정. Codex CLI는 이 환경에 없어 동일 위험 미검증(후속 확인
  필요, "아직 안 한 것" 참고).
- **`--claude-only` 임시 조치**: 이 환경에 유효한 Gemini API 키/Codex CLI
  부재 — `run_cost_estimate.py --claude-only`로 claude 하나만으로 candidate
  2개 + judge 구성, 배선 자체는 끝까지 검증. 서로 다른 모델 비교라는
  fan_out_judge 취지와는 다름을 코드에 명시, 정상 흐름 재검증은 후속 과제.
