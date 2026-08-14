"""매니저-워커 위임의 조립 테스트 (ADR 0014, 2026-08-13).

`test_chain_final_composition.py`를 대체한다. 그 파일은 체인의 "마지막 비검토 스텝을
최종본으로 고른다"(ADR 0013)를 고정했는데, 재작성으로 **고를 대상 자체가 없어졌다** —
조각은 전부 살아서 하나의 문서가 된다.

이전 파일에서 이어받는 의도가 하나 있다: **내부 메타데이터가 발행물에 새어 들어가지
않는가.** 체인 시절 스텝 파일에 디버깅용 헤더("# Chain Step ... - tokens: ...")를
넣어두고 그걸 그대로 final.md에 실어, 발행물에 공정 기록이 섞였다. 조각 파일은 본문만
담아야 한다.

여기서 고정하는 것:

- 매니저가 조각 본문을 보지 않는다 (concat 모드 — **이 패턴의 존재 이유**)
- 조각끼리 서로를 보지 않는다 (입력 증폭이 선형인 근거)
- 성공한 조각 본문이 전부 최종본에 남고, 순서가 계획과 같다
- 조각 파일에 메타데이터 헤더가 없다
- 조각 수 상한을 매니저 판단에 맡기지 않는다
- 조립이 실패해도 이어붙인 초안을 버리지 않는다
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import delegation, run_store  # noqa: E402
from harness.schemas import Candidate, DelegationPlan, ProviderConfig, WorkerPart  # noqa: E402
from providers.base import Provider  # noqa: E402


class _RecordingWorker(Provider):
    """받은 프롬프트를 전부 기록하는 워커. "조각끼리 서로를 보나"를 확인하려면 필요하다."""

    def __init__(self, model_id: str = "worker-mock") -> None:
        super().__init__(ProviderConfig(provider_id=model_id, model_id=model_id))
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.prompts.append(prompt)
        return Candidate(
            model_id=self.model_id,
            content=f"본문-{len(self.prompts)}: 이 섹션의 실제 내용이다.",
            tokens=5, latency_ms=1, cost_usd=None, status="success",
        )


class _ScriptedManager(Provider):
    """정해진 응답을 순서대로 돌려주는 매니저."""

    def __init__(self, responses: list[str]) -> None:
        super().__init__(ProviderConfig(provider_id="manager", model_id="manager-mock"))
        self.responses = list(responses)
        self.prompts: list[str] = []

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.prompts.append(prompt)
        content = self.responses.pop(0) if self.responses else "{}"
        return Candidate(
            model_id=self.model_id, content=content, tokens=3,
            latency_ms=1, cost_usd=None, status="success",
        )


def _plan_json(count: int, *, titles: list[str] | None = None) -> str:
    titles = titles or [f"섹션 {i}" for i in range(1, count + 1)]
    return json.dumps(
        {
            "document_title": "테스트 문서",
            "intro": "이 문서는 요청 범위를 섹션별로 다룬다.",
            "parts": [{"title": t, "instruction": f"{t}을 작성하라."} for t in titles],
        },
        ensure_ascii=False,
    )


class DelegationCompositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="delegation-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-delegation", root=self.tmp_dir)
        self.request = "백업 복구 절차서를 작성해줘. 대상은 리눅스 서버다."

    def _run(self, *, parts: int = 3, mode: str = "concat", extra: list[str] | None = None):
        manager = _ScriptedManager([_plan_json(parts), *(extra or [])])
        worker = _RecordingWorker()
        plan, _ = delegation.decompose(self.request, manager)
        delegation.run_workers(self.request, plan.parts, [worker], self.run_dir)
        final, assembler = delegation.assemble(
            self.request, plan, self.run_dir, mode=mode, manager=manager
        )
        return plan, manager, worker, final, assembler

    def test_manager_never_sees_part_bodies_in_concat_mode(self) -> None:
        """**이 패턴의 존재 이유.** 매니저 호출은 분해 1회뿐이고, 어떤 조각 본문도 안 받는다."""
        _, manager, worker, final, assembler = self._run()

        self.assertEqual(len(manager.prompts), 1)  # 분해만 — 조립은 LLM 호출 0회
        self.assertIsNone(assembler)
        for body in (c for c in ("본문-1", "본문-2", "본문-3")):
            self.assertNotIn(body, manager.prompts[0])
        # 그런데 그 본문들은 최종본에 다 들어 있다 — 매니저를 거치지 않고 파일에서 왔다
        self.assertIn("본문-1", final)
        self.assertIn("본문-3", final)
        self.assertEqual(len(worker.prompts), 3)

    def test_parts_do_not_see_each_other(self) -> None:
        """입력 증폭이 조각 수에 **선형**인 근거. 체인은 앞 출력을 다시 실어 제곱이 됐다."""
        _, _, worker, _, _ = self._run()

        # 2·3번째 워커 프롬프트에 앞 조각의 본문이 없어야 한다
        self.assertNotIn("본문-1", worker.prompts[1])
        self.assertNotIn("본문-1", worker.prompts[2])
        self.assertNotIn("본문-2", worker.prompts[2])
        # 대신 원본 요청은 모두 받는다(자기 범위를 알기 위해)
        for prompt in worker.prompts:
            self.assertIn(self.request, prompt)

    def test_every_successful_part_survives_in_plan_order(self) -> None:
        """조각은 골라내는 게 아니라 전부 남는다 — 그리고 순서가 계획과 같아야 재현된다."""
        plan, _, _, final, _ = self._run(parts=3)

        positions = [final.index(f"## {p.title}") for p in plan.parts]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("# 테스트 문서", final)
        self.assertIn("이 문서는 요청 범위를 섹션별로 다룬다.", final)

    def test_part_files_contain_body_only_no_metadata_header(self) -> None:
        """이전 파일에서 이어받은 회귀 방지: 공정 기록이 발행물에 새어 들어가지 않는다."""
        plan, _, _, final, _ = self._run(parts=2)

        for part in plan.parts:
            body = run_store.read_markdown(self.run_dir, part.output_ref)
            self.assertTrue(body.startswith("본문-"), f"헤더가 섞였다: {body[:60]!r}")
            for leaked in ("status:", "tokens:", "cost_usd", "# Chain Step"):
                self.assertNotIn(leaked, body)
        self.assertNotIn("tokens:", final)

    def test_part_records_size_and_path_but_not_content(self) -> None:
        """조각 레코드는 본문을 갖지 않는다 — 그게 `WorkerPart`에 content가 없는 이유다."""
        plan, _, _, _, _ = self._run(parts=2)

        for part in plan.parts:
            self.assertEqual(part.status, "success")
            self.assertTrue(part.output_ref.startswith("artifacts/parts/"))
            self.assertGreater(part.chars, 0)
            self.assertFalse(hasattr(part, "content"))

    def test_max_parts_is_enforced_by_code_not_by_the_manager(self) -> None:
        """매니저가 상한을 넘겨 제안해도 잘라낸다 — 조각 하나가 워커 호출 하나다."""
        manager = _ScriptedManager([_plan_json(10)])

        plan, _ = delegation.decompose(self.request, manager, max_parts=3)

        self.assertEqual(len(plan.parts), 3)

    def test_llm_assembly_sends_bodies_once_and_costs_a_call(self) -> None:
        """`llm` 모드는 정합성을 얻고 조각 총량만큼 토큰을 쓰는 교환이다."""
        _, manager, _, final, assembler = self._run(mode="llm", extra=["[편집됨] 하나로 합친 문서"])

        self.assertEqual(len(manager.prompts), 2)  # 분해 + 조립
        self.assertIn("본문-1", manager.prompts[1])  # 조립 프롬프트에는 본문이 들어간다
        self.assertIsNotNone(assembler)
        self.assertIn("[편집됨]", final)

    def test_llm_assembly_failure_keeps_the_concatenated_draft(self) -> None:
        """조립이 실패해도 산출물을 버리지 않는다 — 이어붙인 초안이 이미 유효한 문서다."""

        class _FailingManager(_ScriptedManager):
            def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
                if "## 초안" in prompt:
                    raise RuntimeError("조립 호출 실패 주입")
                return super().generate(prompt, temperature=temperature)

        manager = _FailingManager([_plan_json(2)])
        worker = _RecordingWorker()
        plan, _ = delegation.decompose(self.request, manager)
        delegation.run_workers(self.request, plan.parts, [worker], self.run_dir)

        final, assembler = delegation.assemble(
            self.request, plan, self.run_dir, mode="llm", manager=manager
        )

        self.assertEqual(assembler.status, "error")
        self.assertIn("본문-1", final)  # 초안이 살아 있다
        self.assertIn("# 테스트 문서", final)


class DelegationFailureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="delegation-fail-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-fail", root=self.tmp_dir)

    def test_non_json_plan_is_an_error_not_a_guess(self) -> None:
        manager = _ScriptedManager(["JSON이 아닌 산문 응답입니다."])

        with self.assertRaises(delegation.DelegationError):
            delegation.decompose("요청", manager)

    def test_plan_without_parts_is_an_error(self) -> None:
        manager = _ScriptedManager(['{"document_title": "제목", "intro": "머리글", "parts": []}'])

        with self.assertRaises(delegation.DelegationError):
            delegation.decompose("요청", manager)

    def test_part_missing_instruction_is_dropped_not_sent_blank(self) -> None:
        """지시가 빈 조각을 워커에 보내면 아무 맥락 없이 생성하게 된다 — 버린다."""
        manager = _ScriptedManager([
            json.dumps({
                "document_title": "제목", "intro": "머리글",
                "parts": [{"title": "쓸 수 있는 섹션", "instruction": "작성하라"},
                          {"title": "빈 섹션", "instruction": "   "}],
            }, ensure_ascii=False)
        ])

        plan, _ = delegation.decompose("요청", manager)

        self.assertEqual([p.title for p in plan.parts], ["쓸 수 있는 섹션"])

    def test_assemble_without_any_successful_part_is_an_error(self) -> None:
        plan = DelegationPlan(
            document_title="제목", intro="머리글",
            parts=[WorkerPart(title="섹션", instruction="작성", status="error")],
        )

        with self.assertRaises(delegation.DelegationError):
            delegation.assemble("요청", plan, self.run_dir)

    def test_no_worker_is_an_error_not_a_silent_manager_fallback(self) -> None:
        """워커가 없으면 실패한다 — 매니저로 조용히 대체하면 아끼려던 걸 그대로 쓴다."""
        parts = [WorkerPart(title="섹션", instruction="작성")]

        with self.assertRaises(delegation.DelegationError):
            delegation.run_workers("요청", parts, [], self.run_dir)


if __name__ == "__main__":
    unittest.main()
