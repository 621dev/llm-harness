"""Phase 5 실패 로그 집계 테스트: failure_analysis.analyze_failures().

harness-implementation-plan-ko.md Section 8 Phase 5("실패 로그 기반 프롬프트/스킬
개선")을 구현한 집계 장치를 검증한다. 실제 run을 orchestrator로 끝까지 돌리지 않고,
run_store로 errors.json/safety_review.json만 직접 심어서 집계 로직만 단위 테스트한다.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import failure_analysis, run_store  # noqa: E402


class AnalyzeFailuresTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def _seed_run(
        self,
        run_id: str,
        *,
        errors: list[dict[str, str]] | None = None,
        safety_note: str | None = None,
    ) -> None:
        run_dir = run_store.create_run(run_id, root=self.tmp_dir)
        run_store.write_json(run_dir, "errors.json", errors if errors is not None else [])
        if safety_note is not None:
            run_store.write_json(run_dir, "safety_review.json", {"status": "pending", "note": safety_note})

    def test_empty_workspace_returns_zero_counts(self) -> None:
        report = failure_analysis.analyze_failures(root=self.tmp_dir)

        self.assertEqual(report.total_runs_scanned, 0)
        self.assertEqual(report.runs_with_errors, 0)
        self.assertEqual(report.runs_with_safety_review, 0)
        self.assertEqual(report.error_categories, [])
        self.assertEqual(report.safety_categories, [])

    def test_run_without_errors_or_safety_review_not_counted_as_failure(self) -> None:
        self._seed_run("run-clean")

        report = failure_analysis.analyze_failures(root=self.tmp_dir)

        self.assertEqual(report.total_runs_scanned, 1)
        self.assertEqual(report.runs_with_errors, 0)
        self.assertEqual(report.runs_with_safety_review, 0)

    def test_error_stage_counted_across_runs(self) -> None:
        self._seed_run("run-1", errors=[{"stage": "safety", "message": "x"}])
        self._seed_run("run-2", errors=[{"stage": "safety", "message": "y"}])
        self._seed_run("run-3", errors=[{"stage": "chain step 'research'", "message": "z"}])

        report = failure_analysis.analyze_failures(root=self.tmp_dir)

        self.assertEqual(report.total_runs_scanned, 3)
        self.assertEqual(report.runs_with_errors, 3)
        categories = {c.key: c.count for c in report.error_categories}
        self.assertEqual(categories["safety"], 2)
        self.assertEqual(categories["chain step 'research'"], 1)
        # count가 큰 순서로 정렬돼야 한다
        self.assertEqual(report.error_categories[0].key, "safety")

    def test_safety_note_split_into_individual_findings(self) -> None:
        self._seed_run(
            "run-safety-1",
            safety_note="Safety 점검 실패: 비밀정보로 보이는 패턴 발견 (sk-); 고위험 키워드 발견 (카드번호)",
        )
        self._seed_run(
            "run-safety-2",
            safety_note="Safety 점검 실패: 고위험 키워드 발견 (카드번호)",
        )

        report = failure_analysis.analyze_failures(root=self.tmp_dir)

        self.assertEqual(report.runs_with_safety_review, 2)
        categories = {c.key: c.count for c in report.safety_categories}
        self.assertEqual(categories["고위험 키워드 발견 (카드번호)"], 2)
        self.assertEqual(categories["비밀정보로 보이는 패턴 발견 (sk-)"], 1)

    def test_example_run_ids_deduped_and_capped_at_three(self) -> None:
        for i in range(5):
            self._seed_run(f"run-{i}", errors=[{"stage": "safety", "message": "x"}])

        report = failure_analysis.analyze_failures(root=self.tmp_dir)

        safety_stage = next(c for c in report.error_categories if c.key == "safety")
        self.assertEqual(safety_stage.count, 5)
        self.assertEqual(len(safety_stage.example_run_ids), 3)
        self.assertEqual(len(set(safety_stage.example_run_ids)), 3)

    def test_missing_safety_note_falls_back_to_placeholder(self) -> None:
        run_dir = run_store.create_run("run-no-note", root=self.tmp_dir)
        run_store.write_json(run_dir, "errors.json", [])
        run_store.write_json(run_dir, "safety_review.json", {"status": "pending", "note": None})

        report = failure_analysis.analyze_failures(root=self.tmp_dir)

        categories = {c.key: c.count for c in report.safety_categories}
        self.assertEqual(categories["(사유 없음)"], 1)


if __name__ == "__main__":
    unittest.main()
