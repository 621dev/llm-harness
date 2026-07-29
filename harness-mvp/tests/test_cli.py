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
from providers.fallback_provider import QuotaFallbackProvider  # noqa: E402

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

    def test_no_fallback_configured_returns_plain_provider(self) -> None:
        """delegation_role_fallback_models가 비어있으면(기본값) 기존과 동일하게
        QuotaFallbackProvider로 안 감싸져야 한다 — 회귀 방지."""
        providers = cli._default_providers(_DEFAULT_CONFIG.candidate_models, _DEFAULT_CONFIG)

        self.assertNotIsInstance(providers["research-mock"], QuotaFallbackProvider)

    def test_fallback_configured_role_wrapped_with_quota_fallback_provider(self) -> None:
        config = HarnessConfig(
            delegation_role_models={"research": "gemini"},
            delegation_role_fallback_models={"research": "codex"},
        )

        providers = cli._default_providers(("claude",), config)

        research_provider = providers["research-mock"]
        self.assertIsInstance(research_provider, QuotaFallbackProvider)
        self.assertEqual(research_provider.primary.model_id, "gemini-2.5-flash")
        self.assertEqual(research_provider.fallback.model_id, "codex-cli")
        # 폴백 미설정 역할은 그대로 plain provider
        self.assertNotIsInstance(providers["design_review-mock"], QuotaFallbackProvider)

    def test_unknown_fallback_model_raises_value_error(self) -> None:
        config = HarnessConfig(delegation_role_fallback_models={"research": "made-up-model"})

        with self.assertRaises(ValueError):
            cli._default_providers(_DEFAULT_CONFIG.candidate_models, config)

    def test_judge_and_candidates_can_have_fallback(self) -> None:
        """judge/후보 폴백 (2026-07-29).

        그전까지 폴백은 **체인 역할에만** 있었다. judge가 종량제(gemini)라 한도가 사실상
        안 마르는 상태였기 때문인데, judge를 구독(codex)으로 옮기면서 **한도가 마르면
        fan_out run 전체가 실패**하는 경로가 생겼다 — "claude 메인 / codex 서브 /
        gemini 보조(넘침 처리)" 구성의 '보조'가 실제로 동작하려면 이 두 자리에도 필요하다.
        """
        config = HarnessConfig(
            judge_model="codex",
            judge_fallback_model="gemini",
            candidate_fallback_model="gemini",
        )

        providers = cli._default_providers(("claude", "codex"), config)

        judge_provider = providers[orchestrator.JUDGE_PROVIDER_KEY]
        self.assertIsInstance(judge_provider, QuotaFallbackProvider)
        self.assertEqual(judge_provider.primary.model_id, "codex-cli")
        self.assertEqual(judge_provider.fallback.model_id, "gemini-2.5-flash")
        for name in ("claude", "codex"):
            with self.subTest(candidate=name):
                self.assertIsInstance(providers[name], QuotaFallbackProvider)
                self.assertEqual(providers[name].fallback.model_id, "gemini-2.5-flash")

    def test_judge_and_candidate_fallback_default_to_none(self) -> None:
        """설정 안 하면 감싸지 않는다 — 기존 동작 회귀 방지."""
        providers = cli._default_providers(("claude",), _DEFAULT_CONFIG)

        self.assertNotIsInstance(providers[orchestrator.JUDGE_PROVIDER_KEY], QuotaFallbackProvider)
        self.assertNotIsInstance(providers["claude"], QuotaFallbackProvider)

    def test_unknown_judge_fallback_model_raises_value_error(self) -> None:
        with self.assertRaises(ValueError):
            cli._default_providers(("claude",), HarnessConfig(judge_fallback_model="made-up-model"))

    def test_candidate_provider_id_is_not_suffixed(self) -> None:
        """후보/judge의 `provider_id`에 `-mock`이 붙으면 안 된다.

        폴백 헬퍼가 원래 역할 전용이라 `f"{role}-mock"`을 안에서 만들었다 —
        후보·judge에 재사용하면서 그 규칙이 새어 나오면 run 산출물의 식별자가 오염된다.
        """
        config = HarnessConfig(judge_model="codex", judge_fallback_model="gemini",
                               candidate_fallback_model="gemini")

        providers = cli._default_providers(("claude",), config)

        self.assertEqual(providers["claude"].provider_id, "claude")
        self.assertEqual(providers[orchestrator.JUDGE_PROVIDER_KEY].provider_id, "judge")


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
        self.assertEqual(config.delegation_role_fallback_models, {})  # 마찬가지로 기본값(빈 dict)

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

    def test_reads_delegation_role_fallback_models_from_file(self) -> None:
        config_path = self.tmp_dir / "config.json"
        config_path.write_text(
            json.dumps({"delegation_role_fallback_models": {"research": "codex"}}), encoding="utf-8"
        )

        config = load_config(config_path)

        self.assertEqual(config.delegation_role_fallback_models, {"research": "codex"})

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


