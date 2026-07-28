"""agentic_task 패턴 통합 테스트 (ADR 0007) — 자율 에이전트를 감싸는 하네스.

다른 세 패턴과 검증 포인트가 다르다. 저 패턴들은 "텍스트가 잘 나오는가"를 보지만,
여기서는 **하네스가 에이전트를 제대로 감싸고 있는가**를 본다:

- 승인 없이는 에이전트가 아예 실행되지 않는가 (되돌리기 어려운 부수 효과 게이트)
- 에이전트가 만든 파일이 격리된 워크스페이스 안에만 생기는가
- 에이전트가 무엇을 했는지 기록이 남는가 (agent_turns.json)
- **에이전트가 만든 파일도 Safety 스캔을 거치는가** (final.md 텍스트만 보면
  이 패턴에서 Safety가 사실상 무력해진다 — 회귀 방지 핵심)
- 턴 상한에 걸려도 그때까지 만든 결과를 버리지 않는가 (partial 승격)

실제 claude CLI는 절대 호출하지 않는다 — provider 자리에 파일을 쓰는 대역을 넣어
"에이전트가 도구를 써서 파일을 만들었다"는 상황만 재현한다.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import agent_runner, orchestrator, run_store  # noqa: E402
from harness.schemas import AgentRunResult, AgentToolUse, AgentTurn, TaskInput  # noqa: E402  (AgentToolUse: 차단 기록 테스트에 사용)

_PROMPT = "리눅스 학습 자료를 주제별 마크다운 파일로 만들어줘. 각 파일에 예시를 포함해줘."


def make_task(task_id: str, *, extra_constraints: list[str] | None = None) -> TaskInput:
    return TaskInput(
        task_id=task_id,
        prompt=_PROMPT,
        constraints=["team_pattern:agentic_task"] + (extra_constraints or []),
    )


class FakeAgentProvider:
    """실제 CLI 대신, 워크스페이스에 파일을 쓰고 턴 기록을 돌려주는 대역.

    `files`로 만들 파일(이름→내용)을, `stop_reason`으로 종료 사유를 지정한다.
    `raises`가 있으면 그 예외를 던진다(에이전트가 시작조차 못 한 경우).
    """

    def __init__(
        self,
        files: dict[str, str],
        *,
        stop_reason: str = "completed",
        raises: Exception | None = None,
        blocked: list[AgentToolUse] | None = None,
    ):
        self.files = files
        self.stop_reason = stop_reason
        self.raises = raises
        self.blocked = blocked or []
        self.called_with_workspace: Path | None = None
        self.called_with_max_turns: int | None = None

    def run_agent(self, prompt: str, workspace: Path, *, max_turns: int) -> AgentRunResult:
        if self.raises is not None:
            raise self.raises
        self.called_with_workspace = workspace
        self.called_with_max_turns = max_turns

        turns = []
        for index, (name, content) in enumerate(self.files.items(), start=1):
            path = workspace / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
            turns.append(
                AgentTurn(
                    turn_index=index,
                    text=f"{name} 작성",
                    tool_uses=[AgentToolUse(tool="Write", target=name)],
                )
            )
        return AgentRunResult(
            turns=turns,
            final_text="요청하신 학습 자료를 작성했습니다.",
            blocked_tool_uses=self.blocked,
            num_turns=len(turns),
            latency_ms=1234,
            cost_usd=None,
            stop_reason=self.stop_reason,
        )


def providers_with(agent: FakeAgentProvider) -> dict:
    return {orchestrator.AGENT_PROVIDER_KEY: agent}


class AgenticTaskIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def approve_and_run(self, task_id: str, agent: FakeAgentProvider):
        """이 패턴은 항상 승인 대기부터 시작한다 — 승인까지 마친 뒤의 결과를 돌려준다."""
        orchestrator.run(make_task(task_id), providers_with(agent), root=self.tmp_dir)
        return orchestrator.resume(f"run-{task_id}", "approved", providers_with(agent), root=self.tmp_dir)

    def test_requires_human_approval_before_agent_runs(self) -> None:
        """되돌리기 어려운 부수 효과(파일 생성)가 있으므로 승인 전에는 에이전트가
        아예 호출되면 안 된다 — 워크스페이스조차 만들어지지 않아야 한다."""
        agent = FakeAgentProvider({"guide.md": "본문"})

        pending = orchestrator.run(make_task("agent-approval"), providers_with(agent), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-agent-approval"
        self.assertEqual(pending.status, "warning")
        self.assertEqual(run_store.read_json(run_dir, "plan.json")["risk_level"], "high")
        self.assertEqual(run_store.read_json(run_dir, "approval.json")["status"], "pending")
        self.assertIsNone(agent.called_with_workspace)  # 에이전트 미실행
        self.assertFalse(agent_runner.agent_workspace(run_dir).exists())
        self.assertFalse((run_dir / "final.md").exists())

    def test_rejected_run_never_creates_files(self) -> None:
        agent = FakeAgentProvider({"guide.md": "본문"})
        orchestrator.run(make_task("agent-reject"), providers_with(agent), root=self.tmp_dir)

        rejected = orchestrator.resume("run-agent-reject", "rejected", providers_with(agent), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-agent-reject"
        self.assertEqual(rejected.status, "error")
        self.assertIsNone(agent.called_with_workspace)
        self.assertFalse(agent_runner.agent_workspace(run_dir).exists())

    def test_approved_run_creates_files_in_isolated_workspace(self) -> None:
        agent = FakeAgentProvider({"process.md": "프로세스 관리", "network.md": "네트워크 기초"})

        observation = self.approve_and_run("agent-success", agent)

        run_dir = self.tmp_dir / "run-agent-success"
        workspace = agent_runner.agent_workspace(run_dir)
        self.assertEqual(observation.status, "success")
        # 파일은 run 디렉토리 안 워크스페이스에만 생긴다(사용자 프로젝트 밖).
        self.assertEqual(agent.called_with_workspace.resolve(), workspace.resolve())
        self.assertTrue((workspace / "process.md").exists())
        self.assertTrue((workspace / "network.md").exists())
        # final.md는 산출물 자체가 아니라 "무엇을 만들었는지" 보고서다.
        final = run_store.read_markdown(run_dir, "final.md")
        self.assertIn("process.md", final)
        self.assertIn("network.md", final)
        self.assertEqual(run_store.read_json(run_dir, "errors.json"), [])

    def test_records_agent_actions_in_agent_turns_json(self) -> None:
        """감쌀 대상의 내부를 관측 못 하면 감싼다고 할 수 없다 — 턴별 도구 호출 기록."""
        agent = FakeAgentProvider({"a.md": "A", "b.md": "B"})

        self.approve_and_run("agent-turns", agent)

        run_dir = self.tmp_dir / "run-agent-turns"
        turns = run_store.read_json(run_dir, "agent_turns.json")
        self.assertEqual(len(turns), 2)
        self.assertEqual(turns[0]["tool_uses"][0]["tool"], "Write")
        self.assertEqual(sorted(t["tool_uses"][0]["target"] for t in turns), ["a.md", "b.md"])

    def test_produced_files_come_from_filesystem_not_agent_report(self) -> None:
        """에이전트 자기 보고가 아니라 실제 파일 시스템을 신뢰하는지 확인 —
        대역은 AgentRunResult.produced_files를 비워둔 채 파일만 쓴다."""
        agent = FakeAgentProvider({"nested/deep.md": "중첩된 파일"})

        self.approve_and_run("agent-scan", agent)

        run_dir = self.tmp_dir / "run-agent-scan"
        self.assertIn("nested/deep.md", run_store.read_markdown(run_dir, "final.md"))

    def test_generated_file_with_secret_goes_to_safety_review(self) -> None:
        """회귀 방지 핵심: 이 패턴의 진짜 산출물은 파일이므로, final.md 텍스트만
        스캔하면 Safety가 무력해진다. 파일 안의 비밀정보를 잡아내고 final.md
        공개를 막아야 한다."""
        agent = FakeAgentProvider({"config.md": "예시 키: sk-abcdefghijklmnopqrstuvwxyz"})

        observation = self.approve_and_run("agent-unsafe", agent)

        run_dir = self.tmp_dir / "run-agent-unsafe"
        self.assertEqual(observation.status, "warning")  # 검토 대기
        self.assertFalse((run_dir / "final.md").exists())  # 공개 차단
        self.assertEqual(run_store.read_json(run_dir, "safety_review.json")["status"], "pending")
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any(e["stage"] == "safety" for e in errors))

    def test_clean_files_pass_safety(self) -> None:
        """반대 방향 확인 — 멀쩡한 파일은 추가 스캔 때문에 오탐되면 안 된다."""
        agent = FakeAgentProvider({"guide.md": "정상적인 학습 자료 본문"})

        observation = self.approve_and_run("agent-safe", agent)

        self.assertEqual(observation.status, "success")
        self.assertTrue((self.tmp_dir / "run-agent-safe" / "final.md").exists())

    def test_blocked_tool_uses_are_reported_without_failing_the_run(self) -> None:
        """경계가 막아낸 시도는 정상 방어이므로 run을 실패시키지 않지만, 반드시
        errors.json과 보고서에 남는다 — 에이전트가 반복적으로 경계 밖을 노리는
        패턴은 사람이 봐야 할 신호다(2026-07-27 e2e에서 실제 관측된 행동)."""
        agent = FakeAgentProvider(
            {"guide.md": "본문"},
            blocked=[AgentToolUse(tool="Bash", target="find / -name secrets")],
        )

        observation = self.approve_and_run("agent-blocked", agent)

        run_dir = self.tmp_dir / "run-agent-blocked"
        self.assertEqual(observation.status, "warning")  # 경고로 남되 산출물은 유효
        self.assertTrue((run_dir / "final.md").exists())
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any("안전 경계가 차단한" in e["message"] and "Bash" in e["message"] for e in errors))
        self.assertIn("Bash", run_store.read_markdown(run_dir, "final.md"))

    def test_max_turns_promotes_partial_result(self) -> None:
        """턴 상한에 걸려도 그때까지 만든 파일은 버리지 않는다(체인 partial 승격과 동일)."""
        agent = FakeAgentProvider({"partial.md": "쓰다 만 자료"}, stop_reason="max_turns")

        observation = self.approve_and_run("agent-maxturns", agent)

        run_dir = self.tmp_dir / "run-agent-maxturns"
        self.assertEqual(observation.status, "warning")
        final = run_store.read_markdown(run_dir, "final.md")
        self.assertTrue(final.startswith("(partial)"))
        self.assertIn("partial.md", final)
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any("턴 상한" in e["message"] for e in errors))
        self.assertTrue((agent_runner.agent_workspace(run_dir) / "partial.md").exists())

    def test_agent_error_with_no_files_ends_without_output(self) -> None:
        agent = FakeAgentProvider({}, stop_reason="error")

        observation = self.approve_and_run("agent-empty-error", agent)

        run_dir = self.tmp_dir / "run-agent-empty-error"
        self.assertEqual(observation.status, "error")
        self.assertFalse((run_dir / "final.md").exists())
        self.assertGreaterEqual(len(run_store.read_json(run_dir, "errors.json")), 1)

    def test_provider_failure_is_recorded_without_retry(self) -> None:
        """에이전트가 시작조차 못 한 경우 — 재시도하지 않는다(부분적으로 파일을
        쓰다 만 상태에서 재실행하면 같은 작업을 두 번 하게 된다)."""
        agent = FakeAgentProvider({}, raises=RuntimeError("claude CLI를 찾을 수 없다"))

        observation = self.approve_and_run("agent-provider-fail", agent)

        run_dir = self.tmp_dir / "run-agent-provider-fail"
        self.assertEqual(observation.status, "error")
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any("에이전트 실행 실패" in e["message"] for e in errors))

    def test_missing_agent_provider_raises(self) -> None:
        with self.assertRaises(ValueError):
            orchestrator.run(
                make_task("agent-no-provider", extra_constraints=["risk_level:medium"]), {}, root=self.tmp_dir
            )

    def test_max_turns_limit_passed_to_provider(self) -> None:
        agent = FakeAgentProvider({"a.md": "A"})

        self.approve_and_run("agent-turn-limit", agent)

        self.assertEqual(agent.called_with_max_turns, orchestrator.MAX_AGENT_TURNS)

    def test_agent_provider_excluded_from_candidate_providers(self) -> None:
        """도구 사용 권한을 가진 provider가 일반 텍스트 생성 자리에 섞이면 안 된다."""
        agent = FakeAgentProvider({"a.md": "A"})

        candidates = orchestrator._candidate_providers({orchestrator.AGENT_PROVIDER_KEY: agent, "plain": agent})

        self.assertEqual(list(candidates), ["plain"])


class ListProducedFilesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.workspace = Path(tempfile.mkdtemp(prefix="agent-ws-"))
        self.addCleanup(shutil.rmtree, self.workspace, ignore_errors=True)

    def test_lists_nested_files_as_relative_posix_paths(self) -> None:
        (self.workspace / "docs").mkdir()
        (self.workspace / "docs" / "guide.md").write_text("본문", encoding="utf-8")
        (self.workspace / "top.md").write_text("본문", encoding="utf-8")

        self.assertEqual(agent_runner.list_produced_files(self.workspace), ["docs/guide.md", "top.md"])

    def test_ignores_tool_artifacts(self) -> None:
        (self.workspace / ".claude").mkdir()
        (self.workspace / ".claude" / "settings.json").write_text("{}", encoding="utf-8")
        (self.workspace / "real.md").write_text("본문", encoding="utf-8")

        self.assertEqual(agent_runner.list_produced_files(self.workspace), ["real.md"])

    def test_workspace_path_is_resolved(self) -> None:
        """회귀 테스트(2026-07-27 경계 검증 중 실측): 워크스페이스 경로가 Windows
        8.3 단축 경로나 심볼릭 링크로 들어오면, CLI가 정규화한 cwd와 에이전트가
        만든 절대경로가 문자열로 안 맞아 **정상적인 안쪽 쓰기까지 거부된다**.
        경계가 뚫리는 게 아니라 반대로 과차단되는 실패라 눈치채기 어렵다."""
        run_dir = self.workspace / "run"

        resolved = agent_runner.agent_workspace(run_dir)

        self.assertEqual(resolved, resolved.resolve())

    def test_missing_workspace_returns_empty(self) -> None:
        self.assertEqual(agent_runner.list_produced_files(self.workspace / "does-not-exist"), [])

    def test_read_produced_texts_skips_unreadable_files(self) -> None:
        (self.workspace / "text.md").write_text("정상 텍스트", encoding="utf-8")
        (self.workspace / "binary.bin").write_bytes(b"\xff\xfe\x00\x01")

        texts = agent_runner.read_produced_texts(self.workspace, ["text.md", "binary.bin", "gone.md"])

        self.assertEqual(texts, ["정상 텍스트"])


if __name__ == "__main__":
    unittest.main()
