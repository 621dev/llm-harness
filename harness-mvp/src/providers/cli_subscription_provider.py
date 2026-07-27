"""CLI Subscription Provider (Phase 3).

harness-implementation-plan-ko.md Section 10을 구현한다. claude/codex CLI를
subprocess로 호출해서, API 키 과금이 아니라 이미 로그인된 구독(Claude Pro/Max,
ChatGPT Plus) 세션의 사용량 한도로 답을 받는다.

**Gemini는 여기 없다.** 실제로 시도해본 결과, 개인 Google 계정으로 Gemini Code
Assist CLI 구독 로그인을 하려 했더니 Google이 `IneligibleTierError`
("This client is no longer supported for Gemini Code Assist for individuals...
migrate to Antigravity")로 막았다 — 재시도로 해결되는 문제가 아니라 Google의
제품 정책 변경이다. Antigravity는 대안으로 제시됐지만 텍스트 프롬프트를 받아
응답을 반환하는 headless CLI가 아니라 VS Code 계열 GUI IDE라 이 provider
인터페이스(subprocess로 prompt -> Candidate)에 맞지 않는다. 그래서 Gemini는
`api_provider.py`의 `api_key` 모드로만 지원한다.

각 CLI는 출력 형식이 달라 서브클래스가 파싱을 나눠 맡는다:
- claude: `--print --output-format json` -> 구조화된 JSON 한 방(`result`, `usage`)
- codex: `exec --json --output-last-message <file>` -> 최종 응답은 파일로 깔끔하게
  받고, 토큰 사용량만 JSONL 이벤트에서 추출

**`ClaudeAgentProvider`(2026-07-27, ADR 0007)**: 위 두 클래스가 CLI의 에이전트
능력을 일부러 묶어두는 것과 반대로, 같은 claude 바이너리를 "에이전트 모드"로
연다(도구 사용 허용). agentic_task 패턴 전용이며 안전 경계(작업공간 격리/도구
허용목록/턴 상한)를 호출 인자로 강제한다 — 자세한 건 그 클래스 docstring 참고.
codex는 stream 이벤트 형식이 달라 이번 범위 밖이다.

실패 처리는 Provider 계약대로 전부 예외(`ProviderError`)로 던진다: CLI 바이너리가
없거나(설치 안 됨), 로그인이 안 돼 있거나, 타임아웃되거나, 응답 파싱이 실패하면
model_runner의 재시도 로직에 맡긴다.

Windows에서 실제 subprocess 호출로 발견한 것 (Phase 3 `.cmd` 이슈와 같은 종류):
codex CLI는 프롬프트를 인자로 줘도 stdin이 파이프/미상속 상태면 "추가 입력"을
더 읽으려고 대기해서, `stdin`을 명시적으로 안 닫으면 무한 대기에 걸린다(부모
프로세스의 stdin을 그대로 물려받으면 EOF가 절대 안 옴) — `stdin=subprocess.DEVNULL`로
즉시 EOF를 줘서 해결. 또한 codex는 실행 디렉토리가 "신뢰된 디렉토리"가 아니면
(git repo 안이어도) 별도 승인을 요구하므로 `--skip-git-repo-check`로 우회한다.
claude CLI는 이 문제가 없었다(실제 호출로 확인).

**2026-07-13 실제 fan_out_judge(cloud-ops 도메인, ADR 0004 judge 호출)로 발견한
버그**: claude는 npm이 `.CMD` 배치 파일로 설치하는데(위 `_resolve_executable()`
docstring 참고), Windows에서 `.CMD`를 통해 긴 멀티바이트(UTF-8) 인자를 넘기면
cmd.exe의 명령줄 처리 과정에서 깨진다 — 실측으로 프롬프트 길이가 약 8KB를 넘는
순간(candidate 2개를 합쳐 심사하는 judge 프롬프트에서 실제로 발생) `subprocess.run`이
디코딩 불가능한 바이트를 stdout으로 돌려주며 죽는 걸 직접 재현해서 확인했다. 인자
대신 stdin으로 프롬프트를 넘기면(`--input-format text` + `input=prompt`) 명령줄
길이 제한 자체를 안 타므로 이 문제가 사라진다 — `ClaudeCliProvider._invoke()`가 이
방식으로 고쳐졌다.

**2026-07-13(다른 환경, Codex CLI 있음)에서 재현/수정 확인**: 같은 14KB대
멀티바이트 프롬프트를 codex에 위치 인자로 주면 응답이 완전히 깨지는(mojibake)
걸 직접 재현했다 — claude와 동일한 `.CMD` 경유 인자 손상. `CodexCliProvider`는
프롬프트를 위치 인자로 주지 않고 stdin(`input=prompt`)으로만 넘기도록 고쳤다.
claude와 다른 점: codex는 별도 `--input-format` 플래그가 없고, 대신 "PROMPT
위치 인자가 없으면 stdin 전체를 지시문으로 읽는다"는 자체 경로가 있어 위치
인자를 생략하는 것만으로 충분하다. 이전에 걸어뒀던 `stdin=subprocess.DEVNULL`
(무한 대기 방지용)도 제거했다 — `input=`이 stdin을 쓰고 자동으로 닫아줘서
(EOF) 같은 효과를 내면서 프롬프트 손실이 없다. 수정 후 같은 14KB 프롬프트로
재검증해서 끝에 심어둔 마커 문자열이 정확히 돌아오는 것까지 확인했다.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Optional, Sequence

from harness.schemas import AgentRunResult, AgentToolUse, AgentTurn, Candidate

from .base import Provider, ProviderError

DEFAULT_TIMEOUT_SEC = 120.0
# 에이전트 모드는 도구를 여러 번 호출하며 진행하므로 단발 완성보다 오래 걸린다
# (ADR 0007). 턴 상한(--max-turns)이 실질 상한이지만, 한 턴이 통째로 멈추는
# 경우를 대비해 벽시계 타임아웃도 별도로 넉넉하게 둔다.
DEFAULT_AGENT_TIMEOUT_SEC = 600.0

# 에이전트 안전 경계 (ADR 0007). **아래 세 가지를 모두 걸어야 실제로 강제된다** —
# 2026-07-27 첫 e2e에서 `--allowedTools`만으로는 전혀 막히지 않는 걸 실측으로
# 확인했다(에이전트가 Bash로 저장소를 뒤지고 워크스페이스 밖 파일을 읽었다):
#
# 1. `--permission-mode dontAsk`: -p(print) 모드는 기본적으로 권한 프롬프트를
#    건너뛰는데, 이게 "묻지 않는다 = 그냥 실행한다"로 동작한다. dontAsk를 줘야
#    "허용 규칙에 없으면 거부"로 바뀐다. 이게 없으면 나머지 두 개도 무의미하다.
# 2. 경로 스코프 allow 규칙(`Read(./**)` 등): 도구 이름만 쓰면(`Read`) 경로 제한이
#    없어 cwd 밖 파일도 읽힌다 — cwd는 보안 경계가 아니다. `./**`로 묶어야
#    워크스페이스 밖 접근이 실제로 거부된다(permission_denials에 기록됨).
# 3. `--disallowedTools`(bare 이름): 해당 도구를 아예 컨텍스트에서 제거한다.
#    dontAsk가 read-only 명령은 통과시키는 등 예외가 있어, 위험 도구는 애초에
#    존재를 지우는 쪽이 확실하다.
#
# 공식 문서의 "--allowedTools는 여기 없는 도구를 거부한다"는 설명은 최소한 print
# 모드에서는 사실이 아니었다 — 문서보다 실측을 따른다.
DEFAULT_AGENT_ALLOWED_TOOLS = ("Read(./**)", "Write(./**)", "Edit(./**)")

# 컨텍스트에서 아예 제거할 도구: 명령 실행(Bash), 워크스페이스 밖 탐색(Glob/Grep),
# 네트워크(WebFetch/WebSearch), 하위 에이전트 위임(Task — 경계를 우회할 수 있음).
DEFAULT_AGENT_DISALLOWED_TOOLS = ("Bash", "Glob", "Grep", "WebFetch", "WebSearch", "Task", "NotebookEdit")

# 권한 모드: "허용 규칙에 없으면 거부" (위 1번). 이 값을 바꾸면 경계 전체가 무너진다.
AGENT_PERMISSION_MODE = "dontAsk"


class CliSubscriptionProvider(Provider):
    """claude/codex CLI subprocess 호출의 공통 로직. temperature는 두 CLI 모두
    노출하지 않는 옵션이라 받기만 하고 무시한다(인터페이스 계약 준수 목적)."""

    executable: str = ""  # 서브클래스가 지정

    def __init__(self, config, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> None:
        super().__init__(config)
        self.timeout_sec = timeout_sec

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        start = time.monotonic()
        try:
            content, tokens = self._invoke(prompt)
        except FileNotFoundError as exc:
            raise ProviderError(
                f"{self.executable} CLI를 찾을 수 없다 (설치돼 있는지, PATH에 잡히는지 확인 필요): {exc}"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"{self.executable} CLI 호출이 {self.timeout_sec}초 안에 끝나지 않음") from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        return Candidate(
            model_id=self.model_id,
            content=content,
            tokens=tokens,
            latency_ms=latency_ms,
            cost_usd=None,  # cli_subscription 모드는 cost_usd를 채우지 않는다 (schemas.py Candidate 참고)
            status="success",
        )

    def _invoke(self, prompt: str) -> tuple[str, Optional[int]]:
        """(content, tokens)를 반환한다. 서브클래스가 구현."""
        raise NotImplementedError

    def _resolve_executable(self) -> str:
        """PATH에서 실제 실행 파일 경로를 찾는다.

        Windows에서는 npm이 CLI를 `.cmd` 배치 파일로 설치하는데, `subprocess.run(["claude", ...])`
        처럼 이름만 주면 `shell=False`(기본값)에서 `FileNotFoundError`가 난다(직접 실행해보고
        확인한 문제). `shutil.which()`로 `.cmd`까지 포함한 실제 경로를 미리 찾아서 넘기면
        `shell=True` 없이도(=프롬프트에 셸 인젝션 위험 없이) 정상 실행된다.
        """
        resolved = shutil.which(self.executable)
        if resolved is None:
            raise ProviderError(f"{self.executable} CLI를 PATH에서 찾을 수 없다 (설치돼 있는지 확인 필요)")
        return resolved


class ClaudeCliProvider(CliSubscriptionProvider):
    """`claude --print --output-format json`으로 Claude Code CLI를 호출한다."""

    executable = "claude"

    def _invoke(self, prompt: str) -> tuple[str, Optional[int]]:
        # 프롬프트를 커맨드라인 인자가 아니라 stdin으로 넘긴다 — Windows에서 claude가
        # .CMD 배치 파일로 설치되는데, 긴(약 8KB+) 멀티바이트 인자를 cmd.exe 경유로
        # 넘기면 깨지는 걸 실제로 재현해서 확인했다(모듈 docstring 참고). stdin에는
        # 이런 길이 제한이 없다. input=을 쓰면 subprocess가 알아서 stdin을 쓰고 닫아줘서
        # 별도 stdin=DEVNULL이 필요 없다(input=과 stdin=은 동시에 줄 수 없음).
        #
        # cwd를 명시 안 하면 부모 프로세스(harness)의 작업 디렉토리를 그대로 물려받는데,
        # `claude`는 여전히 Claude Code라 그 디렉토리의 CLAUDE.md를 자동 탐지하고 git
        # 상태까지 인지해서(--bare로 끄면 OAuth/구독 인증까지 같이 꺼짐 — 못 씀)
        # "순수 텍스트 완성"이어야 할 응답에 실제 저장소 상태가 새어 들어온다(2026-07-14
        # hierarchical_delegation 역할 분담 실제 검증 중 발견: harness-mvp 자체를 프로젝트로
        # 인식해서 "git status에 cli.py 수정사항이 남아있는데..." 같은 응답을 반환함).
        # 격리된 빈 임시 디렉토리를 cwd로 줘서 CLAUDE.md/git 저장소를 못 찾게 막는다.
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = subprocess.run(
                [self._resolve_executable(), "--print", "--output-format", "json", "--input-format", "text"],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                encoding="utf-8",
                cwd=tmp_dir,
            )
        if result.returncode != 0:
            raise ProviderError(f"claude CLI 종료 코드 {result.returncode}: {result.stderr.strip()}")

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise ProviderError(f"claude CLI 응답을 JSON으로 파싱하지 못함: {result.stdout[:200]!r}") from exc

        if data.get("is_error"):
            raise ProviderError(f"claude CLI가 오류를 반환함: {data.get('result')}")

        content = data.get("result")
        if not content:
            raise ProviderError(f"claude CLI 응답에 result 필드가 없음: {data}")

        tokens = data.get("usage", {}).get("output_tokens")
        return content, tokens


class ClaudeAgentProvider(ClaudeCliProvider):
    """같은 claude CLI를 "에이전트 모드"로 호출한다 (agentic_task 패턴, ADR 0007).

    `ClaudeCliProvider`를 상속하므로 `generate()`(단발 텍스트 완성)는 그대로
    유효하다 — 같은 바이너리의 두 사용 모드일 뿐이다. 차이는 `run_agent()`가
    도구 사용을 **허용**한다는 것: 부모 클래스가 빈 임시 디렉토리로 에이전트
    능력을 묶어두는 반면, 여기서는 하네스가 만든 작업공간을 cwd로 주고 파일
    도구를 열어준다.

    안전 경계는 호출 인자로 강제한다(하네스가 아니라 CLI가 지키게 함):
    - `--permission-mode dontAsk` + 경로 스코프 allow 규칙(`Read(./**)` 등) —
      워크스페이스 밖 파일 접근 거부. **cwd만으로는 안 막힌다**(실측 확인)
    - `--disallowedTools`로 Bash/Glob/Grep/Web*/Task를 컨텍스트에서 제거
    - `--max-turns`로 루프 상한
    세 가지는 세트다 — 자세한 근거는 모듈 상단 상수 주석 참고.

    `--output-format stream-json`을 쓰는 이유: 최종 결과만 받는 `json`과 달리
    턴별 도구 호출 이벤트가 스트리밍돼서, 하네스가 "에이전트가 실제로 무엇을
    했는지"를 기록할 수 있다. 감쌀 대상의 내부를 관측 못 하면 감싼다고 할 수 없다.
    """

    def __init__(self, config, *, timeout_sec: float = DEFAULT_AGENT_TIMEOUT_SEC) -> None:
        super().__init__(config, timeout_sec=timeout_sec)

    def run_agent(
        self,
        prompt: str,
        workspace: Path,
        *,
        max_turns: int,
        allowed_tools: Sequence[str] = DEFAULT_AGENT_ALLOWED_TOOLS,
        disallowed_tools: Sequence[str] = DEFAULT_AGENT_DISALLOWED_TOOLS,
    ) -> AgentRunResult:
        """에이전트를 workspace 안에서 실행하고 턴별 기록과 함께 결과를 돌려준다.

        정상 종료가 아니어도 예외를 안 던지는 경우가 있다 — 턴 상한 도달은
        CLI가 오류로 종료하지만 그때까지 만든 파일은 유효하므로
        `stop_reason="max_turns"`로 반환한다(체인/refinement의 partial 승격과
        같은 철학). 진짜 실패(바이너리 없음/타임아웃/파싱 불가)만 ProviderError다.
        """
        start = time.monotonic()
        command = [
            self._resolve_executable(),
            "--print",
            "--output-format", "stream-json",
            "--verbose",  # stream-json은 print 모드에서 --verbose와 함께 써야 한다(공식 문서)
            "--input-format", "text",
            "--max-turns", str(max_turns),
            # 아래 세 인자는 세트로만 의미가 있다 (모듈 상단 상수 주석 참고) —
            # 하나라도 빠지면 에이전트가 워크스페이스 밖으로 나갈 수 있다.
            "--permission-mode", AGENT_PERMISSION_MODE,
            "--allowedTools", ",".join(allowed_tools),
            "--disallowedTools", ",".join(disallowed_tools),
        ]
        try:
            result = subprocess.run(
                command,
                input=prompt,  # 프롬프트는 stdin (부모 클래스 docstring의 .CMD 인자 손상 이슈)
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                encoding="utf-8",
                cwd=str(workspace),
            )
        except FileNotFoundError as exc:
            raise ProviderError(f"claude CLI를 찾을 수 없다: {exc}") from exc
        except subprocess.TimeoutExpired as exc:
            raise ProviderError(f"claude 에이전트 실행이 {self.timeout_sec}초 안에 끝나지 않음") from exc

        latency_ms = int((time.monotonic() - start) * 1000)
        run_result = _parse_agent_stream(result.stdout)

        if run_result is None:
            # result 메시지가 아예 없다 = 에이전트가 시작조차 못 했다는 뜻이라
            # 파일도 있을 수 없다. 이건 진짜 실패다.
            raise ProviderError(
                f"claude 에이전트 응답을 해석할 수 없음 (종료 코드 {result.returncode}): "
                f"{(result.stderr or result.stdout)[:200]!r}"
            )

        run_result.latency_ms = latency_ms
        return run_result


def _parse_agent_stream(stdout: str) -> Optional[AgentRunResult]:
    """stream-json(JSONL) 출력에서 턴별 도구 호출과 최종 결과를 뽑는다.

    result 메시지를 못 찾으면 None(호출부가 ProviderError로 승격). JSON이 아닌
    배너 줄이 섞일 수 있어 `{`로 시작하는 줄만 파싱하는 건 codex 쪽
    `_extract_codex_output_tokens`와 같은 방식이다.
    """
    turns: list[AgentTurn] = []
    final: Optional[dict] = None

    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if event.get("type") == "assistant":
            turn = _parse_assistant_turn(event, turn_index=len(turns) + 1)
            if turn is not None:
                turns.append(turn)
        elif event.get("type") == "result":
            final = event

    if final is None:
        return None

    return AgentRunResult(
        turns=turns,
        final_text=final.get("result") or "",
        produced_files=[],  # 실행 후 워크스페이스 스캔으로 agent_runner가 채운다
        blocked_tool_uses=_parse_denials(final.get("permission_denials")),
        num_turns=final.get("num_turns"),
        cost_usd=None,  # 구독 모드는 $ 집계 대상이 아니다 (schemas.py Candidate와 동일 규칙)
        stop_reason=_stop_reason(final),
    )


def _parse_assistant_turn(event: dict, *, turn_index: int) -> Optional[AgentTurn]:
    """assistant 이벤트에서 텍스트와 tool_use 블록을 뽑아 한 턴으로 만든다.

    서브에이전트가 만든 메시지(`parent_tool_use_id`가 있음)는 건너뛴다 — 메인
    에이전트의 행동 흐름만 턴으로 세야 기록이 실제 진행 순서와 맞는다.
    """
    if event.get("parent_tool_use_id"):
        return None

    blocks = event.get("message", {}).get("content") or []
    texts: list[str] = []
    tool_uses: list[AgentToolUse] = []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "text":
            texts.append(block.get("text", ""))
        elif block.get("type") == "tool_use":
            tool_uses.append(
                AgentToolUse(tool=block.get("name", "unknown"), target=_tool_target(block.get("input")))
            )

    if not texts and not tool_uses:
        return None
    return AgentTurn(turn_index=turn_index, text="\n".join(t for t in texts if t).strip(), tool_uses=tool_uses)


def _parse_denials(denials: object) -> list[AgentToolUse]:
    """result 메시지의 permission_denials를 안전 경계가 막아낸 시도 목록으로 바꾼다.

    2026-07-27 e2e에서 확인: 에이전트는 경계 밖 도구를 실제로 시도한다(Bash로
    저장소 탐색 등). 그 시도가 거부됐다는 기록이 남아야 경계가 작동했음을
    사후에 증명할 수 있다.
    """
    if not isinstance(denials, list):
        return []
    blocked: list[AgentToolUse] = []
    for entry in denials:
        if not isinstance(entry, dict):
            continue
        blocked.append(
            AgentToolUse(tool=entry.get("tool_name", "unknown"), target=_tool_target(entry.get("tool_input")))
        )
    return blocked


def _tool_target(tool_input: object) -> Optional[str]:
    """도구 입력에서 사람이 알아볼 짧은 대상만 뽑는다(파일 경로 등).

    입력 전체를 저장하면 파일 본문까지 기록에 들어가 agent_turns.json이 비대해진다 —
    "무엇을 대상으로 했는가"만 남기고 내용은 실제 산출물 파일로 확인하게 한다.
    """
    if not isinstance(tool_input, dict):
        return None
    for key in ("file_path", "path", "notebook_path", "pattern", "command"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value[:200]
    return None


def _stop_reason(final: dict) -> str:
    """result 메시지의 subtype/is_error로 종료 사유를 판정한다.

    턴 상한 도달(`error_max_turns`)은 실패가 아니라 partial 승격 대상이다 —
    그때까지 에이전트가 만든 파일은 그대로 유효하기 때문이다.
    """
    subtype = final.get("subtype") or ""
    if "max_turns" in subtype:
        return "max_turns"
    if final.get("is_error") or subtype.startswith("error"):
        return "error"
    return "completed"


class CodexCliProvider(CliSubscriptionProvider):
    """`codex exec --json --output-last-message <file>`로 Codex CLI를 호출한다."""

    executable = "codex"

    def _invoke(self, prompt: str) -> tuple[str, Optional[int]]:
        with tempfile.TemporaryDirectory() as tmp_dir:
            last_message_path = Path(tmp_dir) / "last_message.txt"
            # 프롬프트를 위치 인자로 안 주고 stdin(input=)으로만 넘긴다 — claude와 같은
            # 이유(모듈 docstring 참고, 2026-07-13 실제 재현: 8KB대 멀티바이트 프롬프트를
            # 인자로 주면 .CMD 경유 과정에서 내용이 깨짐). codex는 "PROMPT 인자가 없으면
            # stdin 전체를 지시문으로 읽는다"는 별도 경로가 있어(claude의
            # --input-format text와 동등), 위치 인자를 그냥 생략하면 된다. input=이
            # stdin을 쓰고 자동으로 닫아줘서(EOF) "추가 입력을 더 기다리는" 옛 무한
            # 대기 문제도 여전히 안 생긴다 — stdin=subprocess.DEVNULL은 이제 불필요.
            #
            # cwd=tmp_dir로 실행한다 — claude와 같은 이유(클래스 docstring 위쪽,
            # ClaudeCliProvider._invoke() 참고): cwd를 부모 프로세스(harness) 것을 그대로
            # 물려받으면 codex도 그 디렉토리를 실제 프로젝트로 인식해서 응답에 무관한
            # 저장소 상태가 새어 들어올 수 있다. tmp_dir는 이미 last_message_path 때문에
            # 만들어져 있어 추가 비용 없음. --skip-git-repo-check는 이 tmp_dir가 git
            # 저장소가 아니라서 여전히 필요하다.
            result = subprocess.run(
                [self._resolve_executable(), "exec", "--json", "--skip-git-repo-check",
                 "--output-last-message", str(last_message_path)],
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self.timeout_sec,
                encoding="utf-8",
                cwd=tmp_dir,
            )
            if result.returncode != 0:
                raise ProviderError(f"codex CLI 종료 코드 {result.returncode}: {result.stderr.strip()}")

            if not last_message_path.exists():
                raise ProviderError("codex CLI가 최종 응답 파일(--output-last-message)을 만들지 않음")

            content = last_message_path.read_text(encoding="utf-8").strip()
            if not content:
                raise ProviderError("codex CLI의 최종 응답이 비어있음")

            tokens = _extract_codex_output_tokens(result.stdout)
            return content, tokens


def _extract_codex_output_tokens(stdout: str) -> Optional[int]:
    """codex의 JSONL 이벤트 스트림에서 turn.completed 이벤트의 output_tokens를 찾는다.

    stdout에는 JSON이 아닌 배너 줄("Reading additional input from stdin..." 등)도
    섞여 있어서, `{`로 시작하는 줄만 JSON으로 파싱을 시도한다.
    """
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "turn.completed":
            return event.get("usage", {}).get("output_tokens")
    return None