def _ok(stdout: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=0, stdout=stdout, stderr="")


def fake_git(*, branch="claude/x", head_before="abc", head_after="def", unmerged="",
             merge=None, unmerged_after=None):
    """`_git` 대역을 **인자로 분기**해서 만든다.

    예전에는 호출 순서에 맞춘 `side_effect` 목록을 썼는데, 구현에 git 호출이 하나
    추가될 때마다(2026-07-28 충돌 사전 확인 추가) 관련 없는 테스트가 한꺼번에
    깨졌다 — 테스트가 "무엇을 검증하는가"가 아니라 "몇 번째 호출인가"에 묶여 있던
    탓이다. 인자로 분기하면 호출이 늘어도 영향이 없다.
    """
    merge_result = merge if merge is not None else _ok("Fast-forward\n")
    seen = {"head": 0}

    def _fake(args, *, cwd):
        if args[:1] == ["merge"]:
            return merge_result
        if args[:1] == ["ls-files"]:
            # merge 이후에는 unmerged_after(주면)를 쓴다 — 충돌이 merge로 생기는 상황 재현
            after = unmerged_after if unmerged_after is not None else unmerged
            return _ok(after if seen["head"] >= 1 and unmerged_after is not None else unmerged)
        if args == ["rev-parse", "--abbrev-ref", "HEAD"]:
            return _ok(f"{branch}\n")
        if args == ["rev-parse", "HEAD"]:
            seen["head"] += 1
            return _ok(f"{head_before}\n" if seen["head"] == 1 else f"{head_after}\n")
        return _ok()

    return _fake


def _merge_calls(mock_git: MagicMock) -> list[list[str]]:
    return [c.args[0] for c in mock_git.call_args_list if c.args[0][:1] == ["merge"]]


