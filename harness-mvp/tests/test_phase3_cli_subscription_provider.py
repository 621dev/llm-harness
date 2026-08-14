"""Phase 3 테스트: providers/cli_subscription_provider.py (stdlib unittest).

harness-implementation-plan-ko.md Section 10을 검증한다. 실제 claude/codex CLI를
호출하면 진짜 구독 사용량이 소모되고 네트워크에 의존하게 되므로, 여기서는
`subprocess.run`을 모킹해서 파싱/에러 처리 로직만 검증한다. 실제 CLI 연동 자체는
이 기능을 만들면서 수동으로 직접 호출해 확인했다(진행상황 문서 참고).
"""
from __future__ import annotations

import shutil
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.schemas import ProviderConfig  # noqa: E402
from providers import cli_subscription_provider  # noqa: E402  (타임아웃 상수 직접 확인용)
from providers.base import ProviderError  # noqa: E402
from providers.cli_subscription_provider import (  # noqa: E402
    ClaudeAgentProvider,
    ClaudeCliProvider,
    CodexCliProvider,
    _extract_codex_output_tokens,
)


def make_config(provider_id: str) -> ProviderConfig:
    return ProviderConfig(provider_id=provider_id, model_id=provider_id, auth_mode="cli_subscription")


class ClaudeCliProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = ClaudeCliProvider(make_config("claude-cli"))
        # 테스트 환경에 claude CLI가 실제로 설치돼 있는지와 무관하게 동작하도록,
        # shutil.which()가 항상 가짜 경로를 찾은 것처럼 모킹한다 (실제 실행은 subprocess.run
        # 모킹이 대신 가로챈다). "설치 안 됨" 케이스는 이 반환값을 None으로 오버라이드해서 테스트한다.
        which_patcher = patch("providers.cli_subscription_provider.shutil.which", return_value="/fake/bin/claude")
        self.mock_which = which_patcher.start()
        self.addCleanup(which_patcher.stop)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_success_parses_result_and_tokens(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"],
            returncode=0,
            stdout='{"is_error":false,"result":"2","usage":{"output_tokens":3}}',
            stderr="",
        )

        candidate = self.provider.generate("1+1은?")

        self.assertEqual(candidate.status, "success")
        self.assertEqual(candidate.content, "2")
        self.assertEqual(candidate.tokens, 3)
        self.assertIsNone(candidate.cost_usd)  # cli_subscription 모드는 cost_usd 안 채움
        self.assertGreaterEqual(candidate.latency_ms, 0)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_is_error_response_raises(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout='{"is_error":true,"result":"Not logged in"}', stderr="",
        )

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_nonzero_exit_code_raises(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=1, stdout="", stderr="claude: command failed",
        )

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_invalid_json_raises(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout="this is not json", stderr="",
        )

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    def test_uninstalled_cli_raises_provider_error(self) -> None:
        # shutil.which()가 못 찾는 경우(설치 안 됨) — subprocess.run까지 가지도 않는다.
        self.mock_which.return_value = None

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_missing_binary_raises_provider_error(self, mock_run) -> None:
        # shutil.which()는 뭔가를 찾았지만(설치는 됐지만 예: 실행 중 삭제된 경쟁 상황),
        # 그래도 subprocess.run 자체가 FileNotFoundError를 던지면 방어적으로 처리되는지 확인.
        mock_run.side_effect = FileNotFoundError("claude not found")

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_timeout_raises_provider_error(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=420.0)

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    def test_default_timeout_covers_measured_subscription_latency(self) -> None:
        """기본 타임아웃이 **실측된 구독 호출 지연**을 덮는가 (2026-08-03).

        120초였을 때 정상 작업이 타임아웃으로 죽었다. 8차 측정(k=6, 구독 생성)의 시도당
        지연이 268~432초였고 한 시도가 구독 2회 + gemini judge 1회이므로 **구독 1회당
        200초를 넘는다.** 420초는 그 측정 12시도가 한 번도 타임아웃 없이 통과한 값이다.

        이 테스트가 지키는 것은 "420"이라는 숫자가 아니라 **실측 하한(구독 1회 200초)을
        덮는다**는 성질이다. 나중에 CLI가 빨라져 값을 낮출 때도 근거 없이는 못 내린다.
        """
        measured_single_call_floor_sec = 200.0

        self.assertGreater(
            cli_subscription_provider.DEFAULT_TIMEOUT_SEC,
            measured_single_call_floor_sec,
            "구독 CLI 단발 호출은 실측 200초를 넘는다(8차 측정). 기본 타임아웃이 그보다 "
            "낮으면 정상 작업이 실패로 바뀌고 구독 호출도 버려진다 — 실패한 시도도 "
            "한도를 소모한다.",
        )
        # 에이전트 모드보다는 짧아야 한다 — 도구를 여러 턴 호출하는 쪽이 더 오래 걸린다.
        self.assertLess(
            cli_subscription_provider.DEFAULT_TIMEOUT_SEC,
            cli_subscription_provider.DEFAULT_AGENT_TIMEOUT_SEC,
            "단발 완성이 에이전트 모드보다 긴 타임아웃을 갖는 건 앞뒤가 안 맞는다",
        )

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_prompt_passed_via_stdin_not_as_cli_argument(self, mock_run) -> None:
        """회귀 테스트: 2026-07-13 실제 fan_out_judge judge 호출(프롬프트 약 8KB)에서
        Windows .CMD 경유로 프롬프트를 커맨드라인 인자로 넘기면 멀티바이트 인코딩이
        깨지는 걸 재현했다 — stdin으로 넘기면(길이 제한 없음) 문제가 없어짐을 확인하고
        고쳤다. 프롬프트가 다시 인자 목록에 섞여 들어가지 않는지 고정해둔다."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout='{"is_error":false,"result":"2","usage":{"output_tokens":1}}', stderr="",
        )
        long_prompt = "질문: " + ("가" * 5000)

        self.provider.generate(long_prompt)

        _, kwargs = mock_run.call_args
        call_args_list = mock_run.call_args.args[0]
        self.assertNotIn(long_prompt, call_args_list)  # 프롬프트가 인자로 안 들어감
        self.assertEqual(kwargs.get("input"), long_prompt)  # stdin(input=)으로 들어감
        self.assertNotIn("stdin", kwargs)  # input=과 stdin=을 동시에 주면 subprocess가 예외를 던짐

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_runs_in_isolated_temp_dir_not_real_cwd(self, mock_run) -> None:
        """회귀 테스트: 2026-07-14 실제 hierarchical_delegation 역할 분담 검증 중 발견 —
        cwd를 안 주면 claude CLI가 harness 프로젝트를 그대로 인식해서(CLAUDE.md 자동
        탐지 + git 상태 인지) 순수 텍스트 완성이어야 할 응답에 무관한 저장소 상태가
        섞여 들어왔다(예: "git status에 cli.py 수정사항이 남아있는데..."). cwd가 실제
        프로젝트 디렉토리가 아닌 격리된 임시 디렉토리로 넘어가는지 고정해둔다."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0,
            stdout='{"is_error":false,"result":"2","usage":{"output_tokens":1}}', stderr="",
        )

        self.provider.generate("1+1은?")

        _, kwargs = mock_run.call_args
        self.assertIn("cwd", kwargs)
        self.assertNotEqual(Path(kwargs["cwd"]).resolve(), Path.cwd().resolve())


