"""Phase 6 대시보드 테스트: dashboard.build_dashboard()/render_html().

harness-implementation-plan-ko.md Section 8 Phase 6("UI / Dashboard")을 구현한
정적 HTML 리포트를 검증한다. 실제 run을 orchestrator로 돌리지 않고, run_store로
plan.json/metrics.json/errors.json/safety_review.json/approval.json/final.md만
직접 심어서 상태 판정·집계 로직만 단위 테스트한다.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import dashboard, run_store  # noqa: E402


class BuildDashboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def _seed_run(
        self,
        run_id: str,
        *,
        team_pattern: str | None = "fan_out_judge",
        final_md: bool = False,
        errors: list[dict] | None = None,
        safety_review_status: str | None = None,
        approval_status: str | None = None,
        latency_ms: int | None = None,
        cost_usd: float | None = None,
    ) -> None:
        run_dir = run_store.create_run(run_id, root=self.tmp_dir)
        if team_pattern is not None:
            run_store.write_json(run_dir, "plan.json", {"team_pattern": team_pattern})
        if latency_ms is not None or cost_usd is not None:
            run_store.write_json(
                run_dir, "metrics.json", {"latency_ms": latency_ms, "estimated_cost_usd": cost_usd}
            )
        if final_md:
            run_store.write_markdown(run_dir, "final.md", "content")
        if errors is not None:
            run_store.write_json(run_dir, "errors.json", errors)
        if safety_review_status is not None:
            run_store.write_json(run_dir, "safety_review.json", {"status": safety_review_status})
        if approval_status is not None:
            run_store.write_json(run_dir, "approval.json", {"status": approval_status})

    def test_empty_workspace_returns_zero_runs(self) -> None:
        report = dashboard.build_dashboard(root=self.tmp_dir)
        self.assertEqual(report.total_runs_scanned, 0)
        self.assertEqual(report.patterns, [])

    def test_final_md_with_no_errors_counts_as_success(self) -> None:
        self._seed_run("run-1", final_md=True, errors=[])
        report = dashboard.build_dashboard(root=self.tmp_dir)
        stats = report.patterns[0]
        self.assertEqual(stats.team_pattern, "fan_out_judge")
        self.assertEqual((stats.success_count, stats.warning_count, stats.error_count), (1, 0, 0))

    def test_final_md_with_errors_counts_as_warning(self) -> None:
        self._seed_run("run-1", final_md=True, errors=[{"stage": "candidate 'x'", "message": "재시도까지 실패"}])
        report = dashboard.build_dashboard(root=self.tmp_dir)
        stats = report.patterns[0]
        self.assertEqual((stats.success_count, stats.warning_count, stats.error_count), (0, 1, 0))

    def test_no_final_md_and_no_review_counts_as_error(self) -> None:
        self._seed_run("run-1", final_md=False, errors=[{"stage": "fan_out_judge", "message": "min_candidates 미달"}])
        report = dashboard.build_dashboard(root=self.tmp_dir)
        stats = report.patterns[0]
        self.assertEqual((stats.success_count, stats.warning_count, stats.error_count), (0, 0, 1))

    def test_safety_review_pending_counts_as_warning(self) -> None:
        self._seed_run("run-1", final_md=False, safety_review_status="pending")
        report = dashboard.build_dashboard(root=self.tmp_dir)
        stats = report.patterns[0]
        self.assertEqual((stats.success_count, stats.warning_count, stats.error_count), (0, 1, 0))

    def test_safety_review_rejected_counts_as_error(self) -> None:
        self._seed_run("run-1", final_md=False, safety_review_status="rejected")
        report = dashboard.build_dashboard(root=self.tmp_dir)
        stats = report.patterns[0]
        self.assertEqual((stats.success_count, stats.warning_count, stats.error_count), (0, 0, 1))

    def test_approval_pending_counts_as_warning(self) -> None:
        self._seed_run("run-1", final_md=False, approval_status="pending")
        report = dashboard.build_dashboard(root=self.tmp_dir)
        stats = report.patterns[0]
        self.assertEqual((stats.success_count, stats.warning_count, stats.error_count), (0, 1, 0))

    def test_approval_rejected_counts_as_error(self) -> None:
        self._seed_run("run-1", final_md=False, approval_status="rejected")
        report = dashboard.build_dashboard(root=self.tmp_dir)
        stats = report.patterns[0]
        self.assertEqual((stats.success_count, stats.warning_count, stats.error_count), (0, 0, 1))

    def test_missing_plan_json_grouped_as_direct_call(self) -> None:
        self._seed_run("run-1", team_pattern=None, final_md=True, errors=[])
        report = dashboard.build_dashboard(root=self.tmp_dir)
        self.assertEqual(report.patterns[0].team_pattern, "direct_call")

    def test_average_latency_and_cost_computed_per_pattern(self) -> None:
        self._seed_run("run-1", final_md=True, errors=[], latency_ms=100, cost_usd=0.01)
        self._seed_run("run-2", final_md=True, errors=[], latency_ms=200, cost_usd=0.03)

        report = dashboard.build_dashboard(root=self.tmp_dir)

        stats = report.patterns[0]
        self.assertEqual(stats.total_runs, 2)
        self.assertAlmostEqual(stats.avg_latency_ms, 150.0)
        self.assertAlmostEqual(stats.avg_cost_usd, 0.02)

    def test_multiple_patterns_kept_separate_and_sorted(self) -> None:
        self._seed_run("run-1", team_pattern="hierarchical_delegation", final_md=True, errors=[])
        self._seed_run("run-2", team_pattern="fan_out_judge", final_md=True, errors=[])

        report = dashboard.build_dashboard(root=self.tmp_dir)

        self.assertEqual([p.team_pattern for p in report.patterns], ["fan_out_judge", "hierarchical_delegation"])
        self.assertEqual(report.total_runs_scanned, 2)


class RenderHtmlTest(unittest.TestCase):
    def test_empty_report_renders_placeholder_row(self) -> None:
        report = dashboard.build_dashboard(root=Path(tempfile.mkdtemp(prefix="harness-test-")))
        html_output = dashboard.render_html(report)
        self.assertIn("<html", html_output)
        self.assertIn("집계된 run이 없다", html_output)

    def test_pattern_row_includes_name_and_counts(self) -> None:
        tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, tmp_dir, ignore_errors=True)
        run_dir = run_store.create_run("run-1", root=tmp_dir)
        run_store.write_json(run_dir, "plan.json", {"team_pattern": "fan_out_judge"})
        run_store.write_markdown(run_dir, "final.md", "content")
        run_store.write_json(run_dir, "errors.json", [])

        report = dashboard.build_dashboard(root=tmp_dir)
        html_output = dashboard.render_html(report)

        self.assertIn("fan_out_judge", html_output)
        self.assertIn("100.0%", html_output)  # 성공률


if __name__ == "__main__":
    unittest.main()
