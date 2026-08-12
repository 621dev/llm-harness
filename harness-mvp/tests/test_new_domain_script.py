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
        # **기본값은 제약을 넣지 않는다** (2026-08-10 변경). 그전 기본값이
        # `hierarchical_delegation`이어서 새 도메인이 ADR 0009로 강등된 패턴을 opt-in
        # 제약으로 강제로 물고 태어났다 — 우위 미입증 + 비용 1.5~3.2배. 기본값이
        # `fan_out_judge`(유일한 자동 진입 패턴, ADR 0010)가 되면서 제약이 사라지고
        # planner의 키워드 라우팅이 살아난다.
        self.assertEqual(task["constraints"], [])

    def test_raises_if_domain_already_exists(self) -> None:
        new_domain.create_domain(
            "test-domain", task_id="test-task", prompt="조사해줘.", domains_root=self.tmp_dir
        )

        with self.assertRaises(new_domain.DomainAlreadyExistsError):
            new_domain.create_domain(
                "test-domain", task_id="test-task-2", prompt="조사해줘.", domains_root=self.tmp_dir
            )


class OptInPatternScaffoldTest(unittest.TestCase):
    """iterative_refinement/agentic_task는 키워드 자동 라우팅이 없어서, 스크립트가
    task json에 `team_pattern:` 제약을 직접 넣어주지 않으면 프롬프트와 무관하게
    fan_out_judge로 폴백된다 — 스캐폴딩이 곧바로 못 쓰는 도메인을 만드는 셈이라
    이 동작을 고정해둔다."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="new-domain-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def scaffold(self, pattern: str, *, prompt: str = "가상의 주제로 자료를 작성해줘.") -> Path:
        return new_domain.create_domain(
            f"test-{pattern}",
            task_id="test-task",
            prompt=prompt,
            pattern=pattern,
            domains_root=self.tmp_dir,
        )

    def test_iterative_refinement_task_gets_optin_constraint_and_routes(self) -> None:
        domain_dir = self.scaffold("iterative_refinement")

        task = json.loads((domain_dir / "examples" / "task.test-task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["constraints"], ["team_pattern:iterative_refinement"])

        result = new_domain.verify_domain(
            domain_dir, task_id="test-task", expected_pattern="iterative_refinement"
        )
        self.assertTrue(result["pattern_matches_expected"])
        self.assertEqual(result["delegation_chain"], [])

    def test_agentic_task_gets_optin_constraint_and_routes(self) -> None:
        domain_dir = self.scaffold("agentic_task")

        task = json.loads((domain_dir / "examples" / "task.test-task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["constraints"], ["team_pattern:agentic_task"])

        result = new_domain.verify_domain(
            domain_dir, task_id="test-task", expected_pattern="agentic_task"
        )
        self.assertTrue(result["pattern_matches_expected"])

    def test_keyword_routed_patterns_get_no_constraint(self) -> None:
        """자동 라우팅되는 패턴에까지 제약을 박아두면 프롬프트를 고쳐도 분류가
        안 바뀌어서, router 검증이라는 이 스크립트의 목적이 무력해진다."""
        # ADR 0009(2026-07-29)로 hierarchical_delegation이 opt-in이 되면서, 키워드 자동
        # 라우팅으로 진입하는 패턴은 fan_out_judge 하나만 남았다.
        for pattern in ("fan_out_judge",):
            with self.subTest(pattern=pattern):
                domain_dir = self.scaffold(pattern, prompt="이 설계안을 검토해줘.")
                task = json.loads(
                    (domain_dir / "examples" / "task.test-task.json").read_text(encoding="utf-8")
                )
                self.assertEqual(task["constraints"], [])

    def test_config_explains_fields_that_matter_for_the_pattern(self) -> None:
        domain_dir = self.scaffold("iterative_refinement")

        config = json.loads((domain_dir / "config.json").read_text(encoding="utf-8"))
        self.assertIn("generator", config["_설명"]["_주의"])
        self.assertEqual(config["max_refinement_rounds"], 3)  # 이 패턴의 비용 상한 knob

    def test_agentic_config_carries_turn_limit_knob(self) -> None:
        domain_dir = self.scaffold("agentic_task")

        config = json.loads((domain_dir / "config.json").read_text(encoding="utf-8"))
        self.assertEqual(config["max_agent_turns"], 8)
        self.assertNotIn("max_refinement_rounds", config)  # 안 쓰는 knob은 넣지 않는다

    def test_agentic_notice_warns_about_approval_gate(self) -> None:
        notice = "\n".join(new_domain.render_pattern_notice("agentic_task"))

        self.assertIn("승인", notice)
        self.assertEqual(new_domain.render_pattern_notice("hierarchical_delegation"), [])

    def test_all_supported_patterns_scaffold_and_route(self) -> None:
        """지원 목록에 패턴을 추가하고 배선을 빠뜨리는 걸 막는 회귀 테스트."""
        for pattern in new_domain.SUPPORTED_PATTERNS:
            with self.subTest(pattern=pattern):
                prompt = "XX를 조사해줘." if pattern == "hierarchical_delegation" else "자료를 작성해줘."
                domain_dir = new_domain.create_domain(
                    f"all-{pattern}",
                    task_id="t",
                    prompt=prompt,
                    pattern=pattern,
                    domains_root=self.tmp_dir,
                )
                result = new_domain.verify_domain(domain_dir, task_id="t", expected_pattern=pattern)
                self.assertTrue(result["pattern_matches_expected"], pattern)


class VerifyDomainTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="new-domain-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_research_keyword_prompt_routes_to_fan_out_without_a_chain(self) -> None:
        """기본값으로 만든 도메인은 **키워드 라우팅**으로 진입한다 (2026-08-10 개정).

        이 테스트는 원래 같은 프롬프트가 `hierarchical_delegation`으로 간다고 고정하고
        있었다. 그건 프롬프트 때문이 아니라 **스캐폴딩이 넣어준 opt-in 제약** 때문이었고,
        그 제약 자체가 ADR 0009로 강등된 패턴을 기본값으로 강제하는 결함이었다.

        체인 스캐폴딩 자체는 `test_all_supported_patterns_scaffold_and_route`가
        `--pattern hierarchical_delegation`을 명시로 넘겨 계속 덮는다 — 커버리지는 유지된다.
        """
        domain_dir = new_domain.create_domain(
            "test-domain",
            task_id="test-task",
            prompt="NCP XX 서비스를 조사해줘. 그 다음 절차서를 설계하고 검토해줘.",
            domains_root=self.tmp_dir,
        )

        result = new_domain.verify_domain(
            domain_dir, task_id="test-task", expected_pattern="fan_out_judge"
        )

        # "조사"가 "설계"보다 먼저 검사되므로(router의 `_TASK_TYPE_RULES` 순서) research다.
        self.assertEqual(result["task_type"], "research")
        self.assertEqual(result["team_pattern"], "fan_out_judge")
        self.assertTrue(result["pattern_matches_expected"])
        # 기본값이 체인을 강제하지 않는다는 게 이 변경의 요점이다.
        self.assertEqual(result["delegation_chain"], [])

    def test_prompt_without_routing_keywords_flags_mismatch(self) -> None:
        """기대 패턴과 실제 분류가 어긋나면 불일치로 보고하는지.

        **`fan_out_judge`로 스캐폴딩한다** — ADR 0009 이후 `hierarchical_delegation`은
        opt-in이라 스캐폴딩이 `team_pattern:` 제약을 넣어주고, 그러면 프롬프트에 키워드가
        없어도 체인으로 진입해서 "키워드가 없어 어긋난다"는 상황이 만들어지지 않는다.
        제약이 안 붙는 패턴으로 만들어야 프롬프트 분류가 실제로 결과를 좌우한다.
        """
        domain_dir = new_domain.create_domain(
            "test-domain",
            task_id="test-task",
            prompt="아무 키워드도 없는 평범한 요청 문장입니다 그냥 이렇게 길게만 써봄",
            pattern="fan_out_judge",
            domains_root=self.tmp_dir,
        )

        result = new_domain.verify_domain(
            domain_dir, task_id="test-task", expected_pattern="hierarchical_delegation"
        )

        self.assertEqual(result["team_pattern"], "fan_out_judge")  # planner 기본값
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
