# ADR 0007: 네 번째 팀 패턴 `agentic_task` — 자율 에이전트를 감싸는 하네스

- 상태: 확정 (2026-07-27)
- 관련: ADR 0006(세 번째 패턴 `iterative_refinement` — 이 ADR이 그 한계를 넘어선다),
  ADR 0002(Safety Release Gate — 생성 파일 스캔으로 확장), ADR 0003/0005(필요할
  때만 만든다는 절제 원칙)

## 배경

프로젝트 장기 목표는 "자율 에이전트를 감싸는 하네스"다. 직전 `iterative_refinement`
도입 후 사용자가 "그럼 에이전트 개념도 들어온 거냐"고 물었고, 코드를 다시 확인한
결과 **아니었다**:

- 저장소 전체에 `tool_call`/`function_call` 관련 코드가 0건
- `iterative_refinement`의 루프는 하네스 코드(`for` 문)가 `verdict.passed`를 보고
  도는 것 — 모델은 매 라운드 "이번 한 번만 답하는" 단발 완성기
- 즉 반복의 **형태**만 빌려왔을 뿐, 참고 레포 taxonomy로 보면 모드 B가 아니라
  사실상 모드 A(결정적 워크플로우)의 연장선이었다

에이전트의 최소 구성 요소 세 가지(도구, 모델이 제어하는 루프, 관찰 피드백)가
전부 없었다.

## 결정

네 번째 팀 패턴 `agentic_task`를 도입한다. **claude CLI의 네이티브 에이전트를
통제된 형태로 열어주는 방식**(경로 A)이다.

핵심 아이러니를 뒤집는 것이다: 우리는 이미 성숙한 에이전트를 갖고 있으면서
`cli_subscription_provider.py`가 빈 임시 디렉토리 + `--print`로 **일부러 묶어두고**
있었다(CLAUDE.md 자동 탐지로 응답이 오염되는 걸 막으려던 조치). 그 족쇄를
안전 경계와 함께 푼다.

이 패턴에서 하네스의 역할이 바뀐다 — 지금까지는 하네스가 "실행 주체"였지만
여기서는 **감싸는 쪽**이다: 작업공간 격리, 도구 허용목록, 턴 상한, 행동 기록,
Safety 게이트, 사람 승인.

### 왜 경로 B(직접 tool-calling 루프 구현)가 아닌가

- 성숙한 에이전트 루프를 밑바닥부터 재발명해야 함(변경량 최대)
- **실측 제약**: 에이전트 루프는 태스크당 수~수십 회 호출인데, 같은 날 측정에서
  Gemini free tier가 일 20회 한도임을 확인 — 결제 활성화 전엔 실행 자체가 불가
- 반면 경로 A는 구독이라 호출 비용 0, 변경량은 provider 1개 + 패턴 분기 1개

예전에 경로 A의 단점으로 꼽았던 "하네스가 중간 과정을 못 본다"는 공식 문서 조사로
해소됐다 — `--output-format stream-json`이 턴별 도구 호출 이벤트를 스트리밍한다.

## 안전 경계 (이 설계의 핵심)

하네스가 사후에 검사하는 게 아니라 **CLI 인자로 애초에 못 하게 막는다**:

| 경계 | 수단 | 효과 |
|---|---|---|
| 기본 거부 | `--permission-mode dontAsk` | 허용 규칙에 없으면 거부 — **나머지 경계의 전제** |
| 경로 격리 | `--allowedTools "Read(./**),Write(./**),Edit(./**)"` + `cwd=<run_dir>/artifacts/agent_workspace/`, `--add-dir` 미사용 | 워크스페이스 밖 파일 접근 거부(`permission_denials`에 기록) |
| 도구 제거 | `--disallowedTools "Bash,Glob,Grep,WebFetch,WebSearch,Task,NotebookEdit"` | 명령 실행·네트워크·경계 우회(Task) 도구를 컨텍스트에서 삭제 |
| 루프 상한 | `--max-turns`(config `max_agent_turns`, 기본 8) | 무한 루프 금지 |
| 사람 승인 | planner가 `risk_level="high"` 강제 | 기존 승인 체크포인트(Section 12.2) 통과 전엔 에이전트 미실행 |
| opt-in 전용 | `constraints: ["team_pattern:agentic_task"]` | 키워드 자동 라우팅 없음 |
| 산출물 검증 | 실행 후 워크스페이스 직접 스캔 | 에이전트 자기 보고를 신뢰하지 않음 |
| 차단 감사 | `permission_denials` → `blocked_tool_uses` | 경계가 "작동했다"는 증거를 기록 |
| Safety | `_finalize(extra_scan_texts=...)` | **생성된 파일 내용도 스캔** |

