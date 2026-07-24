"""live_status.py 테스트 (stdlib unittest).

dashboard.py(회고적 집계)와 달리 "지금 뭐가 돌고 있나"를 판정하는 로직을
검증한다. run_meta.json의 pid 생존 여부로 "실행 중"과 "중간에 프로세스가
죽음"을 구분하는 게 핵심이라, 실제 프로세스를 띄우지 않고도 검증 가능하도록
현재 프로세스의 pid(os.getpid(), 반드시 살아있음)와 존재할 수 없는 pid(아주
큰 값)를 대조한다 — 실제 API/CLI 호출 없음.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import live_status, run_store  # noqa: E402

_NONEXISTENT_PID = 999_999_999  # 실제 존재할 수 없는 매우 큰 PID


class PidIsAliveTest(unittest.TestCase):
    def test_current_process_pid_is_alive(self) -> None:
        self.assertTrue(live_status.pid_is_alive(os.getpid()))

    def test_nonexistent_pid_is_not_alive(self) -> None:
        self.assertFalse(live_status.pid_is_alive(_NONEXISTENT_PID))


class DescribeRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="live-status-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def _run_dir(self, run_id: str = "run-1") -> Path:
        return run_store.create_run(run_id, root=self.tmp_dir)

    def test_alive_pid_with_no_terminal_file_is_running(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "plan.json", {"team_pattern": "fan_out_judge"})
        live_status.write_run_meta(run_dir, pid=os.getpid())

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "running")
        self.assertEqual(result["team_pattern"], "fan_out_judge")
        self.assertIsNotNone(result["started_at"])

    def test_input_json_prompt_is_included(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "input.json", {"task_id": "run-1", "prompt": "이 설계안을 검토해줘"})

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["prompt"], "이 설계안을 검토해줘")

    def test_missing_input_json_gives_none_prompt(self) -> None:
        run_dir = self._run_dir()

        result = live_status.describe_run(run_dir)

        self.assertIsNone(result["prompt"])

    def test_input_json_task_id_is_included(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "input.json", {"task_id": "fan-out-demo", "prompt": "검토해줘"})

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["task_id"], "fan-out-demo")

    def test_missing_input_json_gives_none_task_id(self) -> None:
        run_dir = self._run_dir()

        result = live_status.describe_run(run_dir)

        self.assertIsNone(result["task_id"])

    def test_dead_pid_with_no_terminal_file_is_interrupted(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "plan.json", {"team_pattern": "hierarchical_delegation"})
        live_status.write_run_meta(run_dir, pid=_NONEXISTENT_PID)

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "interrupted")

    def test_errors_json_without_final_md_is_done_error_not_interrupted(self) -> None:
        """실제 수동 검증(2026-07-16)으로 발견한 버그의 회귀 테스트: fan_out_judge가
        min_candidates 미달로 실패하면 orchestrator._finalize_without_output()가
        errors.json만 쓰고 final.md는 안 쓴 채 정상 종료한다. 이건 크래시가 아니라
        "출력 없이 끝난 정상 종료"인데, errors.json 존재를 확인 안 하면 죽은 pid와
        똑같이 "interrupted"로 오판하게 된다."""
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "errors.json", [{"stage": "fan_out_judge", "message": "min_candidates 미달"}])
        live_status.write_run_meta(run_dir, pid=_NONEXISTENT_PID)  # 이미 종료된 프로세스

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "done_error")

    def test_no_run_meta_at_all_is_unknown(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "plan.json", {"team_pattern": "fan_out_judge"})

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "unknown")

    def test_final_md_with_no_errors_is_done_success(self) -> None:
        run_dir = self._run_dir()
        live_status.write_run_meta(run_dir, pid=os.getpid())
        run_store.write_markdown(run_dir, "final.md", "content")
        run_store.write_json(run_dir, "errors.json", [])

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "done_success")

    def test_final_md_with_errors_is_done_warning(self) -> None:
        run_dir = self._run_dir()
        run_store.write_markdown(run_dir, "final.md", "content")
        run_store.write_json(run_dir, "errors.json", [{"stage": "x", "message": "y"}])

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "done_warning")

    def test_pending_approval_takes_precedence_over_dead_pid(self) -> None:
        # approval.json이 pending이면 원래 프로세스는 이미 종료된 게 정상(별도 CLI
        # 호출로 승인/반려를 기다리는 중) — "interrupted"가 아니라 정상적인 대기 상태로
        # 판정돼야 한다.
        run_dir = self._run_dir()
        live_status.write_run_meta(run_dir, pid=_NONEXISTENT_PID)
        run_store.write_json(run_dir, "approval.json", {"status": "pending"})

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "awaiting_approval")

    def test_rejected_approval_is_done_rejected(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "approval.json", {"status": "rejected"})

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "done_rejected")

    def test_pending_safety_review_is_awaiting_safety_review(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "safety_review.json", {"status": "pending"})

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "awaiting_safety_review")

    def test_rejected_safety_review_is_done_blocked(self) -> None:
        run_dir = self._run_dir()
        run_store.write_json(run_dir, "safety_review.json", {"status": "rejected"})

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["status"], "done_blocked")

    def test_missing_plan_json_defaults_to_direct_call(self) -> None:
        run_dir = self._run_dir()
        run_store.write_markdown(run_dir, "final.md", "content")
        run_store.write_json(run_dir, "errors.json", [])

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["team_pattern"], "direct_call")


class ListLiveStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="live-status-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_scans_all_runs_in_workspace(self) -> None:
        run1 = run_store.create_run("run-1", root=self.tmp_dir)
        run_store.write_markdown(run1, "final.md", "ok")
        run_store.write_json(run1, "errors.json", [])
        run2 = run_store.create_run("run-2", root=self.tmp_dir)
        live_status.write_run_meta(run2, pid=os.getpid())

        results = live_status.list_live_status(root=self.tmp_dir)

        statuses_by_id = {r["run_id"]: r["status"] for r in results}
        self.assertEqual(statuses_by_id, {"run-1": "done_success", "run-2": "running"})

    def test_empty_workspace_returns_empty_list(self) -> None:
        self.assertEqual(live_status.list_live_status(root=self.tmp_dir), [])


class DomainLabelTest(unittest.TestCase):
    """run_dir 경로 구조(.../<도메인 폴더>/_workspace/runs/<run_id>)에서 도메인 이름을
    뽑아내는 로직 — 여러 workspace를 총체적으로 조회할 때(2026-07-20 사용자 요청)
    어디 소속인지 구분하는 데 쓰인다."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="live-status-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_domain_label_extracted_from_workspace_runs_path(self) -> None:
        root = self.tmp_dir / "cloud-ops" / "_workspace" / "runs"
        run_dir = run_store.create_run("run-1", root=root)
        run_store.write_markdown(run_dir, "final.md", "ok")
        run_store.write_json(run_dir, "errors.json", [])

        result = live_status.describe_run(run_dir)

        self.assertEqual(result["domain"], "cloud-ops")


class ListLiveStatusMultiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="live-status-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_aggregates_across_multiple_roots_with_domain_labels(self) -> None:
        root_a = self.tmp_dir / "domain-a" / "_workspace" / "runs"
        root_b = self.tmp_dir / "domain-b" / "_workspace" / "runs"
        run_a = run_store.create_run("run-a1", root=root_a)
        run_store.write_markdown(run_a, "final.md", "ok")
        run_store.write_json(run_a, "errors.json", [])
        run_b = run_store.create_run("run-b1", root=root_b)
        run_store.write_markdown(run_b, "final.md", "ok")
        run_store.write_json(run_b, "errors.json", [])

        results = live_status.list_live_status_multi([root_a, root_b])

        domains = sorted(r["domain"] for r in results)
        self.assertEqual(domains, ["domain-a", "domain-b"])

    def test_empty_roots_list_returns_empty_list(self) -> None:
        self.assertEqual(live_status.list_live_status_multi([]), [])


class EstimateOutputTest(unittest.TestCase):
    """cloud-ops처럼 LLM run 없이 결정론적으로 파일만 생성하는 도메인 작업을
    대시보드에서 확인할 수 있게 하는 기능(2026-07-20 사용자 요청)의 테스트."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="live-status-test-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp_dir, ignore_errors=True))

    def test_describe_estimate_output_with_files(self) -> None:
        output_dir = self.tmp_dir / "cloud-ops" / "_workspace" / "estimates" / "full-infra-estimate"
        output_dir.mkdir(parents=True)
        (output_dir / "estimate_document_aws.xlsx").write_text("dummy", encoding="utf-8")
        (output_dir / "estimate_document_ncp.xlsx").write_text("dummy", encoding="utf-8")

        result = live_status.describe_estimate_output(output_dir)

        self.assertEqual(result["task_id"], "full-infra-estimate")
        self.assertEqual(result["domain"], "cloud-ops")
        self.assertEqual(result["team_pattern"], "direct_output")
        self.assertEqual(result["status"], "done_success")
        self.assertIsNotNone(result["started_at"])
        self.assertIn("estimate_document_aws.xlsx", result["prompt"])

    def test_describe_estimate_output_empty_dir_returns_none(self) -> None:
        output_dir = self.tmp_dir / "cloud-ops" / "_workspace" / "estimates" / "empty-task"
        output_dir.mkdir(parents=True)

        self.assertIsNone(live_status.describe_estimate_output(output_dir))

    def test_list_estimate_outputs_skips_empty_and_non_dirs(self) -> None:
        estimates_root = self.tmp_dir / "cloud-ops" / "_workspace" / "estimates"
        good_dir = estimates_root / "task-a"
        good_dir.mkdir(parents=True)
        (good_dir / "out.xlsx").write_text("dummy", encoding="utf-8")
        empty_dir = estimates_root / "task-b"
        empty_dir.mkdir(parents=True)
        (estimates_root / "stray-file.txt").write_text("x", encoding="utf-8")

        results = live_status.list_estimate_outputs(estimates_root)

        self.assertEqual([r["task_id"] for r in results], ["task-a"])

    def test_list_estimate_outputs_missing_dir_returns_empty(self) -> None:
        self.assertEqual(live_status.list_estimate_outputs(self.tmp_dir / "does-not-exist"), [])

    def test_list_domain_activity_combines_runs_and_estimates(self) -> None:
        root = self.tmp_dir / "cloud-ops" / "_workspace" / "runs"
        run_dir = run_store.create_run("run-1", root=root)
        run_store.write_markdown(run_dir, "final.md", "ok")
        run_store.write_json(run_dir, "errors.json", [])
        estimates_dir = self.tmp_dir / "cloud-ops" / "_workspace" / "estimates" / "some-estimate"
        estimates_dir.mkdir(parents=True)
        (estimates_dir / "out.xlsx").write_text("dummy", encoding="utf-8")

        results = live_status.list_domain_activity(root=root)

        task_ids = sorted(r["task_id"] for r in results if r.get("task_id"))
        team_patterns = sorted(r["team_pattern"] for r in results)
        self.assertIn("some-estimate", task_ids)
        self.assertIn("direct_output", team_patterns)
        self.assertEqual(len(results), 2)

    def test_list_domain_activity_multi_aggregates(self) -> None:
        root_a = self.tmp_dir / "domain-a" / "_workspace" / "runs"
        estimates_a = self.tmp_dir / "domain-a" / "_workspace" / "estimates" / "task-1"
        estimates_a.mkdir(parents=True)
        (estimates_a / "out.xlsx").write_text("dummy", encoding="utf-8")
        root_b = self.tmp_dir / "domain-b" / "_workspace" / "runs"
        run_b = run_store.create_run("run-b1", root=root_b)
        run_store.write_markdown(run_b, "final.md", "ok")
        run_store.write_json(run_b, "errors.json", [])

        results = live_status.list_domain_activity_multi([root_a, root_b])

        domains = sorted(r["domain"] for r in results)
        self.assertEqual(domains, ["domain-a", "domain-b"])


class FormatElapsedTest(unittest.TestCase):
    def test_none_started_at_returns_none(self) -> None:
        self.assertIsNone(live_status.format_elapsed(None))

    def test_recent_timestamp_formats_in_seconds(self) -> None:
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc).isoformat()
        result = live_status.format_elapsed(now)
        self.assertTrue(result.endswith("초"))


class RenderHtmlTest(unittest.TestCase):
    def test_filter_bar_has_options_for_present_values(self) -> None:
        """2026-07-23 사용자 요청: "항목별로 필터를 걸 수 있어?" — 도메인/
        team_pattern/상태 드롭다운에 실제 데이터에 있는 값만 선택지로 뜨는지 확인."""
        statuses = [
            {
                "run_id": "run-1",
                "domain": "cloud-ops",
                "team_pattern": "fan_out_judge",
                "status": "done_success",
                "started_at": None,
            },
            {
                "run_id": "run-2",
                "domain": "ncp-snapshot-drill",
                "team_pattern": "direct_output",
                "status": "done_error",
                "started_at": None,
            },
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn('<option value="cloud-ops">cloud-ops</option>', html_output)
        self.assertIn('<option value="ncp-snapshot-drill">ncp-snapshot-drill</option>', html_output)
        self.assertIn('<option value="fan_out_judge">fan_out_judge</option>', html_output)
        self.assertIn('<option value="direct_output">direct_output</option>', html_output)
        self.assertIn('<option value="done_success">완료(성공)</option>', html_output)
        self.assertIn('<option value="done_error">완료(오류, 출력 없음)</option>', html_output)

    def test_rows_carry_data_attributes_for_filtering(self) -> None:
        statuses = [
            {
                "run_id": "run-1",
                "domain": "cloud-ops",
                "team_pattern": "fan_out_judge",
                "status": "done_success",
                "started_at": None,
            }
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn('data-domain="cloud-ops"', html_output)
        self.assertIn('data-pattern="fan_out_judge"', html_output)
        self.assertIn('data-status="done_success"', html_output)

    def test_filter_bar_survives_unknown_status(self) -> None:
        """직전 버그 회귀 테스트: STATUS_LABELS에 없는 상태값이 있어도
        KeyError 없이 원본 값을 그대로 라벨로 써야 한다."""
        statuses = [
            {
                "run_id": "run-1",
                "domain": "d",
                "team_pattern": "direct_call",
                "status": "some_future_status",
                "started_at": None,
            }
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn('<option value="some_future_status">some_future_status</option>', html_output)

    def test_dashboard_links_to_separate_guide_page(self) -> None:
        """2026-07-23 사용자 요청: "가이드를 하단에 두지 말고 따로 페이지로
        빼줘" — 대시보드 본문에는 가이드 내용이 아니라 별도 파일로 가는 링크만
        있어야 한다."""
        html_output = live_status.render_html([])

        self.assertNotIn('<section id="guide">', html_output)
        self.assertIn(f'<a href="{live_status.DEFAULT_GUIDE_FILENAME}">', html_output)

    def test_dashboard_guide_href_is_customizable(self) -> None:
        html_output = live_status.render_html([], guide_href="custom-guide.html")

        self.assertIn('<a href="custom-guide.html">', html_output)

    def test_guide_page_links_back_to_dashboard(self) -> None:
        html_output = live_status.render_guide_html(back_href="overview.html")

        self.assertIn('<a href="overview.html">', html_output)

    def test_guide_page_explains_all_status_values(self) -> None:
        html_output = live_status.render_guide_html()

        for label in live_status.STATUS_LABELS.values():
            self.assertIn(label, html_output)

    def test_empty_list_renders_placeholder_row(self) -> None:
        html_output = live_status.render_html([])
        self.assertIn("<html", html_output)
        self.assertIn("run이 없다", html_output)

    def test_status_row_includes_run_id_and_label(self) -> None:
        statuses = [{"run_id": "run-1", "team_pattern": "fan_out_judge", "status": "running", "started_at": None}]

        html_output = live_status.render_html(statuses)

        self.assertIn("run-1", html_output)
        self.assertIn("fan_out_judge", html_output)
        self.assertIn("실행 중", html_output)

    def test_unknown_status_falls_back_to_raw_value(self) -> None:
        statuses = [
            {"run_id": "run-x", "team_pattern": "direct_call", "status": "some_future_status", "started_at": None}
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn("some_future_status", html_output)

    def test_prompt_is_shown_when_present(self) -> None:
        statuses = [
            {
                "run_id": "run-1",
                "team_pattern": "fan_out_judge",
                "status": "done_success",
                "started_at": None,
                "prompt": "이 설계안을 검토해줘",
            }
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn("이 설계안을 검토해줘", html_output)

    def test_short_prompt_renders_directly_without_details(self) -> None:
        """접었다 펼치는 <details>는 긴 텍스트에만 필요 — 짧은 요청 내용은
        그냥 바로 보여준다(2026-07-23 사용자 요청: 간략한 주제만 보이고 상세는
        클릭해야 나오는 형태로)."""
        statuses = [
            {
                "run_id": "run-1",
                "team_pattern": "direct_call",
                "status": "done_success",
                "started_at": None,
                "prompt": "짧은 요청",
            }
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn("짧은 요청", html_output)
        self.assertNotIn("<details>", html_output)

    def test_long_prompt_collapses_behind_details_summary(self) -> None:
        long_prompt = (
            "이 설계안을 검토해줘: 마이크로서비스로 분리할지, 모놀리식으로 유지할지 비교 분석 부탁해."
        )
        statuses = [
            {
                "run_id": "run-1",
                "team_pattern": "fan_out_judge",
                "status": "done_success",
                "started_at": None,
                "prompt": long_prompt,
            }
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn("<details>", html_output)
        self.assertIn("<summary>", html_output)
        # 요약(summary)엔 앞부분만, 본문(full-text)엔 전체 텍스트가 들어있어야 한다.
        self.assertIn(long_prompt[:20], html_output)
        self.assertIn(long_prompt, html_output)
        self.assertLess(html_output.index("<summary>"), html_output.index('class="full-text"'))

    def test_task_id_appears_first(self) -> None:
        statuses = [
            {
                "run_id": "run-fan-out-demo",
                "task_id": "fan-out-demo",
                "team_pattern": "fan_out_judge",
                "status": "done_success",
                "started_at": None,
            }
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn("<td>fan-out-demo</td>", html_output)
        self.assertLess(html_output.index("<td>fan-out-demo</td>"), html_output.index("<td>run-fan-out-demo</td>"))

    def test_domain_appears_before_task_id(self) -> None:
        statuses = [
            {
                "run_id": "run-1",
                "task_id": "fan-out-demo",
                "domain": "cloud-ops",
                "team_pattern": "fan_out_judge",
                "status": "done_success",
                "started_at": None,
            }
        ]

        html_output = live_status.render_html(statuses)

        self.assertIn("<td>cloud-ops</td>", html_output)
        self.assertLess(html_output.index("<td>cloud-ops</td>"), html_output.index("<td>fan-out-demo</td>"))

    def test_missing_domain_renders_dash(self) -> None:
        statuses = [{"run_id": "run-1", "team_pattern": "direct_call", "status": "done_success", "started_at": None}]

        html_output = live_status.render_html(statuses)

        self.assertIn("<td>-</td>", html_output)

    def test_missing_prompt_renders_dash(self) -> None:
        statuses = [{"run_id": "run-1", "team_pattern": "direct_call", "status": "done_success", "started_at": None}]

        html_output = live_status.render_html(statuses)

        self.assertIn("<td>-</td>", html_output)


if __name__ == "__main__":
    unittest.main()
