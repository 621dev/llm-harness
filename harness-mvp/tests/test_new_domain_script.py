"""scripts/new_domain.py 테스트 (stdlib unittest).

도메인 스캐폴딩 자동화 스크립트(2026-07-16, ncp-snapshot-drill/centos-eol-migration을
손으로 두 번 만들며 반복한 절차를 자동화)를 검증한다. planner/router는 규칙 기반
로컬 로직이라 실제 API/CLI 호출이 전혀 없다 — 모킹 없이 그대로 테스트 가능하다.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import new_domain  # noqa: E402


class CreateDomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="new-domain-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_creates_config_and_task_files(self) -> None:
        domain_dir = new_domain.create_domain(
            "test-domain",
            task_id="test-task",
            prompt="XX에 대해 조사해줘.",
            domains_root=self.tmp_dir,
        )

        self.assertTrue((domain_dir / "config.json").exists())
        self.assertTrue((domain_dir / "examples" / "task.test-task.json").exists())

    def test_config_json_matches_validated_template(self) -> None:
        domain_dir = new_domain.create_domain(
            "test-domain",
            task_id="test-task",
            prompt="XX에 대해 조사해줘.",
            domains_root=self.tmp_dir,
        )

        config = json.loads((domain_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(
            config["delegation_role_models"],
            {"research": "gemini", "design_review": "claude", "implementation_review": "codex"},
        )
        self.assertEqual(config["delegation_model"], "claude")

    def test_task_json_has_given_task_id_and_prompt(self) -> None:
        domain_dir = new_domain.create_domain(
            "test-domain",
            task_id="test-task",
            prompt="XX에 대해 조사해줘.",
            domains_root=self.tmp_dir,
        )

        task = json.loads((domain_dir / "examples" / "task.test-task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["task_id"], "test-task")
        self.assertEqual(task["prompt"], "XX에 대해 조사해줘.")
        self.assertEqual(task["constraints"], [])

    def test_raises_if_domain_already_exists(self) -> None:
        new_domain.create_domain(
            "test-domain", task_id="test-task", prompt="조사해줘.", domains_root=self.tmp_dir
        )

        with self.assertRaises(new_domain.DomainAlreadyExistsError):
            new_domain.create_domain(
                "test-domain", task_id="test-task-2", prompt="조사해줘.", domains_root=self.tmp_dir
            )


class VerifyDomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="new-domain-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_research_keyword_prompt_classifies_as_hierarchical_delegation(self) -> None:
        domain_dir = new_domain.create_domain(
            "test-domain",
            task_id="test-task",
            prompt="NCP XX 서비스를 조사해줘. 그 다음 절차서를 설계하고 검토해줘.",
            domains_root=self.tmp_dir,
        )

        result = new_domain.verify_domain(
            domain_dir, task_id="test-task", expected_pattern="hierarchical_delegation"
        )

        self.assertEqual(result["team_pattern"], "hierarchical_delegation")
        self.assertEqual(result["task_type"], "research")
        self.assertTrue(result["pattern_matches_expected"])
        self.assertEqual(
            [role for role, _ in result["delegation_chain"]], ["research", "design_review"]
        )

    def test_prompt_without_routing_keywords_flags_mismatch(self) -> None:
        domain_dir = new_domain.create_domain(
            "test-domain",
            task_id="test-task",
            prompt="아무 키워드도 없는 평범한 요청 문장입니다 그냥 이렇게 길게만 써봄",
            domains_root=self.tmp_dir,
        )

        result = new_domain.verify_domain(
            domain_dir, task_id="test-task", expected_pattern="hierarchical_delegation"
        )

        self.assertEqual(result["team_pattern"], "fan_out_judge")
        self.assertFalse(result["pattern_matches_expected"])

    def test_provider_registry_builds_without_error(self) -> None:
        domain_dir = new_domain.create_domain(
            "test-domain", task_id="test-task", prompt="조사해줘.", domains_root=self.tmp_dir
        )

        result = new_domain.verify_domain(
            domain_dir, task_id="test-task", expected_pattern="hierarchical_delegation"
        )

        self.assertIn("__judge__", result["provider_keys"])
        self.assertIn("research-mock", result["provider_keys"])
        self.assertIn("design_review-mock", result["provider_keys"])
        self.assertIn("implementation_review-mock", result["provider_keys"])


class RenderFollowupChecklistTest(unittest.TestCase):
    """공개 구조 미러(`621dev/llm-harness`)에는 docs/03_진행상황/이 없어서
    (2026-07-24 사용자 요청으로 도메인 실제 업무 내용과 함께 제외), 그 폴더가
    없을 때는 "갱신하라"는 안내를 아예 빼야 한다(실제로 공개 미러를 clone해
    이 스크립트를 돌려보다가 발견한 문제)."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="new-domain-checklist-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_mentions_progress_checklist_when_present(self) -> None:
        progress_dir = self.tmp_dir / "docs" / "03_진행상황"
        progress_dir.mkdir(parents=True)
        (progress_dir / "harness-progress-checklist-ko.md").write_text("", encoding="utf-8")

        checklist = new_domain.render_followup_checklist("d", "t", repo_root=self.tmp_dir)

        self.assertIn("docs/03_진행상황", checklist)

    def test_omits_progress_checklist_when_absent(self) -> None:
        checklist = new_domain.render_followup_checklist("d", "t", repo_root=self.tmp_dir)

        self.assertNotIn("docs/03_진행상황", checklist)
        self.assertIn("README.md", checklist)


if __name__ == "__main__":
    unittest.main()
