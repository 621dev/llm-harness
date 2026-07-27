"""cli.py 테스트: --models 파싱/레지스트리 + config.json 연동 (stdlib unittest).

fan_out_judge 후보/judge/delegation 모델과 구독 한도 상한을 config.json으로
뺀 기능(사용자 요청, 2026-07-10)을 검증한다. 실제 provider 인스턴스 생성은
credentials 없이도 가능하므로(실제 API/CLI 호출은 generate() 시점에만 발생)
여기서는 실제 호출 없이 provider 구성만 확인한다.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import cli, orchestrator  # noqa: E402
from harness.config import HarnessConfig, load_config  # noqa: E402

_DEFAULT_CONFIG = HarnessConfig()


class ParseModelsTest(unittest.TestCase):
    def test_none_returns_given_default(self) -> None:
        self.assertEqual(cli._parse_models(None, ("claude", "gemini")), ("claude", "gemini"))

    def test_parses_comma_separated_list(self) -> None:
        self.assertEqual(cli._parse_models("claude,gemini", _DEFAULT_CONFIG.candidate_models), ("claude", "gemini"))

    def test_trims_whitespace_around_names(self) -> None:
        self.assertEqual(
            cli._parse_models(" claude , gemini ", _DEFAULT_CONFIG.candidate_models), ("claude", "gemini")
        )

    def test_unknown_model_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            cli._parse_models("claude,made-up-model", _DEFAULT_CONFIG.candidate_models)


class DefaultProvidersTest(unittest.TestCase):
    def test_default_includes_all_three_candidate_models(self) -> None:
        providers = cli._default_providers(_DEFAULT_CONFIG.candidate_models, _DEFAULT_CONFIG)

        for name in ("claude", "codex", "gemini"):
            self.assertIn(name, providers)

    def test_selecting_subset_excludes_the_rest(self) -> None:
        providers = cli._default_providers(("claude", "gemini"), _DEFAULT_CONFIG)

        self.assertIn("claude", providers)
        self.assertIn("gemini", providers)
        self.assertNotIn("codex", providers)

    def test_judge_and_delegation_providers_always_present_regardless_of_models(self) -> None:
        providers = cli._default_providers(("claude",), _DEFAULT_CONFIG)

        self.assertIn(orchestrator.JUDGE_PROVIDER_KEY, providers)
        for role in ("research", "design_review", "implementation_review"):
            self.assertIn(f"{role}-mock", providers)

    def test_agent_provider_registered_and_capable_of_agent_mode(self) -> None:
        """agentic_task용 에이전트 provider(ADR 0007)는 후보 모델 선택과 무관하게
        전용 예약 키로만 등록된다 — 일반 텍스트 생성 자리에 섞이면 안 되기 때문."""
        providers = cli._default_providers(("gemini",), _DEFAULT_CONFIG)

        agent = providers[orchestrator.AGENT_PROVIDER_KEY]
        self.assertTrue(hasattr(agent, "run_agent"))
        self.assertEqual(agent.model_id, "claude-cli")  # codex/gemini는 이번 범위 밖

    def test_judge_model_config_selects_backend(self) -> None:
        config = HarnessConfig(judge_model="claude")

        providers = cli._default_providers(("gemini",), config)

        judge_provider = providers[orchestrator.JUDGE_PROVIDER_KEY]
        self.assertEqual(judge_provider.model_id, "claude-cli")

    def test_delegation_model_config_selects_backend(self) -> None:
        config = HarnessConfig(delegation_model="gemini")

        providers = cli._default_providers(("claude",), config)

        self.assertEqual(providers["research-mock"].model_id, "gemini-2.5-flash")

    def test_unknown_judge_model_in_config_raises_value_error(self) -> None:
        config = HarnessConfig(judge_model="made-up-model")

        with self.assertRaises(ValueError):
            cli._default_providers(_DEFAULT_CONFIG.candidate_models, config)

    def test_delegation_role_models_overrides_specific_role_only(self) -> None:
        """역할 분담: research만 다른 모델을 쓰고, 명시 안 된 나머지 역할은
        delegation_model(기본 모델)을 그대로 쓴다."""
        config = HarnessConfig(delegation_model="claude", delegation_role_models={"research": "gemini"})

        providers = cli._default_providers(("claude",), config)

        self.assertEqual(providers["research-mock"].model_id, "gemini-2.5-flash")
        self.assertEqual(providers["design_review-mock"].model_id, "claude-cli")
        self.assertEqual(providers["implementation_review-mock"].model_id, "claude-cli")

    def test_delegation_role_models_empty_falls_back_to_delegation_model_for_all_roles(self) -> None:
        """delegation_role_models가 빈 dict(기본값)면 역할 분담을 도입하기 전과
        동일하게 전체 역할이 delegation_model 하나로 통일돼야 한다(회귀 방지)."""
        config = HarnessConfig(delegation_model="gemini")

        providers = cli._default_providers(("claude",), config)

        for role in ("research", "design_review", "implementation_review"):
            self.assertEqual(providers[f"{role}-mock"].model_id, "gemini-2.5-flash")

    def test_unknown_delegation_role_model_raises_value_error(self) -> None:
        config = HarnessConfig(delegation_role_models={"research": "made-up-model"})

        with self.assertRaises(ValueError):
            cli._default_providers(_DEFAULT_CONFIG.candidate_models, config)


class LoadConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-config-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_missing_file_returns_defaults(self) -> None:
        config = load_config(self.tmp_dir / "does-not-exist.json")

        self.assertEqual(config, HarnessConfig())

    def test_reads_values_from_file(self) -> None:
        config_path = self.tmp_dir / "config.json"
        config_path.write_text(
            json.dumps({"candidate_models": ["claude"], "max_subscription_candidates": 2}), encoding="utf-8"
        )

        config = load_config(config_path)

        self.assertEqual(config.candidate_models, ["claude"])
        self.assertEqual(config.max_subscription_candidates, 2)
        self.assertEqual(config.judge_model, "gemini")  # 파일에 없는 필드는 기본값
        self.assertEqual(config.delegation_role_models, {})  # 파일에 없는 필드는 기본값(빈 dict)

    def test_reads_max_agent_turns_from_file(self) -> None:
        config_path = self.tmp_dir / "config.json"
        config_path.write_text(json.dumps({"max_agent_turns": 8}), encoding="utf-8")

        config = load_config(config_path)

        self.assertEqual(config.max_agent_turns, 8)
        self.assertEqual(config.max_refinement_rounds, 3)  # 파일에 없는 필드는 기본값

    def test_reads_max_refinement_rounds_from_file(self) -> None:
        config_path = self.tmp_dir / "config.json"
        config_path.write_text(json.dumps({"max_refinement_rounds": 5}), encoding="utf-8")

        config = load_config(config_path)

        self.assertEqual(config.max_refinement_rounds, 5)

    def test_max_refinement_rounds_defaults_to_orchestrator_constant(self) -> None:
        """config.json에 값이 없으면 orchestrator의 하드코딩 기본값(도입 당시
        MAX_REFINEMENT_ROUNDS=3)과 같아야 한다 — 파일 없이도 기존 동작 유지."""
        config = load_config(self.tmp_dir / "does-not-exist.json")

        self.assertEqual(config.max_refinement_rounds, 3)

    def test_reads_delegation_role_models_from_file(self) -> None:
        config_path = self.tmp_dir / "config.json"
        config_path.write_text(
            json.dumps({"delegation_role_models": {"research": "gemini", "implementation_review": "codex"}}),
            encoding="utf-8",
        )

        config = load_config(config_path)

        self.assertEqual(config.delegation_role_models, {"research": "gemini", "implementation_review": "codex"})

    def test_default_path_resolves_relative_to_cwd(self) -> None:
        """도메인 폴더 분리(harness-100 패턴)의 전제: 경로를 안 주면 패키지 설치
        위치가 아니라 '현재 작업 디렉터리'의 config.json을 읽어야, 같은 엔진을
        domains/<name>/에서 실행했을 때 그 폴더의 설정을 집어온다."""
        (self.tmp_dir / "config.json").write_text(
            json.dumps({"candidate_models": ["gemini"]}), encoding="utf-8"
        )
        original_cwd = Path.cwd()
        os.chdir(self.tmp_dir)
        self.addCleanup(os.chdir, original_cwd)

        config = load_config()

        self.assertEqual(config.candidate_models, ["gemini"])


class ParseWorktreePorcelainOutputTest(unittest.TestCase):
    """`harness.cli.status --all-domains`가 다른 worktree까지 자동으로 찾을 수 있게
    (2026-07-20 사용자 요청) `git worktree list --porcelain` 출력을 파싱하는 순수
    함수 테스트 — 실제 git 호출 없이 하드코딩된 출력 문자열로 검증한다."""

    def test_extracts_all_worktree_paths(self) -> None:
        output = (
            "worktree C:/Users/x/multi-llm-harness\n"
            "HEAD 79836bc85fc07471a1ee2fb0116448ba8a6f810d\n"
            "branch refs/heads/main\n"
            "\n"
            "worktree C:/Users/x/multi-llm-harness/.claude/worktrees/cloud-ops-estimate-034a88\n"
            "HEAD fc7b976fd157b0067c4eb93a1c3a5cc1f1552486\n"
            "branch refs/heads/claude/cloud-ops-estimate-034a88\n"
        )

        result = cli._parse_worktree_porcelain_output(output)

        self.assertEqual(
            result,
            [
                Path("C:/Users/x/multi-llm-harness"),
                Path("C:/Users/x/multi-llm-harness/.claude/worktrees/cloud-ops-estimate-034a88"),
            ],
        )

    def test_empty_output_returns_empty_list(self) -> None:
        self.assertEqual(cli._parse_worktree_porcelain_output(""), [])

    def test_ignores_non_worktree_lines(self) -> None:
        output = "HEAD abc123\nbranch refs/heads/main\n"
        self.assertEqual(cli._parse_worktree_porcelain_output(output), [])


class DiscoverGitWorktreesTest(unittest.TestCase):
    @patch("harness.cli.subprocess.run")
    def test_calls_git_worktree_list_and_parses_output(self, mock_run: MagicMock) -> None:
        mock_run.return_value = subprocess.CompletedProcess(
            args=["git", "worktree", "list", "--porcelain"],
            returncode=0,
            stdout="worktree /repo\nHEAD abc\nbranch refs/heads/main\n",
        )

        result = cli._discover_git_worktrees()

        self.assertEqual(result, [Path("/repo")])
        called_args = mock_run.call_args.args[0]
        self.assertEqual(called_args, ["git", "worktree", "list", "--porcelain"])

    @patch("harness.cli.subprocess.run")
    def test_git_not_found_returns_empty_list(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = FileNotFoundError()

        self.assertEqual(cli._discover_git_worktrees(), [])

    @patch("harness.cli.subprocess.run")
    def test_git_command_failure_returns_empty_list(self, mock_run: MagicMock) -> None:
        mock_run.side_effect = subprocess.CalledProcessError(returncode=128, cmd=["git"])

        self.assertEqual(cli._discover_git_worktrees(), [])


class SyncWorktreeWithMainTest(unittest.TestCase):
    """2026-07-24 사용자 요청 "워크트리 관리 자동화" — 지금까지 매번 "각 도메인
    worktree도 main이랑 동기화해줘"로 손으로 반복했던 `git merge main`을 명령어
    하나(`worktree-sync`)로 대체하는 핵심 로직."""

    @patch("harness.cli._git")
    def test_domain_branch_merges_origin_main(self, mock_git: MagicMock) -> None:
        mock_git.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="claude/cloud-ops-034a88\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),  # before HEAD
            subprocess.CompletedProcess(args=[], returncode=0, stdout="Updating abc..def\nFast-forward\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="def456\n", stderr=""),  # after HEAD
        ]

        result = cli.sync_worktree_with_main(Path("/repo/worktree"))

        self.assertEqual(result["status"], "merged")
        merge_call_args = mock_git.call_args_list[2].args[0]
        self.assertEqual(merge_call_args, ["merge", "origin/main", "--no-edit"])

    @patch("harness.cli._git")
    def test_main_branch_uses_fast_forward_only_pull(self, mock_git: MagicMock) -> None:
        mock_git.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="main\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),  # before HEAD
            subprocess.CompletedProcess(args=[], returncode=0, stdout="Already up to date.\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),  # after HEAD (동일)
        ]

        result = cli.sync_worktree_with_main(Path("/repo"))

        self.assertEqual(result["status"], "up_to_date")
        merge_call_args = mock_git.call_args_list[2].args[0]
        self.assertEqual(merge_call_args, ["merge", "--ff-only", "origin/main"])

    @patch("harness.cli._git")
    def test_conflict_is_reported_not_raised(self, mock_git: MagicMock) -> None:
        mock_git.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="claude/ncp-6191ba\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),  # before HEAD
            subprocess.CompletedProcess(
                args=[], returncode=1, stdout="CONFLICT (content): Merge conflict in x.md\n", stderr=""
            ),
        ]

        result = cli.sync_worktree_with_main(Path("/repo/worktree"))

        self.assertEqual(result["status"], "conflict")

    @patch("harness.cli._git")
    def test_status_derived_from_head_sha_not_stdout_wording(self, mock_git: MagicMock) -> None:
        # 회귀 방지(2026-07-27 실제로 겪음): PR #44 merge 직후 실제 환경에서 fast-forward/
        # merge가 실제로 일어났는데도 git stdout에 "Already up to date" 문구가 섞여
        # up_to_date로 잘못 라벨링됐다. HEAD 커밋이 실제로 바뀌었으면 stdout 문구와
        # 무관하게 "merged"여야 한다.
        mock_git.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout="main\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="abc123\n", stderr=""),  # before HEAD
            # 오해를 부르는 문구지만 실제로는 HEAD가 바뀌는 상황을 시뮬레이션
            subprocess.CompletedProcess(args=[], returncode=0, stdout="Already up to date.\n", stderr=""),
            subprocess.CompletedProcess(args=[], returncode=0, stdout="def456\n", stderr=""),  # after HEAD (다름)
        ]

        result = cli.sync_worktree_with_main(Path("/repo"))

        self.assertEqual(result["status"], "merged")


class FindStaleWorktreeBranchesTest(unittest.TestCase):
    """정리 대상 탐지(2026-07-24 사용자 요청). 처음엔 `gh pr list --state merged`로
    "이 브랜치로 merge된 PR이 있었나"를 물어봤는데, 실제 CLI로 돌려보니 이 프로젝트는
    PR merge 후에도 같은 브랜치에서 도메인 작업을 계속 이어가는 패턴이라(예:
    centos-eol-migration-plan-49a2d3가 PR #33 이후로도 계속 커밋됨) 4개 도메인
    worktree 전부가 잘못 걸렸다. 그래서 "지금 이 순간 main과 트리 내용이 완전히
    같은가"(git diff main HEAD가 비어있는가) + "커밋 안 된 변경사항이 없는가"로
    다시 판단하도록 고쳤다."""

    @patch("harness.cli._current_branch")
    @patch("harness.cli._git")
    def test_flags_worktree_with_no_unique_content_and_clean_tree(
        self, mock_git: MagicMock, mock_branch: MagicMock
    ) -> None:
        mock_branch.side_effect = ["main", "claude/cloud-ops-034a88"]
        # wt1: status 깨끗, diff 없음(main과 완전히 같음) -> stale
        mock_git.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout=""),  # status --porcelain
            subprocess.CompletedProcess(args=[], returncode=0, stdout=""),  # diff --quiet (0 = 없음)
        ]
        wt1 = Path("/repo/wt1")
        worktrees = [Path("/repo"), wt1]

        stale = cli.find_stale_worktree_branches(worktrees)

        self.assertEqual(stale, [{"path": str(wt1), "branch": "claude/cloud-ops-034a88"}])

    @patch("harness.cli._current_branch")
    def test_main_branch_never_flagged(self, mock_branch: MagicMock) -> None:
        mock_branch.return_value = "main"

        stale = cli.find_stale_worktree_branches([Path("/repo")])

        self.assertEqual(stale, [])

    @patch("harness.cli._current_branch")
    @patch("harness.cli._git")
    def test_uncommitted_changes_prevent_stale_flag(self, mock_git: MagicMock, mock_branch: MagicMock) -> None:
        mock_branch.return_value = "claude/cloud-ops-034a88"
        mock_git.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout=" M some-file.md\n")

        stale = cli.find_stale_worktree_branches([Path("/repo/wt1")])

        self.assertEqual(stale, [])

    @patch("harness.cli._current_branch")
    @patch("harness.cli._git")
    def test_unique_content_vs_main_prevents_stale_flag(self, mock_git: MagicMock, mock_branch: MagicMock) -> None:
        mock_branch.return_value = "claude/cloud-ops-034a88"
        mock_git.side_effect = [
            subprocess.CompletedProcess(args=[], returncode=0, stdout=""),  # status: clean
            subprocess.CompletedProcess(args=[], returncode=1, stdout=""),  # diff --quiet: 차이 있음
        ]

        stale = cli.find_stale_worktree_branches([Path("/repo/wt1")])

        self.assertEqual(stale, [])


if __name__ == "__main__":
    unittest.main()