마지막 항목이 중요하다: 이 패턴의 진짜 산출물은 final.md 텍스트가 아니라 파일이라,
파일을 안 보면 "Safety는 어떤 경로에서도 생략하지 않는다"(Section 12.1)가
형해화된다. `_finalize()`에 선택 인자를 추가해 기존 세 패턴 동작은 그대로 두고
확장했다.

### 첫 e2e에서 경계가 뚫린 것을 발견 (2026-07-27) — 위 표는 그 수정 후 상태다

최초 구현은 공식 문서의 "`--allowedTools`는 여기 없는 도구를 거부한다"는 설명을
믿고 `--allowedTools "Read,Write,Edit"`만 걸었다. **첫 e2e에서 그게 사실이
아님이 드러났다** — 에이전트가 `Bash`로 사용자의 실제 저장소를
탐색(`find /c/Users/.../multi-llm-harness ...`)하고 `Glob`으로 워크스페이스 밖
경로를 훑었다. 통제된 프로브 4회로 원인과 해법을 확정했다:

| 시도한 조합 | 결과 |
|---|---|
| `--allowedTools "Read,Write,Edit"` | Bash 실행됨 (`permission_denials: []`) |
| + `--permission-mode dontAsk` | Bash 여전히 실행됨 |
| + `--disallowedTools "Bash,..."` | Bash 차단("세션에 Bash 도구가 제공되지 않아 실행할 수 없습니다") |
| 도구 이름만 쓴 allow 규칙 | 워크스페이스 **밖** CLAUDE.md 읽기 성공 — cwd는 보안 경계가 아니다 |
| `dontAsk` + `Read(./**)` 스코프 규칙 | 밖 읽기 거부, `permission_denials`에 기록. 안쪽 쓰기는 정상 |

교훈 두 가지:
1. **`-p`(print) 모드에서 `--allowedTools`는 "사전 승인"이지 "제한"이 아니다.**
   print 모드는 기본적으로 권한 프롬프트를 건너뛰는데, 그게 "묻지 않는다 = 그냥
   실행한다"로 동작한다. `dontAsk`로 바꿔야 "허용 규칙에 없으면 거부"가 된다.
2. **cwd는 보안 경계가 아니다.** 경로 스코프 allow 규칙(`Read(./**)`)이 있어야
   실제로 막힌다.

문서를 믿고 "안전하다"고 적었던 것이 실제로는 아니었다 — 안전 경계 주장은
반드시 실측으로 검증해야 한다는 걸 이 프로젝트에서 다시 확인했다(claude CLI
`.CMD` 인자 손상, codex stdin 대기 등과 같은 계열의 교훈). 회귀 방지를 위해
세 인자가 전부 전달되는지 검사하는 테스트를 넣었다.

## 구현

- `schemas.py`: `TeamPattern += "agentic_task"`, `AgentToolUse`/`AgentTurn`/
  `AgentRunResult`
- `providers/cli_subscription_provider.py`: `ClaudeAgentProvider(ClaudeCliProvider)` —
  같은 바이너리의 두 모드(`generate()`는 그대로 유효, `run_agent()` 추가).
  stream-json(JSONL) 파싱은 codex의 `_extract_codex_output_tokens`와 같은 방식
- `harness/agent_runner.py`(신규, `subagent_runner.py`와 같은 자리): 워크스페이스
  준비, 실제 파일 스캔, `agent_turns.json` 기록
- `orchestrator.py`: `AGENT_PROVIDER_KEY`(후보 provider에서 제외 — 도구 권한을
  가진 provider가 일반 생성 자리에 섞이면 안 됨), `_run_agentic_task()`,
  `_finalize()`의 `extra_scan_texts`
