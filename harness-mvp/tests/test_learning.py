"""run 간 학습 테스트 (2026-07-29 도입).

**해결한 갭**: 하네스가 run 사이에 아무것도 배우지 않았다 — 매 run이 백지에서
시작하고 축적은 사람이 읽는 문서에만 쌓였다(ECC 재분석에서 확인한 최대 갭).

**여기서 고정하는 핵심은 "자동 집계가 그대로 주입되지 않는다"는 것이다.** 사용자
결정이 "기록은 자동, 반영은 명시적"이었고, 그 이유는 잘못된 학습이 누적되면 판정을
조용히 오염시키기 때문이다("실패를 조용히 감추지 않는다"와 같은 결). 이 경계가
무너지면 이 기능은 위험한 기능이 된다.

그 외 고정하는 것:
- run이 끝나면 관측이 자동으로 쌓인다(파일 기반이라 run 산출물만 읽어서 만든다)
- 예산 중단을 provider 실패로 세지 않는다 — 문구가 아니라 `kind` 필드로 분류
- 학습 기록이 실패해도 run은 살아남는다(부가 기능이 본체를 죽이면 안 된다)
- 주입한 내용을 run에 복사한다(`learned.md`는 바뀌므로 없으면 재현/재해석 불가)
"""
from __future__ import annotations

import argparse
import io
import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import learning, orchestrator, run_store  # noqa: E402
from harness.schemas import ProviderConfig, TaskInput  # noqa: E402
from providers.mock import MockProvider  # noqa: E402

_FAN_OUT_PROMPT = "NCP와 AWS 스토리지 비용 구조를 비교해서 정리해줘. 항목별 근거도 같이."


def fan_out_providers() -> dict[str, MockProvider]:
    providers = {
        name: MockProvider(ProviderConfig(provider_id=name, model_id=name), profile="detailed")
        for name in ("model-a", "model-b")
    }
    providers[orchestrator.JUDGE_PROVIDER_KEY] = MockProvider(
        ProviderConfig(provider_id="judge", model_id="judge"), profile="judge"
    )
    return providers


class RecordRunTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="learning-record-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def make_run(self, run_id: str, *, judging: dict | None = None, errors: list | None = None) -> Path:
        run_dir = run_store.create_run(run_id=run_id, root=self.tmp_dir)
        run_store.write_json(run_dir, "plan.json", {"task_id": "t", "team_pattern": "fan_out_judge"})
        run_store.write_json(run_dir, "metrics.json", {"estimated_cost_usd": 0.02, "subscription_calls": 1})
        run_store.write_json(run_dir, "errors.json", errors or [])
        if judging is not None:
            run_store.write_json(run_dir, "judging.json", judging)
        return run_dir

    def test_observation_is_appended_per_run(self) -> None:
        learning.record_run(self.make_run("run-1"))
        learning.record_run(self.make_run("run-2"))

        records = learning.read_observations(root=self.tmp_dir)

        self.assertEqual([r["run_id"] for r in records], ["run-1", "run-2"])

    def test_winner_and_cost_are_captured(self) -> None:
        run_dir = self.make_run(
            "run-w", judging={"winner": "model-b", "scores": [{"candidate": "model-b", "score": 0.9}]}
        )

        record = learning.record_run(run_dir)

        self.assertEqual(record["winner"], "model-b")
        self.assertEqual(record["cost_usd"], 0.02)

    def test_budget_stop_is_not_counted_as_provider_failure(self) -> None:
        """문구가 아니라 `kind`로 분류한다 — 섞이면 "이 provider가 자주 실패한다"는
        잘못된 학습이 생긴다(예산 때문에 호출조차 안 된 것까지 실패로 잡힌다)."""
        run_dir = self.make_run(
            "run-b",
            errors=[
                {"kind": "budget", "stage": "fan_out_judge", "message": "예산 상한 도달: ..."},
                {"kind": "candidate_failure", "provider": "model-a", "stage": "candidate 'model-a'",
                 "message": "재시도까지 실패: ..."},
            ],
        )

        record = learning.record_run(run_dir)

        self.assertTrue(record["budget_stopped"])
        self.assertEqual(record["failed_providers"], ["model-a"])

    def test_broken_line_does_not_break_the_whole_log(self) -> None:
        """append-only 로그의 장점을 지키려면 한 줄이 깨져도 나머지를 읽어야 한다."""
        learning.record_run(self.make_run("run-ok"))
        path = self.tmp_dir / learning.LEARNED_DIRNAME / learning.OBSERVATIONS_FILENAME
        with path.open("a", encoding="utf-8") as handle:
            handle.write("{깨진 JSON\n")

        self.assertEqual(len(learning.read_observations(root=self.tmp_dir)), 1)

    def test_summarize_gives_numbers_not_conclusions(self) -> None:
        for index in range(3):
            self.make_run(f"run-s{index}", judging={"winner": "model-b", "scores": []})
            learning.record_run(self.tmp_dir / f"run-s{index}")

        summary = learning.summarize(root=self.tmp_dir)

        self.assertEqual(summary["runs"], 3)
        self.assertEqual(summary["wins"], {"model-b": 3})
        # 예산 상한을 얼마로 둘지 정할 근거 — 도입 당시 참고할 실측치가 없었다
        self.assertEqual(summary["cost_usd"]["samples"], 3)
        self.assertEqual(summary["cost_usd"]["mean"], 0.02)


class RecordingIsAutomaticTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="learning-auto-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_finished_run_records_itself(self) -> None:
        task = TaskInput(task_id="auto", prompt=_FAN_OUT_PROMPT)

        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)

        records = learning.read_observations(root=self.tmp_dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["team_pattern"], "fan_out_judge")

    def test_learning_failure_does_not_lose_the_run(self) -> None:
        """부가 기능이 완성된 산출물을 죽이면 배보다 배꼽이 크다."""
        task = TaskInput(task_id="auto-fail", prompt=_FAN_OUT_PROMPT)

        with mock.patch.object(learning, "record_run", side_effect=OSError("디스크 오류")):
            observation = orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-auto-fail"
        self.assertTrue(run_store.read_markdown(run_dir, "final.md"))  # 산출물은 남았다
        self.assertEqual(observation.status, "warning")  # 조용히 넘기지도 않는다
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any(e.get("stage") == "learning" for e in errors))


class LearnCommandTest(unittest.TestCase):
    """`cli learn`을 실제로 호출한다 — 모듈 함수만 테스트하면 커맨드가 깨진 걸 못 잡는다.

    실제로 그랬다(2026-07-29): 출력에 `print`가 아니라 `_out()`을 썼는데, `_out`은
    subprocess 결과의 `stdout`이 None일 때를 막는 헬퍼라 문자열을 주면
    `AttributeError`로 죽었다. 모듈 테스트 13개가 전부 통과하는 동안 커맨드는
    첫 줄에서 죽고 있었다.
    """

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="learning-cmd-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def run_learn(self) -> str:
        from harness import cli

        buffer = io.StringIO()
        with mock.patch("sys.stdout", buffer), mock.patch(
            "harness.run_store.DEFAULT_WORKSPACE_ROOT", self.tmp_dir
        ), mock.patch("harness.learning.Path.cwd", return_value=self.tmp_dir):
            exit_code = cli.cmd_learn(argparse.Namespace())
        self.assertEqual(exit_code, 0)
        return buffer.getvalue()

    def test_empty_state_says_so_without_crashing(self) -> None:
        output = self.run_learn()

        self.assertIn("아직 기록된 run이 없다", output)

    def test_summary_is_printed_and_promotion_is_explained(self) -> None:
        run_dir = run_store.create_run(run_id="run-c1", root=self.tmp_dir)
        run_store.write_json(run_dir, "plan.json", {"task_id": "t", "team_pattern": "fan_out_judge"})
        run_store.write_json(run_dir, "metrics.json", {"estimated_cost_usd": 0.03, "subscription_calls": 2})
        run_store.write_json(run_dir, "errors.json", [])
        run_store.write_json(run_dir, "judging.json", {"winner": "model-b", "scores": []})
        learning.record_run(run_dir)

        output = self.run_learn()

        self.assertIn("기록된 run: 1개", output)
        self.assertIn("model-b", output)
        # 반영이 자동이 아니라는 것을 사용자에게 알려야 한다
        self.assertIn(learning.LEARNED_NOTES_FILENAME, output)


