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
        # ADR 0009(2026-07-29)로 체인이 opt-in 전용이 됐다 — 프롬프트 키워드만으로는
        # fan_out_judge로 간다. 이 테스트가 검증하는 건 체인의 최종 산출물 구성이므로
        # 명시적으로 진입시킨다.
        task = TaskInput(
            task_id=task_id, prompt=_RESEARCH_PROMPT, constraints=["team_pattern:hierarchical_delegation"]
        )
        orchestrator.run(task, delegation_providers(**kwargs), root=self.tmp_dir)
        run_dir = self.tmp_dir / f"run-{task_id}"
        return run_dir, run_store.read_markdown(run_dir, "final.md")

    def test_final_is_the_last_non_review_step_only(self) -> None:
        """final.md는 **발행물 한 개**다 — 검토 역할이 아닌 마지막 스텝 (ADR 0013).

        ADR 0008은 "성공한 모든 스텝을 `## N. 역할`로 엮기"였는데, 그러면 final.md가
        **같은 문서의 두 판본(초안·최종본) + 그 사이 리뷰**가 된다(실측 424줄).
        발행물이 아니라 공정 기록이라 ADR 0013으로 개정했다.
        """
        run_dir, final = self.run_chain_task("chain-publishable-only")

        plan = run_store.read_json(run_dir, "plan.json")
        roles = [step["role"] for step in plan["delegation_chain"]]
        self.assertEqual(roles, ["research", "design_review", "content_finalization"])  # 전제

        expected = subagent_runner.read_step_content(
            run_dir,
            DelegationStep(role="content_finalization", provider_id="content_finalization-mock",
                           output_ref="artifacts/chain/step-3-content_finalization.md"),
        )
        # `finalize`가 `rstrip()` 후 개행 하나를 붙여 쓴다 — 그 형식까지 맞춰 비교한다.
        self.assertEqual(final, expected + "\n")

        # 역할 제목(`## N. 역할`)이 발행물에 안 남는다.
        for role in roles:
            with self.subTest(role=role):
                self.assertNotIn(f". {role}", final)

        # **"리뷰 본문이 final에 없다"는 이 mock으로 검증할 수 없다.** `MockProvider`가
        # 입력 프롬프트를 되풀이하고 체인은 누적 히스토리를 넘기므로,
        # `content_finalization`의 출력 자체에 리뷰 텍스트가 들어 있다(실제 LLM은 그러지
        # 않는다 — mock 경로와 실제 경로의 불일치, v22 §6).
        # 대신 **위의 완전 일치(assertEqual)가 그 성질을 이미 보장한다** — 발행물이 그
        # 스텝의 본문과 정확히 같으므로 다른 스텝이 덧붙지 않았다.

    def test_all_steps_remain_as_artifacts(self) -> None:
        """발행물에서 뺀 스텝이 **사라지는 게 아니다** — 스텝 파일은 그대로 남는다.

        ADR 0013이 ADR 0008의 원래 우려("요청물이 안 보인다")를 되살리지 않는다는 확인.
        """
        run_dir, _ = self.run_chain_task("chain-artifacts-kept")

        roles = ["research", "design_review", "content_finalization"]
        for index, role in enumerate(roles, start=1):
            with self.subTest(role=role):
                path = run_dir / "artifacts" / "chain" / f"step-{index}-{role}.md"
                self.assertTrue(path.is_file(), f"{path.name}이 없다")
                self.assertIn(role, path.read_text(encoding="utf-8"))

    def test_review_only_chain_publishes_its_last_review(self) -> None:
        """검토만으로 이뤄진 체인은 **리뷰가 곧 요청물**이다 (ADR 0013 폴백).

        `sequential_review` 기본 구성이 `[design_review, implementation_review]`로 둘 다
        검토 역할이다. 폴백이 없으면 그 체인의 final.md가 빈다.
        """
        steps = [
            DelegationStep(role="design_review", provider_id="design_review-mock",
                           output_ref="artifacts/chain/step-1-design_review.md", status="success"),
            DelegationStep(role="implementation_review", provider_id="implementation_review-mock",
                           output_ref="artifacts/chain/step-2-implementation_review.md",
                           status="success"),
        ]
        run_dir = run_store.create_run(run_id="run-review-only", root=self.tmp_dir)
        for step, body in zip(steps, ("설계 리뷰 본문", "구현 리뷰 본문")):
            run_store.write_markdown(
                run_dir, step.output_ref,
                # `- latency_ms:`가 본문 시작 판정 기준이다
                # (`subagent_runner._STEP_META_LAST_FIELD`) — 없으면 헤더가 안 걷히고
                # 파일이 통째로 반환된다.
                f"# Chain Step {step.role} ({step.provider_id})\n\n- status: success\n"
                f"- latency_ms: 10\n\n{body}\n",
            )

        composed = orchestrator._render_chain_final(run_dir, steps)

        self.assertEqual(composed, "구현 리뷰 본문")

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
