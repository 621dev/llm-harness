"""정적 HTML 대시보드: 저장된 run 산출물만으로 team_pattern별 성공/경고/실패율과
평균 latency/cost를 집계해 자기완결형 HTML 리포트를 만든다 (Phase 6).

읽는 파일은 4개뿐이다 — `plan.json`(team_pattern), `metrics.json`(latency/cost),
`errors.json`, `safety_review.json`/`approval.json`(둘 다 run 상태 판정용). 어떤
run도 재실행하지 않는다. run 하나의 최종 상태(success/warning/error)는
`orchestrator.py`가 실제로 반환했던 `Observation.status`를 파일 존재 여부만으로
재구성한다 — 우선순위는 orchestrator._finalize/_await_approval/resume/
resolve_safety_review의 종료 지점과 정확히 대응한다:

1. final.md가 있으면: errors.json이 비어있으면 success, 아니면 warning
2. final.md가 없고 safety_review.json이 있으면: pending=warning(검토 대기),
   rejected=error(위험 확정, 계속 보류)
3. final.md가 없고 approval.json이 있으면: pending=warning(승인 대기),
   rejected=error(사용자 반려)
4. 그 외(예: fan_out_judge min_candidates 미달, 체인 첫 단계부터 실패)는 error

eval pass@k는 다루지 않는다 — `evals/runner.py`의 `EvalReport`는 디스크에 저장된 적이
없어(Phase 2), "저장된 run 산출물만 집계"라는 이 리포트의 범위 밖이다.
"""
from __future__ import annotations

import html as html_lib
from pathlib import Path
from typing import Optional

from . import run_store
from .schemas import DashboardReport, PatternStats

_DIRECT_CALL_PATTERN = "direct_call"
_STATUS_COLORS = {"success": "#2e7d32", "warning": "#ed6c02", "error": "#c62828"}


def build_dashboard(*, root: Optional[Path] = None) -> DashboardReport:
    root = root if root is not None else run_store.DEFAULT_WORKSPACE_ROOT
    run_ids = run_store.list_runs(root=root)

    buckets: dict[str, dict] = {}
    for run_id in run_ids:
        run_dir = root / run_id
        pattern = _read_team_pattern(run_dir)
        status = _derive_run_status(run_dir)
        latency_ms, cost_usd = _read_metrics(run_dir)

        bucket = buckets.setdefault(
            pattern, {"success": 0, "warning": 0, "error": 0, "latencies": [], "costs": []}
        )
        bucket[status] += 1
        if latency_ms is not None:
            bucket["latencies"].append(latency_ms)
        if cost_usd is not None:
            bucket["costs"].append(cost_usd)

    patterns = [
        PatternStats(
            team_pattern=pattern,
            total_runs=bucket["success"] + bucket["warning"] + bucket["error"],
            success_count=bucket["success"],
            warning_count=bucket["warning"],
            error_count=bucket["error"],
            avg_latency_ms=_avg(bucket["latencies"]),
            avg_cost_usd=_avg(bucket["costs"]),
        )
        for pattern, bucket in buckets.items()
    ]
    patterns.sort(key=lambda p: p.team_pattern)

    return DashboardReport(total_runs_scanned=len(run_ids), patterns=patterns)


def _read_team_pattern(run_dir: Path) -> str:
    plan = _safe_read_json(run_dir, "plan.json")
    if plan is None:
        return _DIRECT_CALL_PATTERN
    return plan.get("team_pattern", _DIRECT_CALL_PATTERN)


def _read_metrics(run_dir: Path) -> tuple[Optional[int], Optional[float]]:
    metrics = _safe_read_json(run_dir, "metrics.json")
    if metrics is None:
        return None, None
    return metrics.get("latency_ms"), metrics.get("estimated_cost_usd")


def _derive_run_status(run_dir: Path) -> str:
    if (run_dir / "final.md").exists():
        errors = _safe_read_json(run_dir, "errors.json") or []
        return "warning" if errors else "success"

    safety_review = _safe_read_json(run_dir, "safety_review.json")
    if safety_review is not None:
        if safety_review.get("status") == "pending":
            return "warning"
        if safety_review.get("status") == "rejected":
            return "error"

    approval = _safe_read_json(run_dir, "approval.json")
    if approval is not None:
        if approval.get("status") == "pending":
            return "warning"
        if approval.get("status") == "rejected":
            return "error"

    return "error"


def _safe_read_json(run_dir: Path, name: str) -> Optional[dict]:
    if not (run_dir / name).exists():
        return None
    return run_store.read_json(run_dir, name)


def _avg(values: list[float]) -> Optional[float]:
    return round(sum(values) / len(values), 3) if values else None


def render_html(report: DashboardReport) -> str:
    """DashboardReport를 외부 CSS/JS/CDN 없는 자기완결형 HTML로 렌더링한다."""
    rows = "\n".join(_render_pattern_row(p) for p in report.patterns) or (
        '<tr><td colspan="7" class="empty">집계된 run이 없다.</td></tr>'
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>multi-llm-harness 대시보드</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 2rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ color: #666; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: right; }}
  th, td:first-child {{ text-align: left; }}
  th {{ background: #f5f5f5; }}
  .bar {{ display: flex; height: 14px; width: 160px; border-radius: 3px; overflow: hidden; }}
  .bar span {{ display: block; height: 100%; }}
  .empty {{ text-align: center; color: #888; }}
</style>
</head>
<body>
<h1>multi-llm-harness 대시보드</h1>
<p class="meta">저장된 run 산출물만 집계(재실행 없음, eval pass@k 미포함) — 총 {report.total_runs_scanned}개 run 스캔</p>
<table>
  <thead>
    <tr>
      <th>team_pattern</th><th>총 run</th><th>성공/경고/실패</th><th>비율</th>
      <th>성공률</th><th>평균 latency(ms)</th><th>평균 cost($)</th>
    </tr>
  </thead>
  <tbody>
{rows}
  </tbody>
</table>
</body>
</html>
"""


def _render_pattern_row(p: PatternStats) -> str:
    total = p.total_runs or 1
    success_rate = round(p.success_count / total * 100, 1)
    bar_segments = "".join(
        f'<span style="width:{count / total * 100:.1f}%;background:{_STATUS_COLORS[status]}"></span>'
        for status, count in (("success", p.success_count), ("warning", p.warning_count), ("error", p.error_count))
        if count > 0
    )
    pattern_name = html_lib.escape(p.team_pattern)
    latency = f"{p.avg_latency_ms:.0f}" if p.avg_latency_ms is not None else "-"
    cost = f"{p.avg_cost_usd:.6f}" if p.avg_cost_usd is not None else "-"
    return (
        "    <tr>"
        f"<td>{pattern_name}</td>"
        f"<td>{p.total_runs}</td>"
        f"<td>{p.success_count} / {p.warning_count} / {p.error_count}</td>"
        f'<td><div class="bar">{bar_segments}</div></td>'
        f"<td>{success_rate}%</td>"
        f"<td>{latency}</td>"
        f"<td>{cost}</td>"
        "</tr>"
    )