class SyncWorktreeWithMainTest(unittest.TestCase):
    """2026-07-24 사용자 요청 "워크트리 관리 자동화" — 지금까지 매번 "각 도메인
    worktree도 main이랑 동기화해줘"로 손으로 반복했던 `git merge main`을 명령어
    하나(`worktree-sync`)로 대체하는 핵심 로직."""

    @patch("harness.cli._git")
    def test_domain_branch_merges_origin_main(self, mock_git: MagicMock) -> None:
        mock_git.side_effect = fake_git(branch="claude/cloud-ops-034a88")

        result = cli.sync_worktree_with_main(Path("/repo/worktree"))

        self.assertEqual(result["status"], "merged")
        self.assertEqual(_merge_calls(mock_git), [["merge", "origin/main", "--no-edit"]])

    @patch("harness.cli._git")
    def test_missing_worktree_directory_does_not_abort_the_run(self, mock_git: MagicMock) -> None:
        """회귀 테스트(2026-07-28 실측): `git worktree list`에 등록은 남아 있는데
        디렉터리가 사라진 worktree가 있으면(앱이 제거했거나 사람이 지운 경우)
        subprocess가 OSError를 던져 **나머지 worktree 동기화까지 통째로 중단**됐다.
        하나가 사라진 게 나머지를 못 맞출 이유는 없으므로 그 worktree만 실패로
        보고하고 계속 진행해야 한다."""
        mock_git.side_effect = NotADirectoryError("[WinError 267] 디렉터리 이름이 올바르지 않습니다")

        result = cli.sync_worktree_with_main(Path("/repo/deleted-worktree"))

        self.assertEqual(result["status"], "missing")
        self.assertIn("worktree prune", result["output"])  # 사람이 뭘 해야 하는지 안내

    @patch("harness.cli.sync_worktree_with_main")
    def test_sync_continues_to_next_worktree_after_missing_one(self, mock_sync: MagicMock) -> None:
        """위 상황에서 여러 worktree를 한 번에 돌릴 때 뒤쪽이 실제로 처리되는지."""
        mock_sync.side_effect = [
            {"path": "/repo/gone", "branch": "?", "status": "missing", "output": ""},
            {"path": "/repo/alive", "branch": "claude/x", "status": "merged", "output": ""},
        ]

        results = cli.sync_all_worktrees([Path("/repo/gone"), Path("/repo/alive")])

        self.assertEqual([r["status"] for r in results], ["missing", "merged"])

    @patch("harness.cli._git")
    def test_conflict_detected_even_when_git_reports_via_stderr(self, mock_git: MagicMock) -> None:
        """회귀 테스트(2026-07-28 실측): squash merge된 브랜치에 main을 다시 병합해
        실제로 충돌했는데, 판정이 `"CONFLICT" in stdout`이라 git이 안내를 **stderr로**
        보낸 경우 `conflict`가 아니라 `error`로 라벨링되고 출력도 비어 나왔다.
        판정은 문구가 아니라 git 인덱스(`ls-files --unmerged`)로 해야 한다."""
        mock_git.side_effect = fake_git(
            merge=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="", stderr="CONFLICT (add/add): README.md\n"
            ),
            unmerged="",  # 시작 시점엔 깨끗
            unmerged_after="100644 abc 1\tharness-mvp/README.md\n",  # merge가 충돌을 만듦
        )

        result = cli.sync_worktree_with_main(Path("/repo/worktree"))

        self.assertEqual(result["status"], "conflict")
        self.assertIn("CONFLICT", result["output"])  # stderr 내용도 사용자에게 보인다

    @patch("harness.cli._git")
    def test_already_wedged_worktree_reported_before_attempting_merge(self, mock_git: MagicMock) -> None:
        """이전 병합이 충돌로 멈춘 상태면 merge 자체가 시작되지 않는다
        ("Merging is not possible because you have unmerged files") — 그대로
        진행하면 원인이 안 드러나는 error가 되므로 먼저 걸러 안내해야 한다."""
        mock_git.side_effect = fake_git(unmerged="100644 abc 1\tsome/file.py\n")

        result = cli.sync_worktree_with_main(Path("/repo/worktree"))

        self.assertEqual(result["status"], "conflict")
        self.assertIn("merge --abort", result["output"])  # 뭘 해야 하는지 안내
        self.assertEqual(_merge_calls(mock_git), [])  # merge를 아예 시도하지 않았다

    @patch("harness.cli._git")
    def test_none_stdout_does_not_crash(self, mock_git: MagicMock) -> None:
        """방어 테스트: capture_output=True면 stdout이 str이어야 하는데 2026-07-28
        실행에서 None이 와서 TypeError로 죽었다(원인 미특정). 진단 문구를 만드는
        자리에서 죽는 건 얻는 것보다 잃는 게 크다."""

        def _all_none(args, *, cwd):
            failed = args[:1] == ["merge"]
            return subprocess.CompletedProcess(
                args=[], returncode=1 if failed else 0, stdout=None,
                stderr="boom\n" if failed else "",
            )

        mock_git.side_effect = _all_none

        result = cli.sync_worktree_with_main(Path("/repo/worktree"))

        self.assertEqual(result["status"], "error")
        self.assertIn("boom", result["output"])

    @patch("harness.cli._git")
    def test_main_branch_uses_fast_forward_only_pull(self, mock_git: MagicMock) -> None:
        mock_git.side_effect = fake_git(
            branch="main", head_before="abc123", head_after="abc123",
            merge=_ok("Already up to date.\n"),
        )

        result = cli.sync_worktree_with_main(Path("/repo"))

        self.assertEqual(result["status"], "up_to_date")
        self.assertEqual(_merge_calls(mock_git), [["merge", "--ff-only", "origin/main"]])

    @patch("harness.cli._git")
    def test_conflict_is_reported_not_raised(self, mock_git: MagicMock) -> None:
        mock_git.side_effect = fake_git(
            branch="claude/ncp-6191ba",
            merge=subprocess.CompletedProcess(
                args=[], returncode=1, stdout="CONFLICT (content): Merge conflict in x.md\n", stderr=""
            ),
            unmerged_after="100644 abc 1\tx.md\n",
        )

        result = cli.sync_worktree_with_main(Path("/repo/worktree"))

        self.assertEqual(result["status"], "conflict")

    @patch("harness.cli._git")
    def test_status_derived_from_head_sha_not_stdout_wording(self, mock_git: MagicMock) -> None:
        # 회귀 방지(2026-07-27 실제로 겪음): PR #44 merge 직후 실제 환경에서 fast-forward/
        # merge가 실제로 일어났는데도 git stdout에 "Already up to date" 문구가 섞여
        # up_to_date로 잘못 라벨링됐다. HEAD 커밋이 실제로 바뀌었으면 stdout 문구와
        # 무관하게 "merged"여야 한다.
        # 오해를 부르는 문구지만 실제로는 HEAD가 바뀌는 상황을 시뮬레이션
        mock_git.side_effect = fake_git(
            branch="main", head_before="abc123", head_after="def456",
            merge=_ok("Already up to date.\n"),
        )

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
