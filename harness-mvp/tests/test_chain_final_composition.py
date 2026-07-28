"""체인 최종 산출물 구성 테스트 (2026-07-28, 패턴 부가가치 측정에서 나온 수정).

**고치기 전 동작**: `hierarchical_delegation`은 `steps[-1]`의 출력 파일을 그대로
final.md로 삼았다. `[research, design_review]`처럼 마지막이 "검토" 역할인 체인에서는
최종 산출물이 사용자가 요청한 내용이 아니라 **그것에 대한 리뷰 코멘트**가 됐고,
요청한 내용은 중간 산출물에만 남아 final.md에서 보이지 않았다. 게다가 스텝 파일의
디버깅용 헤더("# Chain Step ... - status: success - tokens: ...")까지 발행물에
섞여 들어갔다.

direct_call과 비교 측정했을 때 체인이 불리하게 나온 실제 원인이 이것이었다 —
품질이 아니라 "다른 물건을 내놓고 있던" 문제. 여기서 고정하는 것:

- 성공한 모든 스텝의 본문이 final.md에 남는가 (요청한 내용이 사라지지 않는가)
- 내부 메타데이터 헤더가 발행물에 안 섞이는가
- 중단된 체인(partial)도 같은 규칙으로 구성되는가
- 스텝이 하나뿐이면 불필요한 구조를 씌우지 않는가
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import orchestrator, run_store, subagent_runner  # noqa: E402
from harness.schemas import DelegationStep, ProviderConfig, TaskInput  # noqa: E402
from providers.mock import MockProvider  # noqa: E402

_RESEARCH_PROMPT = "NCP XX 서비스를 조사해줘. 그 다음 절차서를 설계하고 검토해줘."


def delegation_providers(*, fail_times: dict[str, int] | None = None) -> dict[str, MockProvider]:
    fail_times = fail_times or {}
    return {
        f"{role}-mock": MockProvider(
            ProviderConfig(provider_id=f"{role}-mock", model_id=f"{role}-mock"),
            profile="detailed",
            fail_times=fail_times.get(f"{role}-mock", 0),
        )
        for role in ("research", "design_review", "implementation_review", "content_finalization")
    }


class ChainFinalCompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="chain-final-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def run_chain_task(self, task_id: str, **kwargs) -> tuple[Path, str]:
        task = TaskInput(task_id=task_id, prompt=_RESEARCH_PROMPT)
        orchestrator.run(task, delegation_providers(**kwargs), root=self.tmp_dir)
        run_dir = self.tmp_dir / f"run-{task_id}"
        return run_dir, run_store.read_markdown(run_dir, "final.md")

    def test_all_successful_step_outputs_survive_into_final(self) -> None:
        """회귀 방지 핵심: 요청한 내용(첫 스텝 산출물)이 final.md에서 사라지면 안 된다."""
        run_dir, final = self.run_chain_task("chain-all-steps")

        plan = run_store.read_json(run_dir, "plan.json")
        roles = [step["role"] for step in plan["delegation_chain"]]
        # 2026-07-27 content_finalization 추가로 research 기본 체인이 3단계가 됨(전제 확인)
        self.assertEqual(roles, ["research", "design_review", "content_finalization"])

        for index, role in enumerate(roles, start=1):
            with self.subTest(role=role):
                self.assertIn(f"## {index}. {role}", final)
                step_body = subagent_runner.read_step_content(
                    run_dir, DelegationStep(role=role, provider_id=f"{role}-mock",
                                            output_ref=f"artifacts/chain/step-{index}-{role}.md")
                )
                self.assertIn(step_body, final)

    def test_internal_step_metadata_is_not_published(self) -> None:
        """스텝 파일의 디버깅용 헤더가 발행물에 섞이면 안 된다."""
        _, final = self.run_chain_task("chain-no-meta")

        self.assertNotIn("# Chain Step", final)
        self.assertNotIn("- status: success", final)
        self.assertNotIn("- latency_ms:", final)

    def test_partial_chain_keeps_earlier_steps_too(self) -> None:
        """중단된 체인도 같은 규칙 — 마지막 성공 스텝만 올리면 그 앞 산출물이 사라진다."""
        run_dir, final = self.run_chain_task(
            "chain-partial", fail_times={"design_review-mock": 2}
        )

        self.assertTrue(final.startswith("(partial)"))
        self.assertIn("research", final)
        # 실패한 스텝은 들어가지 않는다
        self.assertNotIn("## 2. design_review", final)

    def test_single_step_chain_has_no_section_headers(self) -> None:
        """1스텝 체인에까지 '## 1. role' 구조를 씌우면 소음이다."""
        steps = [DelegationStep(role="research", provider_id="research-mock",
                                output_ref="artifacts/chain/step-1-research.md", status="success")]
        run_dir = run_store.create_run(run_id="run-single", root=self.tmp_dir)
        run_store.write_markdown(
            run_dir, steps[0].output_ref,
            "# Chain Step research (research-mock)\n\n- status: success\n- tokens: 5\n"
            "- latency_ms: 10\n\n본문만 남아야 한다\n",
        )

        composed = orchestrator._render_chain_final(run_dir, steps)

        self.assertEqual(composed, "본문만 남아야 한다")


class ReadStepContentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="step-content-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-x", root=self.tmp_dir)

    def make_step(self, raw: str) -> DelegationStep:
        step = DelegationStep(role="research", provider_id="p",
                              output_ref="artifacts/chain/step-1-research.md")
        run_store.write_markdown(self.run_dir, step.output_ref, raw)
        return step

    def test_strips_metadata_header(self) -> None:
        step = self.make_step(
            "# Chain Step research (p)\n\n- status: success\n- tokens: 3\n"
            "- latency_ms: 12\n\n실제 본문\n\n두 번째 문단\n"
        )

        self.assertEqual(
            subagent_runner.read_step_content(self.run_dir, step), "실제 본문\n\n두 번째 문단"
        )

    def test_unknown_format_returns_whole_file(self) -> None:
        """헤더 도입 이전 run 등 형식이 다르면 본문을 잃느니 통째로 돌려준다."""
        step = self.make_step("헤더 없는 옛 형식 내용")

        self.assertEqual(
            subagent_runner.read_step_content(self.run_dir, step), "헤더 없는 옛 형식 내용"
        )


if __name__ == "__main__":
    unittest.main()
