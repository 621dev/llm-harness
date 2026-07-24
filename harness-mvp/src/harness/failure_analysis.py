"""실패 로그 집계: 여러 run의 errors.json/safety_review.json을 모아 반복되는 실패
유형을 요약한다 (Phase 5 "실패 로그 기반 프롬프트/스킬 개선").

이 모듈은 Planner/Judge/Safety 규칙을 자동으로 고치지 않는다 — 사람이 볼 수 있게
집계만 한다. 세 번째 팀 패턴(Debate/Consensus) 도입 여부를 보류한 ADR 0003도
"실제 Judge 오판 근거가 로그/eval 리포트에 쌓이면 재검토"를 트리거로 삼고 있으므로,
이 집계가 그 근거를 만드는 역할을 한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from . import run_store
from .schemas import FailureCategory, FailureReport

_SAFETY_FAILURE_PREFIX = "Safety 점검 실패: "


def analyze_failures(*, root: Optional[Path] = None) -> FailureReport:
    """워크스페이스의 모든 run을 스캔해 errors.json/safety_review.json을 집계한다."""
    root = root if root is not None else run_store.DEFAULT_WORKSPACE_ROOT
    run_ids = run_store.list_runs(root=root)

    error_hits: dict[str, list[str]] = {}
    safety_hits: dict[str, list[str]] = {}
    runs_with_errors = 0
    runs_with_safety_review = 0

    for run_id in run_ids:
        run_dir = root / run_id

        if (run_dir / "errors.json").exists():
            entries = run_store.read_json(run_dir, "errors.json")
            if entries:
                runs_with_errors += 1
            for entry in entries:
                key = entry.get("stage", "unknown")
                error_hits.setdefault(key, []).append(run_id)

        if (run_dir / "safety_review.json").exists():
            runs_with_safety_review += 1
            review = run_store.read_json(run_dir, "safety_review.json")
            for finding in _split_safety_findings(review.get("note")):
                safety_hits.setdefault(finding, []).append(run_id)

    return FailureReport(
        total_runs_scanned=len(run_ids),
        runs_with_errors=runs_with_errors,
        runs_with_safety_review=runs_with_safety_review,
        error_categories=_to_categories(error_hits),
        safety_categories=_to_categories(safety_hits),
    )


def _split_safety_findings(note: Optional[str]) -> list[str]:
    """safety.check()의 summary는 "Safety 점검 실패: A; B; C" 형태다 (safety.py 참고).

    개별 finding 단위로 쪼개야 "어떤 종류의 오탐이 반복되는가"를 구분할 수 있다.
    """
    if not note:
        return ["(사유 없음)"]
    body = note[len(_SAFETY_FAILURE_PREFIX):] if note.startswith(_SAFETY_FAILURE_PREFIX) else note
    findings = [item.strip() for item in body.split(";") if item.strip()]
    return findings or ["(사유 없음)"]


def _to_categories(hits: dict[str, list[str]]) -> list[FailureCategory]:
    categories = [
        FailureCategory(key=key, count=len(run_ids), example_run_ids=_dedupe_preserve_order(run_ids)[:3])
        for key, run_ids in hits.items()
    ]
    return sorted(categories, key=lambda c: c.count, reverse=True)


def _dedupe_preserve_order(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result
