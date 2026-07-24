"""Step 0 잔존 테스트 (stdlib unittest, 외부 패키지 불필요).

Step 0 당시에는 Planner가 없어서 `orchestrator.run(task, plan, root=...)`처럼 Plan을
호출부가 직접 만들어 넘겼다. Step 8에서 orchestrator가 완성되며 `run(task, providers)`
로 시그니처가 바뀌었고(Planner가 내부에서 Plan을 만듦), 그때 검증하던
"두 패턴 디렉토리 구조/재현성" 테스트는 이제 `test_step9_integration.py`가 실제
provider까지 갖춰서 더 폭넓게 검증한다(대체됨, 삭제).

여기 남겨둔 두 테스트는 orchestrator의 공개 시그니처와 무관하게 여전히 유효한
저수준 계약 검증이다.
- pydantic이 생성 시점에 Literal 외 값을 즉시 거부하는가 (강한 타입 검증)
- `model_construct()`로 검증을 우회해 만든(예: 구버전 데이터 역직렬화) Plan이
  dispatcher에 도달했을 때도 orchestrator 자체의 방어 로직이 작동하는가

실행: python -m unittest tests/test_step0_smoke.py -v  (harness-mvp 디렉토리에서)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import orchestrator, run_store  # noqa: E402
from harness.schemas import Plan, TaskInput  # noqa: E402


class Step0ContractTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_invalid_team_pattern_rejected_at_construction(self) -> None:
        """pydantic이 생성 시점에 Literal 외 값을 즉시 거부하는지 확인 (강한 타입 검증)."""
        with self.assertRaises(ValueError):
            Plan(task_id="t-c", task_type="x", team_pattern="something_else")  # type: ignore[arg-type]

    def test_unknown_team_pattern_raises_in_dispatcher(self) -> None:
        """model_construct()로 검증을 우회해 만든 Plan이 내부 dispatch 함수에 도달했을 때도
        orchestrator 자체의 방어 로직이 작동하는지 확인한다 (Plan 생성 자체는 pydantic이
        막지만, 예컨대 구버전 데이터 역직렬화처럼 검증을 우회한 데이터가 들어올 가능성에
        대비한 마지막 방어선)."""
        task = TaskInput(task_id="t-c", prompt="테스트")
        plan = Plan.model_construct(task_id="t-c", team_pattern="something_else")
        run_dir = run_store.create_run(run_id="run-t-c", root=self.tmp_dir)

        with self.assertRaises(ValueError):
            orchestrator._run_pattern(task, plan, providers={}, run_dir=run_dir)


if __name__ == "__main__":
    unittest.main()