- `planner.py`: agentic_task면 `risk_level="high"` 강제(명시적 `risk_level:`
  override가 있으면 그쪽 우선 — 테스트가 승인 게이트를 우회해 실행 경로만
  검증할 수 있어야 함)
- claude 전용: codex는 stream 이벤트 형식이 다르고, gemini는 CLI 구독 모드 자체가
  없다(모듈 docstring 참고)

## 알려진 한계 (의도적 — 실제 필요가 생기면 재검토)

- **파일 도구만**: Bash가 없어 에이전트가 코드를 실행/검증할 수 없다. "테스트를
  돌려보고 고치는" 진짜 개발 루프는 아직 못 한다 — 명령 실행 허용은 격리
  수준(컨테이너 등)을 먼저 갖춘 뒤 별도로 결정할 문제다. 실제로 에이전트는
  Bash를 쓰고 싶어 한다(e2e 1·2회차에서 관측) — 열어주기 전에 격리부터.
- **경계는 CLI 구현에 의존한다**: 우리가 강제하는 건 CLI 인자일 뿐, 실제 차단은
  claude CLI가 한다. CLI 버전이 바뀌면 플래그 의미도 바뀔 수 있다(이번 사고가
  정확히 그런 종류였다 — 문서와 실제가 달랐음). 프로세스 수준 격리(컨테이너,
  별도 사용자 계정)는 없다. CLI 업그레이드 후에는 경계를 재검증할 것.
- **Safety 실패 시 파일이 남는다**: 생성 파일이 Safety에 걸리면 final.md 공개는
  차단되지만 파일 자체는 run 디렉토리 안에 남는다(사용자 프로젝트 밖이라 유출은
  아니고, errors.json에 기록됨). 격리 삭제는 과잉이라 판단.
- **재시도 없음**: 에이전트가 시작조차 못 하면 재시도하지 않는다 — 부분적으로
  파일을 쓰다 만 상태에서 재실행하면 같은 작업을 두 번 하게 된다(다른 패턴의
  "1회 재시도"와 다른 이유).
- **구독 사용량 미집계**: 다른 구독 호출과 마찬가지로 `cost_usd`는 None이다.
  턴 수는 `agent_turns.json`/metrics로 관측 가능.

## ADR 0003/0005의 절제 원칙과의 관계

"실제 트리거 없이 미리 만들지 않는다"에 대한 답: 트리거는 **산출물이 텍스트가
아니라 실제 파일이어야 하는 도메인 필요**다(예: 학습 자료를 주제별 마크다운
파일 세트로 생성). 이 판단의 근거가 되는 실제 e2e는 도입 직후 수행한다.

## 검증 (2026-07-27)

- mock 테스트 32개 추가(총 **294개** 통과, 실제 CLI 미호출) — provider의
  stream-json 파싱/**안전 경계 3종 인자 전달**(위 사고의 회귀 방지)/차단 기록,
  승인 게이트(승인 전 워크스페이스조차 안 생김), 파일 시스템 기반 산출물 판정,
  **생성 파일 비밀정보 → safety review**, max_turns partial 승격
- **실제 e2e 3회**(claude 구독, `examples/task.agentic.json`):
  1. 최초 실행 — 승인 게이트 정상 작동 확인, 그러나 **경계가 뚫린 것을 발견**
     (위 절). 7턴 중 5턴을 Bash/Glob 탐색에 소모하고 파일 1개만 생성 후 상한 도달
  2. 경계 수정 후 — Bash 시도가 실패하고 파일 쓰기로 전환하는 것 확인(파일 2개,
     상한 도달). 이때 "차단 기록이 안 남는다"를 발견해 `permission_denials` →
     `blocked_tool_uses` 기록 추가, 턴 상한 기본값 5→8 상향(초반 2~3턴이 방향
     파악에 쓰이는 걸 실측)
  3. 최종 — **정상 완료**: 5턴 만에 `linux-basics/` 아래 마크다운 3개(4.7KB/
     4.5KB/5.5KB) 생성, `errors.json` 빈 배열, 차단된 시도 0건, 전부 워크스페이스
     안. 108초 소요
- 이 e2e가 ADR 0003/0005의 "실제 트리거"(산출물이 텍스트가 아니라 파일이어야
  하는 필요) 충족 근거이자, 하네스가 에이전트를 감쌌다는 실증이다.