def agent_stream(*, subtype: str = "success", is_error: bool = False, num_turns: int = 2) -> str:
    """claude CLI의 stream-json(JSONL) 출력을 흉내낸다 (ADR 0007).

    실제 형식대로 배너 줄 + system/init + assistant(도구 호출 포함) + result를
    섞어서, 파서가 필요한 것만 골라내는지 확인할 수 있게 한다.
    """
    lines = [
        "Reading prompt from stdin...",  # JSON이 아닌 배너 줄
        '{"type":"system","subtype":"init","tools":["Read","Write"]}',
        '{"type":"assistant","parent_tool_use_id":null,"message":{"content":['
        '{"type":"text","text":"파일을 만들겠습니다"},'
        '{"type":"tool_use","name":"Write","input":{"file_path":"guide.md","content":"본문"}}]}}',
        '{"type":"user","message":{"content":[{"type":"tool_result","content":"ok"}]}}',
        '{"type":"assistant","parent_tool_use_id":null,"message":{"content":['
        '{"type":"text","text":"완료했습니다"}]}}',
        '{"type":"result","subtype":"%s","is_error":%s,"num_turns":%d,"result":"작업 완료"}'
        % (subtype, "true" if is_error else "false", num_turns),
    ]
    return "\n".join(lines)


class ClaudeAgentProviderTest(unittest.TestCase):
    """agentic_task용 에이전트 모드 (ADR 0007). 실제 CLI는 호출하지 않는다."""

    def setUp(self) -> None:
        self.provider = ClaudeAgentProvider(make_config("claude-cli"))
        which_patcher = patch("providers.cli_subscription_provider.shutil.which", return_value="/fake/bin/claude")
        which_patcher.start()
        self.addCleanup(which_patcher.stop)
        self.workspace = Path(tempfile.mkdtemp(prefix="agent-ws-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

    def run_agent(self, mock_run, stdout: str, *, returncode: int = 0):
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=returncode, stdout=stdout, stderr=""
        )
        return self.provider.run_agent("학습 자료를 만들어줘", self.workspace, max_turns=5)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_parses_turns_tool_uses_and_result(self, mock_run) -> None:
        result = self.run_agent(mock_run, agent_stream())

        self.assertEqual(result.stop_reason, "completed")
        self.assertEqual(result.final_text, "작업 완료")
        self.assertEqual(result.num_turns, 2)
        self.assertEqual([t.turn_index for t in result.turns], [1, 2])
        self.assertEqual(result.turns[0].tool_uses[0].tool, "Write")
        self.assertEqual(result.turns[0].tool_uses[0].target, "guide.md")
        self.assertEqual(result.turns[1].tool_uses, [])  # 텍스트만 있는 턴
        self.assertIsNone(result.cost_usd)  # 구독 모드는 $ 집계 대상 아님

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_tool_target_excludes_file_body(self, mock_run) -> None:
        """도구 입력 전체가 아니라 대상(파일 경로)만 기록해야 한다 — 파일 본문까지
        넣으면 agent_turns.json이 산출물 사본으로 비대해진다."""
        result = self.run_agent(mock_run, agent_stream())

        self.assertNotIn("본문", str(result.turns[0].tool_uses[0].model_dump()))

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_max_turns_is_not_an_exception(self, mock_run) -> None:
        """턴 상한 도달 시 CLI는 오류로 종료하지만, 그때까지 만든 파일은 유효하므로
        예외가 아니라 stop_reason으로 돌려줘야 한다(partial 승격 대상)."""
        result = self.run_agent(
            mock_run, agent_stream(subtype="error_max_turns", is_error=True), returncode=1
        )

        self.assertEqual(result.stop_reason, "max_turns")
        self.assertEqual(result.turns[0].tool_uses[0].tool, "Write")  # 기록은 그대로 남음

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_agent_error_reported_as_stop_reason(self, mock_run) -> None:
        result = self.run_agent(mock_run, agent_stream(subtype="error_during_execution", is_error=True))

        self.assertEqual(result.stop_reason, "error")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_missing_result_message_raises(self, mock_run) -> None:
        """result 메시지가 없다 = 에이전트가 시작조차 못 했다는 뜻이라 진짜 실패다."""
        with self.assertRaises(ProviderError):
            self.run_agent(mock_run, '{"type":"system","subtype":"init"}', returncode=1)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_safety_boundaries_passed_as_cli_arguments(self, mock_run) -> None:
        """회귀 테스트(2026-07-27 첫 e2e에서 실제로 뚫린 것): 안전 경계는 세 인자가
        **전부** 있어야 강제된다. 처음엔 `--allowedTools "Read,Write,Edit"`만 걸었는데
        에이전트가 Bash로 저장소를 뒤지고 워크스페이스 밖 CLAUDE.md를 읽었다 —
        print 모드에서 --allowedTools는 사전 승인일 뿐 제한이 아니었다.

        하나라도 빠지면 경계가 무너지므로 셋 다 고정한다:
        1. --permission-mode dontAsk (허용 규칙에 없으면 거부)
        2. 경로 스코프 allow 규칙 (도구 이름만 쓰면 cwd 밖도 열림)
        3. --disallowedTools bare 이름 (위험 도구를 컨텍스트에서 제거)
        """
        self.run_agent(mock_run, agent_stream())

        args = mock_run.call_args.args[0]
        kwargs = mock_run.call_args.kwargs
        self.assertEqual(Path(kwargs["cwd"]).resolve(), self.workspace.resolve())
        self.assertNotIn("--add-dir", args)  # 워크스페이스 밖 접근 경로를 열지 않음

        # 1) 기본 거부 모드 — 이게 없으면 나머지 두 개도 무의미하다
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")

        # 2) 허용 규칙은 반드시 경로로 스코프돼야 한다
        allowed = args[args.index("--allowedTools") + 1]
        self.assertEqual(allowed, "Read(./**),Write(./**),Edit(./**)")
        for rule in allowed.split(","):
            self.assertIn("(./**)", rule)  # 도구 이름만 있는 무제한 규칙 금지

        # 3) 위험 도구는 컨텍스트에서 제거
        disallowed = args[args.index("--disallowedTools") + 1].split(",")
        for tool in ("Bash", "Glob", "Grep", "WebFetch", "WebSearch", "Task"):
            self.assertIn(tool, disallowed)

        self.assertIn("--max-turns", args)
        self.assertEqual(args[args.index("--max-turns") + 1], "5")
        self.assertIn("stream-json", args)  # 턴별 관측이 가능한 형식
        self.assertEqual(kwargs.get("input"), "학습 자료를 만들어줘")  # 프롬프트는 stdin

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_system_prompt_is_appended_not_replaced(self, mock_run) -> None:
        """에이전트에 환경을 알려주되(2026-07-29) 기본 시스템 프롬프트를 지우지 않는다.

        `--system-prompt`(교체)를 쓰면 CLI 기본 프롬프트의 도구 사용 규칙까지 사라진다.
        경계를 인자로 강제하고 있긴 하지만, 모델이 도구를 어떻게 쓰는지에 대한 기본
        지침을 굳이 없앨 이유가 없다.
        """
        self.run_agent(mock_run, agent_stream())

        args = mock_run.call_args.args[0]
        self.assertIn("--append-system-prompt", args)
        self.assertNotIn("--system-prompt", args)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_default_system_prompt_states_the_actual_boundary(self, mock_run) -> None:
        """주입 내용이 실제 경계와 어긋나면 에이전트를 오히려 헷갈리게 한다.

        차단 도구 목록을 프롬프트에 문장으로 적어두는 방식이라, `--disallowedTools`를
        바꾸고 프롬프트를 안 바꾸면 거짓말이 된다. 그 불일치를 여기서 잡는다.
        """
        self.run_agent(mock_run, agent_stream())

        args = mock_run.call_args.args[0]
        injected = args[args.index("--append-system-prompt") + 1]
        disallowed = args[args.index("--disallowedTools") + 1].split(",")

        for tool in disallowed:
            if tool == "NotebookEdit":
                continue  # 문장에 일일이 안 적음 — 파일 도구 계열이라 혼선 여지가 없다
            with self.subTest(tool=tool):
                self.assertIn(tool, injected)
        # 산출물이 파일이라는 것도 반드시 알려야 한다(응답 요약만 내놓는 실측 사례 있음)
        self.assertIn("파일", injected)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_injection_can_be_turned_off(self, mock_run) -> None:
        """도메인이 자기 지시문을 직접 쓰고 싶을 때 기본값을 끌 수 있어야 한다."""
        mock_run.return_value = subprocess.CompletedProcess(
            args=["claude"], returncode=0, stdout=agent_stream(), stderr=""
        )

        self.provider.run_agent(
            "학습 자료를 만들어줘", self.workspace, max_turns=5, system_prompt_append=""
        )

        self.assertNotIn("--append-system-prompt", mock_run.call_args.args[0])

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_injection_does_not_widen_the_boundary(self, mock_run) -> None:
        """튜닝을 추가해도 경계는 그대로여야 한다 — 이게 이 변경의 가장 큰 위험이다.

        `--add-dir`(접근 범위 확대)이나 워크스페이스에 파일을 써넣는 방식(산출물 오염)을
        쓰지 않고 시스템 프롬프트만으로 알려주는 걸 고정한다.
        """
        before = sorted(p.name for p in self.workspace.iterdir())

        self.run_agent(mock_run, agent_stream())

        args = mock_run.call_args.args[0]
        self.assertNotIn("--add-dir", args)
        self.assertEqual(args[args.index("--permission-mode") + 1], "dontAsk")
        self.assertEqual(args[args.index("--allowedTools") + 1], "Read(./**),Write(./**),Edit(./**)")
        # 하네스가 워크스페이스에 스킬/설정 파일을 심지 않았는지 — 심으면 그 파일이
        # produced_files로 수집돼 사용자 산출물에 섞인다
        self.assertEqual(sorted(p.name for p in self.workspace.iterdir()), before)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_records_blocked_tool_uses_from_permission_denials(self, mock_run) -> None:
        """경계가 막아낸 시도를 감사 증거로 남기는지 확인 — 2026-07-27 e2e에서
        에이전트가 실제로 Bash를 시도했다(경계 밖 도구를 노리는 건 예외가 아니라
        관측된 행동이다). "설정돼 있다"가 아니라 "작동했다"의 근거가 된다."""
        stream = agent_stream().replace(
            '"result":"작업 완료"',
            '"result":"작업 완료","permission_denials":['
            '{"tool_name":"Bash","tool_input":{"command":"find / -name secrets"}}]',
        )

        result = self.run_agent(mock_run, stream)

        self.assertEqual(len(result.blocked_tool_uses), 1)
        self.assertEqual(result.blocked_tool_uses[0].tool, "Bash")
        self.assertIn("find", result.blocked_tool_uses[0].target)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_no_denials_means_empty_blocked_list(self, mock_run) -> None:
        result = self.run_agent(mock_run, agent_stream())

        self.assertEqual(result.blocked_tool_uses, [])

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_timeout_raises_provider_error(self, mock_run) -> None:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=600.0)

        with self.assertRaises(ProviderError):
            self.provider.run_agent("작업", self.workspace, max_turns=5)

    def test_still_usable_as_plain_provider(self) -> None:
        """같은 바이너리의 두 모드 — generate()(단발 완성)도 그대로 동작해야 한다."""
        self.assertTrue(callable(self.provider.generate))


class CodexCliProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = CodexCliProvider(make_config("codex-cli"))
        which_patcher = patch("providers.cli_subscription_provider.shutil.which", return_value="/fake/bin/codex")
        self.mock_which = which_patcher.start()
        self.addCleanup(which_patcher.stop)

    def _fake_run_writing_last_message(self, content: str, *, output_tokens: int = 5):
        """--output-last-message 파일에 실제로 써주는 subprocess.run 대역."""

        def _side_effect(cmd, **kwargs):
            last_message_path = Path(cmd[cmd.index("--output-last-message") + 1])
            last_message_path.write_text(content, encoding="utf-8")
            stdout = (
                'Reading additional input from stdin...\n'
                '{"type":"thread.started","thread_id":"t-1"}\n'
                f'{{"type":"item.completed","item":{{"type":"agent_message","text":"{content}"}}}}\n'
                f'{{"type":"turn.completed","usage":{{"output_tokens":{output_tokens}}}}}\n'
            )
            return subprocess.CompletedProcess(args=cmd, returncode=0, stdout=stdout, stderr="")

        return _side_effect

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_success_reads_last_message_file_and_tokens(self, mock_run) -> None:
        mock_run.side_effect = self._fake_run_writing_last_message("2", output_tokens=5)

        candidate = self.provider.generate("1+1은?")

        self.assertEqual(candidate.status, "success")
        self.assertEqual(candidate.content, "2")
        self.assertEqual(candidate.tokens, 5)
        self.assertIsNone(candidate.cost_usd)

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_nonzero_exit_code_raises(self, mock_run) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["codex"], returncode=1, stdout="", stderr="codex: auth error",
        )

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_missing_last_message_file_raises(self, mock_run) -> None:
        # returncode=0이지만 --output-last-message 파일을 안 쓴 상황을 흉내낸다.
        mock_run.return_value = subprocess.CompletedProcess(args=["codex"], returncode=0, stdout="", stderr="")

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_empty_last_message_raises(self, mock_run) -> None:
        mock_run.side_effect = self._fake_run_writing_last_message("   ", output_tokens=0)

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    def test_uninstalled_cli_raises_provider_error(self) -> None:
        self.mock_which.return_value = None

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_missing_binary_raises_provider_error(self, mock_run) -> None:
        mock_run.side_effect = FileNotFoundError("codex not found")

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_prompt_passed_via_stdin_not_as_cli_argument(self, mock_run) -> None:
        """회귀 테스트: 2026-07-13 실제 codex CLI로 14KB대 멀티바이트 프롬프트를
        위치 인자로 넘겼더니 응답이 완전히 깨지는(mojibake) 걸 재현했다(claude와
        같은 Windows .CMD 경유 인자 손상). stdin(input=)으로 넘기면 문제가
        없어짐을 확인하고 고쳤다 — 프롬프트가 다시 인자 목록에 섞여 들어가지
        않는지, stdin=DEVNULL(예전 무한 대기 방지용, 이제 불필요)도 안 남아있는지
        고정해둔다."""
        mock_run.side_effect = self._fake_run_writing_last_message("2", output_tokens=1)
        long_prompt = "질문: " + ("가" * 5000)

        self.provider.generate(long_prompt)

        call_args_list = mock_run.call_args.args[0]
        _, kwargs = mock_run.call_args
        self.assertNotIn(long_prompt, call_args_list)  # 프롬프트가 인자로 안 들어감
        self.assertEqual(kwargs.get("input"), long_prompt)  # stdin(input=)으로 들어감
        self.assertNotIn("stdin", kwargs)  # input=과 stdin=을 동시에 주면 subprocess가 예외를 던짐

    @patch("providers.cli_subscription_provider.subprocess.run")
    def test_runs_in_isolated_temp_dir_not_real_cwd(self, mock_run) -> None:
        """claude와 같은 이유의 회귀 테스트(위 ClaudeCliProviderTest 참고) — codex도
        cwd를 안 주면 부모 프로세스(harness) 디렉토리를 그대로 물려받아 실제 저장소를
        인식할 위험이 있다. cwd가 격리된 임시 디렉토리로 넘어가는지 고정해둔다."""
        mock_run.side_effect = self._fake_run_writing_last_message("2", output_tokens=1)

        self.provider.generate("1+1은?")

        _, kwargs = mock_run.call_args
        self.assertIn("cwd", kwargs)
        self.assertNotEqual(Path(kwargs["cwd"]).resolve(), Path.cwd().resolve())


