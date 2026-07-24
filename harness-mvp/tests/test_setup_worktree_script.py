"""scripts/setup_worktree.py 테스트 (stdlib unittest).

새 도메인 worktree에 sparse-checkout을 자동 적용하는 스크립트(2026-07-24, "워크트리
관리 자동화" 사용자 요청 — 지금까지 새 worktree를 만들 때마다 손으로 치던
`git sparse-checkout init/set`을 잊어버리면 그 worktree가 저장소 전체를 다 보게
되는 위험이 있었다)를 검증한다. 실제 git 저장소를 만들지 않고 `subprocess.run`을
모킹해서 테스트한다(작업 규칙: 자동 테스트는 실제 CLI 호출 금지).
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import setup_worktree  # noqa: E402


def _worktree_list_output(main_path: str, worktree_path: str, branch: str) -> str:
    return (
        f"worktree {main_path}\n"
        "HEAD 79836bc85fc07471a1ee2fb0116448ba8a6f810d\n"
        "branch refs/heads/main\n"
        "\n"
        f"worktree {worktree_path}\n"
        "HEAD fc7b976fd157b0067c4eb93a1c3a5cc1f1552486\n"
        f"branch refs/heads/{branch}\n"
    )


class ApplySparseCheckoutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.main_dir = Path(tempfile.mkdtemp(prefix="setup-worktree-main-"))
        self.worktree_dir = Path(tempfile.mkdtemp(prefix="setup-worktree-wt-"))
        self.addCleanup(lambda: shutil.rmtree(self.main_dir, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self.worktree_dir, ignore_errors=True))
        (self.worktree_dir / "domains" / "cloud-ops").mkdir(parents=True)

    @patch("setup_worktree.subprocess.run")
    def test_applies_sparse_checkout_from_domain_worktree(self, mock_run: MagicMock) -> None:
        list_result = subprocess.CompletedProcess(
            args=["git", "worktree", "list", "--porcelain"],
            returncode=0,
            stdout=_worktree_list_output(str(self.main_dir), str(self.worktree_dir), "claude/cloud-ops-034a88"),
        )
        mock_run.side_effect = [list_result, MagicMock(returncode=0), MagicMock(returncode=0)]

        setup_worktree.apply_sparse_checkout("cloud-ops", cwd=self.worktree_dir)

        init_call, set_call = mock_run.call_args_list[1], mock_run.call_args_list[2]
        self.assertEqual(init_call.args[0], ["git", "sparse-checkout", "init", "--cone"])
        self.assertEqual(
            set_call.args[0],
            ["git", "sparse-checkout", "set", "harness-mvp", "docs", "domains/cloud-ops"],
        )

    @patch("setup_worktree.subprocess.run")
    def test_refuses_on_main_worktree(self, mock_run: MagicMock) -> None:
        (self.main_dir / "domains" / "cloud-ops").mkdir(parents=True)
        list_result = subprocess.CompletedProcess(
            args=["git", "worktree", "list", "--porcelain"],
            returncode=0,
            stdout=_worktree_list_output(str(self.main_dir), str(self.worktree_dir), "claude/cloud-ops-034a88"),
        )
        mock_run.return_value = list_result

        with self.assertRaises(setup_worktree.NotAWorktreeError):
            setup_worktree.apply_sparse_checkout("cloud-ops", cwd=self.main_dir)

        # sparse-checkout init/set을 실제로 호출하면 안 된다 — worktree list 조회 1회만.
        self.assertEqual(mock_run.call_count, 1)

    @patch("setup_worktree.subprocess.run")
    def test_missing_domain_folder_raises_without_calling_git(self, mock_run: MagicMock) -> None:
        with self.assertRaises(setup_worktree.DomainNotFoundError):
            setup_worktree.apply_sparse_checkout("nonexistent-domain", cwd=self.worktree_dir)

        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