class InjectionIsExplicitTest(unittest.TestCase):
    """이 클래스가 이 파일의 핵심이다 — 자동 집계가 다음 run에 새어 들어가면 안 된다."""

    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="learning-inject-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.cwd = Path(tempfile.mkdtemp(prefix="learning-cwd-"))
        self.addCleanup(shutil.rmtree, self.cwd, ignore_errors=True)
        saved = orchestrator.USE_LEARNED_NOTES

        def restore() -> None:
            orchestrator.USE_LEARNED_NOTES = saved

        self.addCleanup(restore)

    def run_with_cwd(self, task_id: str) -> Path:
        with mock.patch("harness.learning.Path.cwd", return_value=self.cwd):
            orchestrator.run(
                TaskInput(task_id=task_id, prompt=_FAN_OUT_PROMPT), fan_out_providers(), root=self.tmp_dir
            )
        return self.tmp_dir / f"run-{task_id}"

    def write_notes(self, text: str) -> None:
        (self.cwd / learning.LEARNED_NOTES_FILENAME).write_text(text, encoding="utf-8")

    def test_auto_observations_are_never_injected(self) -> None:
        """learned.md가 없으면 관측이 아무리 쌓여도 프롬프트에 안 들어간다."""
        first = self.run_with_cwd("inject-1")
        self.assertTrue(learning.read_observations(root=self.tmp_dir))  # 관측은 쌓였다

        second = self.run_with_cwd("inject-2")

        prompt = run_store.read_json(second, "input.json")["prompt"]
        self.assertEqual(prompt, _FAN_OUT_PROMPT)  # 원문 그대로
        self.assertFalse((second / learning.INJECTED_FILENAME).exists())
        del first

    def test_human_written_notes_are_injected(self) -> None:
        self.write_notes("- model-b가 비용 비교에서 더 근거를 잘 붙인다")

        run_dir = self.run_with_cwd("inject-3")

        # input.json은 사용자가 준 원본을 남기고, 주입은 실행 프롬프트에만 적용된다
        self.assertEqual(run_store.read_json(run_dir, "input.json")["prompt"], _FAN_OUT_PROMPT)
        injected = run_store.read_markdown(run_dir, learning.INJECTED_FILENAME)
        self.assertIn("model-b", injected)

    def test_injected_content_is_copied_into_the_run(self) -> None:
        """learned.md는 시간이 지나며 바뀐다 — 사본이 없으면 그때 무엇을 학습한
        상태였는지 몰라 run을 다시 해석할 수 없다."""
        self.write_notes("첫 번째 버전")
        first = self.run_with_cwd("inject-4")
        self.write_notes("두 번째 버전")
        second = self.run_with_cwd("inject-5")

        self.assertIn("첫 번째", run_store.read_markdown(first, learning.INJECTED_FILENAME))
        self.assertIn("두 번째", run_store.read_markdown(second, learning.INJECTED_FILENAME))

    def test_injection_can_be_disabled_but_recording_continues(self) -> None:
        """반영만 끄고 기록은 계속되는지 — 끄는 순간 축적까지 멈추면 나중에 켤 때 백지다."""
        self.write_notes("무시돼야 하는 메모")
        orchestrator.USE_LEARNED_NOTES = False

        run_dir = self.run_with_cwd("inject-6")

        self.assertFalse((run_dir / learning.INJECTED_FILENAME).exists())
        self.assertTrue(learning.read_observations(root=self.tmp_dir))

    def test_empty_notes_file_is_treated_as_absent(self) -> None:
        self.write_notes("   \n\n")

        run_dir = self.run_with_cwd("inject-7")

        self.assertFalse((run_dir / learning.INJECTED_FILENAME).exists())

    def test_request_comes_after_the_reference_material(self) -> None:
        """참고 자료가 요청을 밀어내면 안 된다 — 요청이 마지막에 와야 지시로 읽힌다."""
        merged = learning.apply_to_prompt("원래 요청", "참고 내용")

        self.assertLess(merged.index("참고 내용"), merged.index("원래 요청"))


if __name__ == "__main__":
    unittest.main()
