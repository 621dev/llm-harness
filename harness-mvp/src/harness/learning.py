"""run 간 학습 (2026-07-29 도입).

**해결하는 갭**: 하네스가 run 사이에 아무것도 배우지 않았다. 매 run이 백지에서
시작하고, 축적은 전부 사람이 읽는 문서(`docs/03_진행상황/`)에만 쌓였다 — 엔진은
"지난번에 gemini가 이겼다", "codex가 세 번 연속 타임아웃했다", "이 도메인 run은
보통 $0.01쯤 든다"를 모른다. `affaan-m/ECC` 재분석에서 우리 최대 갭으로 확인한 부분.

**2단 구조 (사용자 결정: "기록은 자동, 반영은 명시적")**

    run 종료 ──자동──> <root>/learned/observations.jsonl   (기계가 append, gitignored)
                            │
                     `cli learn`으로 집계해서 사람이 읽음
                            │
                       사람이 판단해서 씀
                            ▼
                        ./learned.md                       (도메인에 커밋, 공유됨)
                            │
                      존재하면 다음 run에 주입
                            ▼
                  run_dir/learned_context.md               (무엇이 주입됐는지 기록)

**왜 자동 반영을 안 하는가**: 잘못된 학습이 누적되면 판정을 조용히 오염시킨다.
"실패를 조용히 감추지 않는다"와 같은 결이다 — 5 run 중 4번 이겼다는 사실이
"그 모델을 쓰라"는 결론과 같지 않다(태스크 성격이 달랐을 수 있다). 그 해석은 사람이
한다. **`learned.md`를 사람이 쓰는 행위 자체가 승인**이고, 파일이 없으면 아무것도
주입되지 않는다.

**왜 주입 내용을 run에 복사하는가**: 재현성 때문이다. `learned.md`는 시간이 지나며
바뀌므로, 기록이 없으면 "그때 무엇을 학습한 상태였나"를 알 수 없어 run을 다시
해석할 수 없게 된다.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from . import run_store

LEARNED_DIRNAME = "learned"
OBSERVATIONS_FILENAME = "observations.jsonl"
# 사람이 쓰는 파일. 도메인 디렉터리(= CLI 실행 cwd, ADR 0005의 상대경로 해석)에 둔다.
LEARNED_NOTES_FILENAME = "learned.md"
# 주입된 내용을 run 안에 복사해두는 이름.
INJECTED_FILENAME = "learned_context.md"


def record_run(run_dir: Path) -> dict[str, Any]:
    """끝난 run에서 학습 후보를 뽑아 `<root>/learned/observations.jsonl`에 한 줄 추가한다.

    **run 산출물만 읽는다** — 실행 중 상태를 따로 들고 있지 않아서, 이 함수는 이미
    파일로 확정된 사실만 본다(파일 기반 하네스라 가능한 방식이고, 나중에 지난 run을
    소급해서 다시 집계할 수도 있다).

    루트는 `run_dir.parent`에서 유도한다 — run이 `<root>/run-<id>/`이므로 항상 맞고,
    호출부가 root를 따로 넘기지 않아 "run은 A에 학습은 B에" 어긋날 여지가 없다.
    """
    record: dict[str, Any] = {
        "run_id": run_dir.name,
        "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    record.update(_extract_plan(run_dir))
    record.update(_extract_judging(run_dir))
    record.update(_extract_metrics(run_dir))
    record.update(_extract_errors(run_dir))

    target = _observations_path(run_dir.parent)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_observations(*, root: Optional[Path] = None) -> list[dict[str, Any]]:
    """쌓인 관측을 전부 읽는다. 깨진 줄은 건너뛴다 — 한 줄이 잘못됐다고 전체를 못 읽으면
    append-only 로그의 장점이 없어진다."""
    path = _observations_path(root)
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def summarize(*, root: Optional[Path] = None) -> dict[str, Any]:
    """사람이 판단할 재료로 집계한다. **결론을 내리지 않는다** — 숫자만 준다.

    "gemini가 4/5 승"은 사실이고 "gemini를 쓰라"는 해석이다. 후자는 태스크 성격이
    달랐는지, 표본이 충분한지를 봐야 하는 일이라 사람에게 남긴다.
    """
    records = read_observations(root=root)
    wins: dict[str, int] = {}
    failures: dict[str, int] = {}
    patterns: dict[str, int] = {}
    costs: list[float] = []
    calls: list[int] = []

    for record in records:
        if winner := record.get("winner"):
            wins[winner] = wins.get(winner, 0) + 1
        for provider in record.get("failed_providers", []):
            failures[provider] = failures.get(provider, 0) + 1
        if pattern := record.get("team_pattern"):
            patterns[pattern] = patterns.get(pattern, 0) + 1
        if (cost := record.get("cost_usd")) is not None:
            costs.append(cost)
        if (count := record.get("subscription_calls")) is not None:
            calls.append(count)

    return {
        "runs": len(records),
        "wins": dict(sorted(wins.items(), key=lambda kv: -kv[1])),
        "failures": dict(sorted(failures.items(), key=lambda kv: -kv[1])),
        "patterns": dict(sorted(patterns.items(), key=lambda kv: -kv[1])),
        # 예산 상한(budget_usd)을 얼마로 둘지 정할 근거 — 도입 당시 사용자가 참고할
        # 실측치가 없었다.
        "cost_usd": _range(costs),
        "subscription_calls": _range([float(c) for c in calls]),
    }


def load_notes(*, cwd: Optional[Path] = None) -> Optional[str]:
    """사람이 쓴 `learned.md`를 읽는다. 없으면 None(= 주입하지 않음)."""
    path = (cwd or Path.cwd()) / LEARNED_NOTES_FILENAME
    if not path.is_file():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def apply_to_prompt(prompt: str, notes: str) -> str:
    """학습 메모를 프롬프트 앞에 참고 자료로 붙인다.

    뒤가 아니라 **앞**에 붙이는 이유: 사용자 요청이 마지막에 오는 게 지시로 읽히고,
    참고 자료가 요청을 밀어내지 않는다. 체인의 `_CONTINUATION_INSTRUCTION_TEMPLATE`가
    이전 결과를 앞에 두는 것과 같은 형태다.
    """
    return f"[이 도메인에서 지금까지 확인된 것 — 참고 자료]\n{notes}\n\n[요청]\n{prompt}"


def _observations_path(root: Optional[Path]) -> Path:
    # root 기본값 해석은 run_store가 소유한다(호출 시점에 참조 — 도메인별 workspace
    # 전환이 그렇게 동작한다). 여기서 따로 계산하면 run은 A에, 학습은 B에 쌓이는
    # 어긋남이 생긴다.
    resolved = root if root is not None else run_store.DEFAULT_WORKSPACE_ROOT
    return resolved / LEARNED_DIRNAME / OBSERVATIONS_FILENAME


def _extract_plan(run_dir: Path) -> dict[str, Any]:
    plan = _safe_json(run_dir, "plan.json")
    if not isinstance(plan, dict):
        return {}
    return {
        "task_id": plan.get("task_id"),
        "task_type": plan.get("task_type"),
        "team_pattern": plan.get("team_pattern"),
    }


def _extract_judging(run_dir: Path) -> dict[str, Any]:
    judging = _safe_json(run_dir, "judging.json")
    if not isinstance(judging, dict):
        return {}
    scores = judging.get("scores") or []
    return {
        "winner": judging.get("winner"),
        "scores": {s.get("candidate"): s.get("score") for s in scores if isinstance(s, dict)},
    }


def _extract_metrics(run_dir: Path) -> dict[str, Any]:
    metrics = _safe_json(run_dir, "metrics.json")
    if not isinstance(metrics, dict):
        return {}
    return {
        "cost_usd": metrics.get("estimated_cost_usd"),
        "subscription_calls": metrics.get("subscription_calls"),
        "latency_ms": metrics.get("latency_ms"),
    }


def _extract_errors(run_dir: Path) -> dict[str, Any]:
    errors = _safe_json(run_dir, "errors.json")
    if not isinstance(errors, list):
        return {}
    failed = []
    budget_stopped = False
    for entry in errors:
        if not isinstance(entry, dict):
            continue
        # **`kind`(구조적 필드)로 분류한다.** 메시지 문구를 매칭하면 문구를 다듬는
        # 순간 조용히 오분류되고, 그건 이 프로젝트에서 세 번 데인 방식이다(v21 §6).
        # 예산 중단을 provider 실패로 세면 "codex가 자주 실패한다"는 잘못된 학습이
        # 생긴다 — 예산 때문에 호출조차 안 된 것까지 실패로 잡히기 때문이다.
        kind = entry.get("kind")
        if kind == "budget":
            budget_stopped = True
        elif kind == "candidate_failure" and entry.get("provider"):
            failed.append(entry["provider"])
    return {"failed_providers": failed, "budget_stopped": budget_stopped}


def _safe_json(run_dir: Path, name: str) -> Any:
    try:
        return run_store.read_json(run_dir, name)
    except (OSError, json.JSONDecodeError):
        return None


def _range(values: list[float]) -> Optional[dict[str, float]]:
    if not values:
        return None
    return {
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "mean": round(sum(values) / len(values), 6),
        "samples": len(values),
    }
