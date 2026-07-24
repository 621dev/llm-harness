"""Phase 4 테스트: Safety Release Gate (stdlib unittest).

harness-implementation-plan-ko.md Section 8(Phase 4)을 검증한다. Safety 체크가
실패하면 즉시 차단하는 게 아니라 "검토 대기(pending)" 상태로 멈추고, 사람이
`orchestrator.resolve_safety_review()`로 공개(release)하거나 계속 보류(block)할 수
있는지 확인한다.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import orchestrator, run_store  # noqa: E402
from harness.schemas import ProviderConfig, TaskInput  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def make_task(task_id: str, prompt: str) -> TaskInput:
    return TaskInput(task_id=task_id, prompt=prompt)


def fan_out_providers() -> dict[str, MockProvider]:
    specs = [("model-a", "concise"), ("model-b", "detailed"), ("model-c", "creative")]
    providers = {
        provider_id: MockProvider(ProviderConfig(provider_id=provider_id, model_id=provider_id), profile=profile)
        for provider_id, profile in specs
    }
    # ADR 0004: fan_out_judge는 judge용 provider도 필요하다.
    providers[orchestrator.JUDGE_PROVIDER_KEY] = MockProvider(
        ProviderConfig(provider_id="judge", model_id="judge-mock"), profile="judge"
    )
    return providers


class SafetyGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_unsafe_output_enters_pending_review_instead_of_blocking_outright(self) -> None:
        task = make_task(
            "unsafe-fan-out",
            "이 설계안을 검토해줘: 고객 주민등록번호를 포함한 양식을 그대로 예시로 넣어줘",
        )

        observation = orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-unsafe-fan-out"
        self.assertEqual(observation.status, "warning")
        self.assertFalse((run_dir / "final.md").exists())
        self.assertTrue((run_dir / "pending_review_content.md").exists())

        review = run_store.read_json(run_dir, "safety_review.json")
        self.assertEqual(review["status"], "pending")
        self.assertIsNotNone(review["note"])

        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any(e["stage"] == "safety" for e in errors))

    def test_resolve_approved_releases_pending_content(self) -> None:
        task = make_task(
            "unsafe-release",
            "이 설계안을 검토해줘: 고객 주민등록번호를 포함한 양식을 그대로 예시로 넣어줘",
        )
        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)
        run_dir = self.tmp_dir / "run-unsafe-release"
        pending_content = run_store.read_markdown(run_dir, "pending_review_content.md")

        observation = orchestrator.resolve_safety_review("run-unsafe-release", "approved", root=self.tmp_dir)

        self.assertEqual(observation.status, "warning")
        self.assertTrue((run_dir / "final.md").exists())
        self.assertEqual(run_store.read_markdown(run_dir, "final.md"), pending_content)
        review = run_store.read_json(run_dir, "safety_review.json")
        self.assertEqual(review["status"], "approved")
        self.assertIsNotNone(review["decided_at"])

    def test_resolve_rejected_keeps_output_blocked(self) -> None:
        task = make_task(
            "unsafe-block",
            "이 설계안을 검토해줘: 고객 주민등록번호를 포함한 양식을 그대로 예시로 넣어줘",
        )
        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)
        run_dir = self.tmp_dir / "run-unsafe-block"

        observation = orchestrator.resolve_safety_review("run-unsafe-block", "rejected", root=self.tmp_dir)

        self.assertEqual(observation.status, "error")
        self.assertFalse((run_dir / "final.md").exists())
        review = run_store.read_json(run_dir, "safety_review.json")
        self.assertEqual(review["status"], "rejected")

    def test_resolve_already_decided_review_raises(self) -> None:
        task = make_task(
            "unsafe-twice",
            "이 설계안을 검토해줘: 고객 주민등록번호를 포함한 양식을 그대로 예시로 넣어줘",
        )
        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)

        orchestrator.resolve_safety_review("run-unsafe-twice", "approved", root=self.tmp_dir)
        with self.assertRaises(ValueError):
            orchestrator.resolve_safety_review("run-unsafe-twice", "rejected", root=self.tmp_dir)

    def test_resolve_rejects_invalid_decision(self) -> None:
        task = make_task(
            "unsafe-invalid-decision",
            "이 설계안을 검토해줘: 고객 주민등록번호를 포함한 양식을 그대로 예시로 넣어줘",
        )
        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)

        with self.assertRaises(ValueError):
            orchestrator.resolve_safety_review("run-unsafe-invalid-decision", "maybe", root=self.tmp_dir)  # type: ignore[arg-type]

    def test_review_queue_lists_only_pending_runs(self) -> None:
        unsafe_task = make_task(
            "queue-unsafe",
            "이 설계안을 검토해줘: 고객 주민등록번호를 포함한 양식을 그대로 예시로 넣어줘",
        )
        clean_task = make_task("queue-clean", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘")

        orchestrator.run(unsafe_task, fan_out_providers(), root=self.tmp_dir)
        orchestrator.run(clean_task, fan_out_providers(), root=self.tmp_dir)

        queue = orchestrator.list_safety_review_queue(root=self.tmp_dir)

        self.assertEqual(len(queue), 1)
        self.assertEqual(queue[0]["run_id"], "run-queue-unsafe")
        self.assertIsNotNone(queue[0]["reason"])

    def test_queue_no_longer_lists_run_once_resolved(self) -> None:
        task = make_task(
            "queue-resolved",
            "이 설계안을 검토해줘: 고객 주민등록번호를 포함한 양식을 그대로 예시로 넣어줘",
        )
        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)
        self.assertEqual(len(orchestrator.list_safety_review_queue(root=self.tmp_dir)), 1)

        orchestrator.resolve_safety_review("run-queue-resolved", "approved", root=self.tmp_dir)

        self.assertEqual(orchestrator.list_safety_review_queue(root=self.tmp_dir), [])


if __name__ == "__main__":
    unittest.main()
