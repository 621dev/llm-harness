"""Run Store: run 디렉토리 입출력. 패턴 무관 공통 (Step 1).

디렉토리 구조는 harness-implementation-plan-ko.md Section 2 를 그대로 따른다.

_workspace/runs/<run_id>/
  input.json
  plan.md
  artifacts/
    candidates/   # fan_out_judge 전용
    chain/        # hierarchical_delegation 전용
  judging.json    # fan_out_judge 에서만 생성
  final.md
  safety.md
  metrics.json
  errors.json
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

DEFAULT_WORKSPACE_ROOT = Path("_workspace/runs")


def new_run_id() -> str:
    """재현 가능성을 해치지 않는 범위에서 고유한 run_id 를 생성한다."""
    return f"run-{uuid.uuid4().hex[:12]}"


def create_run(run_id: str | None = None, root: Path | None = None) -> Path:
    """run 디렉토리와 필수 서브디렉토리를 생성하고 경로를 반환한다.

    root 를 명시하지 않으면 호출 시점의 DEFAULT_WORKSPACE_ROOT 를 참조한다
    (테스트에서 monkeypatch로 워크스페이스 위치를 바꿀 수 있도록 함수 정의 시점이 아닌
    호출 시점에 전역을 조회한다).
    """
    root = root if root is not None else DEFAULT_WORKSPACE_ROOT
    run_id = run_id or new_run_id()
    run_dir = root / run_id
    (run_dir / "artifacts" / "candidates").mkdir(parents=True, exist_ok=True)
    (run_dir / "artifacts" / "chain").mkdir(parents=True, exist_ok=True)
    return run_dir


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, indent=2, default=str)


def write_json(run_dir: Path, name: str, data: Any) -> Path:
    """dict 또는 pydantic 모델(.model_dump() 결과)을 JSON으로 저장한다."""
    path = run_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json_dumps(data), encoding="utf-8")
    return path


def read_json(run_dir: Path, name: str) -> Any:
    return json.loads((run_dir / name).read_text(encoding="utf-8"))


def write_markdown(run_dir: Path, name: str, content: str) -> Path:
    path = run_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def read_markdown(run_dir: Path, name: str) -> str:
    return (run_dir / name).read_text(encoding="utf-8")


def candidate_path(run_dir: Path, model_id: str) -> Path:
    """fan_out_judge 패턴: 후보 파일 경로."""
    return run_dir / "artifacts" / "candidates" / f"{model_id}.md"


def chain_step_path(run_dir: Path, index: int, role: str) -> Path:
    """hierarchical_delegation 패턴: 체인 스텝 파일 경로."""
    return run_dir / "artifacts" / "chain" / f"step-{index}-{role}.md"


def list_runs(root: Path | None = None) -> list[str]:
    root = root if root is not None else DEFAULT_WORKSPACE_ROOT
    if not root.exists():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def existing_run_dir(run_id: str, root: Path | None = None) -> Path:
    """이미 존재하는 run 디렉토리를 찾는다 (replay/resume 용도, 새로 만들지 않는다).

    create_run()과 달리 디렉토리를 생성하지 않는다 — 없는 run_id를 실수로 새로
    만들어버리면 replay/approve가 "성공"한 것처럼 보이는 조용한 오류로 이어지기 때문이다.
    """
    root = root if root is not None else DEFAULT_WORKSPACE_ROOT
    run_dir = root / run_id
    if not run_dir.is_dir():
        raise FileNotFoundError(f"run을 찾을 수 없다: {run_dir}")
    return run_dir
