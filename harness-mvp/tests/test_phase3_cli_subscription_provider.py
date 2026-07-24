"""Phase 3 테스트: providers/cli_subscription_provider.py (stdlib unittest).

harness-implementation-plan-ko.md Section 10을 검증한다. 실제 claude/codex CLI를
호출하면 진짜 구독 사용량이 소모되고 네트워크에 의존하게 되므로, 여기서는
`subprocess.run`을 모킹해서 파싱/에러 처리 로직만 검증한다. 실제 CLI 연동 자체는
이 기능을 만들면서 수동으로 직접 호출해 확인했다(진행상황 문서 참고).
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.schemas import ProviderConfig  # noqa: E402
from providers.base import ProviderError  # noqa: E402
from providers.cli_subscription_provider import (  # noqa: E402
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
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="claude", timeout=120.0)

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

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


if __name__ == "__main__":
    unittest.main()