class ExtractCodexOutputTokensTest(unittest.TestCase):
    def test_finds_output_tokens_in_turn_completed_event(self) -> None:
        stdout = (
            "Reading additional input from stdin...\n"
            '{"type":"thread.started","thread_id":"t-1"}\n'
            '{"type":"turn.completed","usage":{"output_tokens":42}}\n'
        )
        self.assertEqual(_extract_codex_output_tokens(stdout), 42)

    def test_returns_none_when_no_turn_completed_event(self) -> None:
        stdout = '{"type":"thread.started","thread_id":"t-1"}\n'
        self.assertIsNone(_extract_codex_output_tokens(stdout))

    def test_ignores_non_json_lines(self) -> None:
        stdout = "not json at all\n{broken json\n" '{"type":"turn.completed","usage":{"output_tokens":7}}\n'
        self.assertEqual(_extract_codex_output_tokens(stdout), 7)


class CliFailureMessageTest(unittest.TestCase):
    """CLI가 0이 아닌 코드로 끝났을 때 원인을 잃지 않는가 (2026-08-13, 실측에서 나온 수정).

    **고치기 전 동작**: 종료 코드 검사가 `stderr`만 실었다. claude CLI의 OAuth 세션이
    만료됐을 때 하네스가 보고한 건 `claude CLI 종료 코드 1: ` 뿐이었고 — 뒤가 비었다 —
    정작 원인인 `"Failed to authenticate: OAuth session expired..."`는 **stdout의
    JSON `result` 필드**에 있었다. `is_error` 판정이 그걸 읽지만 **종료 코드 검사가
    먼저라 거기까지 도달하지 못했다.** 순서 때문에 가장 쓸모 있는 정보를 버렸다.

    게다가 인증 실패로 분류되지 않아 **재시도로 구독 호출을 한 번 더 낭비했다**
    (실제로 "1차 … 2차"로 두 번 나갔다).
    """

    def _result(self, *, returncode: int = 1, stdout: str = "", stderr: str = ""):
        return subprocess.CompletedProcess(args=["claude"], returncode=returncode,
                                           stdout=stdout, stderr=stderr)

    def test_message_comes_from_stdout_json_when_stderr_is_empty(self) -> None:
        """핵심 회귀 방지: 이 조합이 실제로 겪은 조합이다(stderr 빈 채 stdout에 원인)."""
        payload = json.dumps({
            "type": "result", "is_error": True,
            "result": "Failed to authenticate: OAuth session expired and could not be refreshed",
        })

        error = cli_subscription_provider._cli_failure("claude", self._result(stdout=payload))

        self.assertIn("OAuth session expired", str(error))
        self.assertNotIn("종료 코드 1: —", str(error))  # 메시지 자리가 비지 않는다

    def test_auth_failure_is_flagged_so_it_is_not_retried(self) -> None:
        """성공할 수 없는 호출에 구독 한도를 한 번 더 쓰지 않는다."""
        payload = json.dumps({"result": "Failed to authenticate: OAuth session expired"})

        error = cli_subscription_provider._cli_failure("claude", self._result(stdout=payload))

        self.assertTrue(error.is_auth_error)
        self.assertFalse(error.is_retryable)
        # **`claude login`이 아니라 `claude auth login`이다.** 첫 구현이 틀린 명령을
        # 안내해서 사용자가 그대로 따라 실패했다 — 안내 문구도 검증 대상이다.
        self.assertIn("claude auth login", str(error))

    def test_stderr_is_kept_when_present(self) -> None:
        """stdout에서 찾았다고 stderr를 버리지 않는다 — 둘 다 단서일 수 있다."""
        payload = json.dumps({"result": "무언가 실패"})

        error = cli_subscription_provider._cli_failure(
            "codex", self._result(stdout=payload, stderr="stderr 쪽 단서")
        )

        self.assertIn("stderr 쪽 단서", str(error))
        self.assertIn("무언가 실패", str(error))

    def test_ordinary_failure_is_still_retryable(self) -> None:
        """인증이 아닌 실패는 기존대로 재시도한다(동작 변경 아님)."""
        error = cli_subscription_provider._cli_failure("codex", self._result(stderr="일시적 네트워크 오류"))

        self.assertFalse(error.is_auth_error)
        self.assertTrue(error.is_retryable)

    def test_totally_silent_failure_says_so_instead_of_empty(self) -> None:
        """아무 메시지도 없으면 '없다'고 말한다 — 빈 문자열은 사람을 헤매게 한다."""
        error = cli_subscription_provider._cli_failure("claude", self._result())

        self.assertIn("아무 메시지도 남기지 않았다", str(error))

    def test_codex_jsonl_stream_error_line_is_found(self) -> None:
        """codex는 JSONL 스트림이라 모양이 다르다 — 배너 줄이 섞여도 찾아야 한다."""
        stdout = "\n".join([
            "Reading additional input from stdin...",   # codex가 실제로 찍는 배너 줄
            '{"error": "codex 쪽 원인"}',
        ])

        error = cli_subscription_provider._cli_failure("codex", self._result(stdout=stdout))

        self.assertIn("codex 쪽 원인", str(error))


if __name__ == "__main__":
    unittest.main()
