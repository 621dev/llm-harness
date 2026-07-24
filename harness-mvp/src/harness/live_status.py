"""진행 중인 run을 실시간으로 보여주는 CLI 상태 조회 (Phase 6 후속, 2026-07-16).

`dashboard.py`는 완전히 회고적(저장된 최종 산출물만 집계)이라 "지금 뭐가 돌고
있나"를 보여줄 수 없다는 게 사용자 요청(2026-07-10)이었다 — 방향만 논의하고
보류돼 있던 걸 여기서 구현한다.

**핵심 난제**: run_dir에 남은 파일만 보면 "지금 실제로 실행 중"과 "중간에
프로세스가 죽어서 멈춘 것"을 구분할 수 없다(둘 다 final.md/safety_review.json/
approval.json이 전부 없는 상태로 보임). `run_meta.json`에 프로세스 ID를 남겨두고
그 프로세스가 실제로 살아있는지 확인하는 방식으로 해결한다(같은 머신에서 실행
중인 경우에만 유효 — 이 하네스는 분산 실행을 하지 않는 로컬 CLI이므로 범위 밖
아님).

`orchestrator.run()`(최초 실행)과 `orchestrator.resume()`(승인 후 재개, 완전히
다른 프로세스에서 호출될 수 있음)이 각각 `write_run_meta()`를 호출해 "지금 이
run을 실제로 실행 중인 프로세스가 어느 것인지" 갱신한다.

`render_html()`은 dashboard.py와 같은 "자기완결형 정적 HTML, 서버 프로세스
없음" 원칙을 그대로 따른다 — 호출 시점의 스냅샷 하나를 만들 뿐 자동 새로고침은
없다(2026-07-16 사용자 확인: 지금은 정적 스냅샷만, 나중에 진짜 라이브 웹페이지가
필요해지면 이 함수를 그대로 로컬 HTTP 서버로 감싸서 주기적으로 재호출하는 방향으로
확장 가능 — describe_run()/list_live_status() 자체는 이미 순수 함수라 손댈 필요
없음).

**도메인별 workspace 지원(2026-07-20)**: cloud-ops는 `run_estimate.py`가 자기
CWD 기준으로 실행돼 `domains/cloud-ops/_workspace/runs/`에 run이 쌓이지만,
ncp-snapshot-drill/centos-eol-migration처럼 커스텀 스크립트 없이 harness-mvp
CLI를 그대로 쓰는 도메인은 harness-mvp의 공용 workspace를 그대로 쓴다 — CWD를
헷갈리면 엉뚱한 workspace를 보게 되는 실수 위험이 있어 `--root`를 명시적으로
지정할 수 있게 했다. `describe_run()`은 run_dir 경로 구조(`.../<도메인 폴더>/
_workspace/runs/<run_id>`)에서 도메인 이름을 자동으로 추출해 `domain` 필드에
채운다 — 여러 workspace를 한 번에 조회(`list_live_status_multi()`)해도 어느
도메인 소속인지 구분할 수 있다.
"""
from __future__ import annotations

import html as html_lib
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional, Sequence

from . import run_store

# describe_run()이 반환하는 status 값:
# - "running": run_meta.json의 pid가 아직 살아있음 (real-time 실행 중)
# - "interrupted": run_meta.json은 있는데 그 pid가 죽어있음 (중간에 프로세스 종료)
# - "awaiting_approval" / "awaiting_safety_review": 사람 결정 대기(정상적인 일시 정지)
# - "done_success" / "done_warning" / "done_blocked" / "done_rejected": 종료됨
# - "unknown": run_meta.json조차 없음(이 기능 도입 이전에 만들어진 run 등)

STATUS_LABELS = {
    "running": "실행 중",
    "interrupted": "중단됨(프로세스 종료)",
    "awaiting_approval": "승인 대기",
    "awaiting_safety_review": "Safety 검토 대기",
    "done_success": "완료(성공)",
    "done_warning": "완료(경고)",
    "done_blocked": "완료(위험 확정, 보류)",
    "done_rejected": "완료(반려)",
    "done_error": "완료(오류, 출력 없음)",
    "unknown": "알 수 없음(run_meta 없음)",
}

_STATUS_COLORS = {
    "running": "#1565c0",
    "interrupted": "#c62828",
    "awaiting_approval": "#ed6c02",
    "awaiting_safety_review": "#ed6c02",
    "done_success": "#2e7d32",
    "done_warning": "#ed6c02",
    "done_blocked": "#c62828",
    "done_rejected": "#c62828",
    "done_error": "#c62828",
    "unknown": "#757575",
}


def pid_is_alive(pid: int) -> bool:
    """같은 머신에서 pid를 가진 프로세스가 아직 살아있는지 확인한다 (OS 무관)."""
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 존재는 하지만 시그널 보낼 권한이 없는 경우 -> 살아있다고 간주
    return True


def write_run_meta(run_dir: Path, *, pid: Optional[int] = None) -> None:
    """이 run을 지금 실행 중인 프로세스의 pid/시작 시각을 기록한다.

    `run()`(최초 실행 시)과 `resume()`(사람 승인 후 재개 시, 다른 프로세스일 수
    있음) 양쪽에서 실제 실행 직전에 호출한다.
    """
    run_store.write_json(
        run_dir,
        "run_meta.json",
        {
            "pid": pid if pid is not None else os.getpid(),
            "started_at": datetime.now(timezone.utc).isoformat(),
        },
    )


def describe_run(run_dir: Path) -> dict[str, Any]:
    """run 하나의 실시간 상태를 판정한다."""
    plan = _safe_read_json(run_dir, "plan.json")
    team_pattern = plan.get("team_pattern", "direct_call") if plan else "direct_call"
    meta = _safe_read_json(run_dir, "run_meta.json")
    started_at = meta.get("started_at") if meta else None
    task_input = _safe_read_json(run_dir, "input.json") or {}
    # task_id가 "간단한 작업명" 역할 — run_id(run-<task_id>)보다 짧고 사람이 붙인
    # 이름 그대로라 목록에서 맨 앞에 놓고 훑어보기 좋다.
    task_id = task_input.get("task_id")
    prompt = task_input.get("prompt")

    if (run_dir / "final.md").exists():
        errors = _safe_read_json(run_dir, "errors.json") or []
        status = "done_warning" if errors else "done_success"
        return _result(run_dir, team_pattern, status, started_at, task_id, prompt)

    safety_review = _safe_read_json(run_dir, "safety_review.json")
    if safety_review is not None:
        if safety_review.get("status") == "pending":
            return _result(run_dir, team_pattern, "awaiting_safety_review", started_at, task_id, prompt)
        if safety_review.get("status") == "rejected":
            return _result(run_dir, team_pattern, "done_blocked", started_at, task_id, prompt)

    approval = _safe_read_json(run_dir, "approval.json")
    if approval is not None:
        if approval.get("status") == "pending":
            return _result(run_dir, team_pattern, "awaiting_approval", started_at, task_id, prompt)
        if approval.get("status") == "rejected":
            return _result(run_dir, team_pattern, "done_rejected", started_at, task_id, prompt)

    # errors.json은 정해진 종료 지점(_finalize_without_output/judge 실패 등)에서만
    # 통째로 한 번에 쓰인다 — 실행 중간에 죽으면 아예 존재하지 않는다. 그래서 final.md
    # 없이 errors.json만 있으면(예: fan_out_judge min_candidates 미달) 이건 크래시가
    # 아니라 "출력 없이 정상 종료"라는 뜻이다(orchestrator._finalize_without_output).
    if (run_dir / "errors.json").exists():
        return _result(run_dir, team_pattern, "done_error", started_at, task_id, prompt)

    if meta is not None:
        status = "running" if pid_is_alive(meta["pid"]) else "interrupted"
        return _result(run_dir, team_pattern, status, started_at, task_id, prompt)

    return _result(run_dir, team_pattern, "unknown", started_at, task_id, prompt)


def list_live_status(*, root: Optional[Path] = None) -> list[dict[str, Any]]:
    """워크스페이스 하나의 모든 run에 대해 `describe_run()`을 실행한다."""
    root = root if root is not None else run_store.DEFAULT_WORKSPACE_ROOT
    return [describe_run(root / run_id) for run_id in run_store.list_runs(root=root)]


def list_live_status_multi(roots: Sequence[Path]) -> list[dict[str, Any]]:
    """여러 workspace를 한 번에 조회해 하나의 목록으로 합친다(여러 도메인을 총체적으로
    확인하려는 용도, 2026-07-20 사용자 요청). 결과의 `domain` 필드로 어디 소속인지
    구분할 수 있다."""
    combined: list[dict[str, Any]] = []
    for root in roots:
        combined.extend(list_live_status(root=root))
    return combined


def describe_estimate_output(output_dir: Path) -> Optional[dict[str, Any]]:
    """cloud-ops처럼 LLM을 호출하지 않고 결정론적으로 파일(엑셀 등)만 생성하는
    도메인 산출물을 describe_run()과 같은 모양으로 맞춰서 반환한다(2026-07-20 사용자
    요청: "cloud-ops 비용 계산 작업들을 대시보드에서 확인할 수 있게"). run_meta.json/
    plan.json 같은 게 전혀 없으므로 파일 자체의 존재/수정 시각으로만 판단 — 파일이
    하나도 없으면(빈 폴더) None을 반환해 목록에서 제외한다."""
    files = sorted(p.name for p in output_dir.iterdir() if p.is_file())
    if not files:
        return None
    latest_mtime = max((output_dir / f).stat().st_mtime for f in files)
    generated_at = datetime.fromtimestamp(latest_mtime, tz=timezone.utc).isoformat()
    return {
        "run_id": output_dir.name,
        "task_id": output_dir.name,
        "domain": _domain_label(output_dir),
        # LLM 팀 패턴이 전혀 아니라는 걸 명확히 하려고 fan_out_judge/
        # hierarchical_delegation/direct_call과 겹치지 않는 값을 쓴다.
        "team_pattern": "direct_output",
        "status": "done_success",
        "started_at": generated_at,
        "prompt": f"{len(files)}개 파일 생성: {', '.join(files)}",
    }


def list_estimate_outputs(estimates_root: Path) -> list[dict[str, Any]]:
    """`<도메인>/_workspace/estimates/<task_id>/` 형태의 결정론적 산출물 폴더를 훑는다."""
    if not estimates_root.is_dir():
        return []
    results = []
    for task_dir in sorted(estimates_root.iterdir()):
        if not task_dir.is_dir():
            continue
        result = describe_estimate_output(task_dir)
        if result is not None:
            results.append(result)
    return results


def list_domain_activity(*, root: Optional[Path] = None) -> list[dict[str, Any]]:
    """workspace 하나의 LLM run(list_live_status)과, 그 바로 옆
    `_workspace/estimates/`(있다면)의 결정론적 산출물을 합쳐서 본다 — "이 도메인이
    지금까지 한 작업"을 하네스 run 여부와 무관하게 총체적으로 보여주는 진입점."""
    root = root if root is not None else run_store.DEFAULT_WORKSPACE_ROOT
    items = list_live_status(root=root)
    estimates_dir = Path(root).parent / "estimates"
    items.extend(list_estimate_outputs(estimates_dir))
    return items


def list_domain_activity_multi(roots: Sequence[Path]) -> list[dict[str, Any]]:
    """`list_domain_activity()`를 여러 workspace에 대해 한 번에 실행해 합친다."""
    combined: list[dict[str, Any]] = []
    for root in roots:
        combined.extend(list_domain_activity(root=root))
    return combined


def format_elapsed(started_at: Optional[str]) -> Optional[str]:
    """started_at(ISO) ~ 지금까지 경과 시간을 사람이 읽기 쉬운 문자열로 만든다."""
    if started_at is None:
        return None
    started = datetime.fromisoformat(started_at)
    seconds = int((datetime.now(timezone.utc) - started).total_seconds())
    if seconds < 60:
        return f"{seconds}초"
    if seconds < 3600:
        return f"{seconds // 60}분"
    return f"{seconds // 3600}시간"


DEFAULT_GUIDE_FILENAME = "guide.html"
DEFAULT_DASHBOARD_FILENAME = "index.html"


def render_html(statuses: list[dict[str, Any]], *, guide_href: str = DEFAULT_GUIDE_FILENAME) -> str:
    """`list_live_status()` 결과를 외부 CSS/JS/CDN 없는 자기완결형 HTML 스냅샷으로
    렌더링한다. 자동 새로고침 없음 — 호출할 때마다 새 스냅샷을 만들어야 한다.

    "이 표를 보는 법" 가이드는 이 페이지 안이 아니라 `render_guide_html()`이 만드는
    별도 페이지에 있다(2026-07-23 사용자 요청: "가이드를 하단에 두지 말고 따로
    페이지로 빼줘" — 표가 길어질수록 가이드를 보려고 매번 스크롤해야 하는 게
    불편했음). `guide_href`는 그 페이지로 가는 상대 경로."""
    rows = "\n".join(_render_status_row(s) for s in statuses) or (
        '<tr><td colspan="7" class="empty">run이 없다.</td></tr>'
    )
    generated_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>multi-llm-harness 진행상황</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 2rem; color: #1a1a1a; }}
  h1 {{ font-size: 1.4rem; }}
  .meta {{ color: #666; margin-bottom: 1.5rem; }}
  table {{ border-collapse: collapse; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 0.5rem 0.75rem; text-align: left; }}
  th {{ background: #f5f5f5; }}
  .empty {{ text-align: center; color: #888; }}
  .status {{ display: inline-block; padding: 0.15rem 0.6rem; border-radius: 3px; color: #fff; font-size: 0.85rem; }}
  td:last-child {{ max-width: 22rem; }}
  details > summary {{ cursor: pointer; color: #1565c0; list-style: none; }}
  details > summary::-webkit-details-marker {{ display: none; }}
  details > summary::before {{ content: "▸ "; }}
  details[open] > summary::before {{ content: "▾ "; }}
  details[open] > summary {{ color: #666; margin-bottom: 0.4rem; }}
  details .full-text {{ white-space: pre-wrap; word-break: break-word; }}
  .guide-link {{ display: inline-block; margin-bottom: 1rem; }}
  .filter-bar {{ display: flex; gap: 1.25rem; flex-wrap: wrap; align-items: center; margin-bottom: 1rem; }}
  .filter-bar label {{ font-size: 0.9rem; color: #444; }}
  .filter-bar select {{ margin-left: 0.3rem; padding: 0.2rem 0.4rem; }}
  #runs-table tbody tr.filtered-out {{ display: none; }}
</style>
</head>
<body>
<h1>multi-llm-harness 진행상황</h1>
<p class="meta">자동 새로고침 없음 — 생성 시점({generated_at} UTC)의 스냅샷. 최신
상태를 보려면 <code>harness.cli status --output ...</code>을 다시 실행할 것.</p>
<p class="guide-link"><a href="{guide_href}">📖 이 표를 보는 법 (가이드)</a></p>
{_render_filter_bar(statuses)}
<table id="runs-table">
  <thead><tr><th>도메인</th><th>작업명</th><th>run_id</th><th>team_pattern</th><th>상태</th><th>경과</th><th>요청 내용</th></tr></thead>
  <tbody>
{rows}
  </tbody>
</table>

<script>
function applyDashboardFilters() {{
  var domain = document.getElementById("filter-domain").value;
  var pattern = document.getElementById("filter-pattern").value;
  var status = document.getElementById("filter-status").value;
  var rows = document.querySelectorAll("#runs-table tbody tr");
  rows.forEach(function (row) {{
    var matches = (!domain || row.dataset.domain === domain)
      && (!pattern || row.dataset.pattern === pattern)
      && (!status || row.dataset.status === status);
    row.classList.toggle("filtered-out", !matches);
  }});
}}
["filter-domain", "filter-pattern", "filter-status"].forEach(function (id) {{
  var el = document.getElementById(id);
  if (el) {{ el.addEventListener("change", applyDashboardFilters); }}
}});
</script>
</body>
</html>
"""


_STATUS_DESCRIPTIONS = {
    "running": "지금 진짜로 돌고 있음(pid 생존 확인됨).",
    "interrupted": "실행 도중 프로세스가 죽어서 멈춤.",
    "awaiting_approval": "risk_level이 높아 사람 승인을 기다리는 중.",
    "awaiting_safety_review": "안전성 검사에 걸려 사람 검토를 기다리는 중.",
    "done_success": "정상적으로 끝까지 완료됨.",
    "done_warning": "완료는 됐지만 일부 후보/단계가 실패했음(경고성 이슈 있음).",
    "done_error": "결과물 없이 실패로 끝남(예: 성공한 후보 수가 최소 기준 미달).",
    "done_blocked": "사람이 검토 후 위험하다고 확정해 계속 보류.",
    "done_rejected": "사람이 승인 요청을 반려함.",
    "unknown": "run_meta.json이 없음(이 기능 도입 이전에 만들어진 run 등).",
}


def render_guide_html(*, back_href: str = DEFAULT_DASHBOARD_FILENAME) -> str:
    """"이 표를 보는 법" 가이드를 대시보드 본문과 분리된 별도 페이지로 만든다
    (2026-07-23 사용자 요청: "가이드를 하단에 두지 말고 따로 페이지로 빼줘" — 이전엔
    `#guide`/`#top` 앵커 링크로 같은 페이지 안에서 오갔지만, 표가 길어질수록 가이드를
    보려고 매번 스크롤해야 하는 게 불편했음). `back_href`는 대시보드 페이지로 돌아가는
    상대 경로(호출한 쪽이 실제 대시보드 파일명을 알고 있으므로 그대로 넘겨준다)."""
    status_rows = "\n".join(
        f"    <tr><td>{html_lib.escape(STATUS_LABELS[key])}</td><td>{html_lib.escape(desc)}</td></tr>"
        for key, desc in _STATUS_DESCRIPTIONS.items()
    )
    return f"""<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<title>multi-llm-harness 대시보드 — 이 표를 보는 법</title>
<style>
  body {{ font-family: -apple-system, "Segoe UI", sans-serif; margin: 2rem; color: #1a1a1a; max-width: 50rem; }}
  h1 {{ font-size: 1.3rem; }}
  h2 {{ font-size: 1.05rem; margin-top: 1.5rem; }}
  table {{ border-collapse: collapse; margin: 0.5rem 0 1rem; width: 100%; }}
  th, td {{ border: 1px solid #ddd; padding: 0.4rem 0.7rem; text-align: left; }}
  th {{ background: #f5f5f5; }}
  .back-link {{ display: inline-block; margin-bottom: 1rem; }}
</style>
</head>
<body>
<p class="back-link"><a href="{back_href}">← 대시보드로 돌아가기</a></p>
<h1>📖 이 표를 보는 법</h1>

<h2>항목(열)</h2>
<table>
  <tr><th>도메인</th><td>어느 도메인 소속인지(harness-mvp, cloud-ops 등) — run 저장 경로에서 자동 추출</td></tr>
  <tr><th>작업명</th><td>task_id — input.json에 저장된 짧은 이름(예: fan-out-demo)</td></tr>
  <tr><th>run_id</th><td>실행 하나하나를 구분하는 전체 이름(예: run-fan-out-demo)</td></tr>
  <tr><th>team_pattern</th><td>fan_out_judge(여러 모델 비교) / hierarchical_delegation(역할별 순차 위임) / direct_call(단순 1회 호출) / direct_output(LLM 없이 결정론적으로 파일만 생성)</td></tr>
  <tr><th>상태</th><td>아래 "상태 값" 표 참고</td></tr>
  <tr><th>경과</th><td>시작 시각부터 지금까지 흐른 시간(이 기능 이전 run은 "-")</td></tr>
  <tr><th>요청 내용</th><td>원래 요청(prompt) 또는 "N개 파일 생성: ..." — 20자 넘으면 접혀있다가 클릭하면 펼쳐짐</td></tr>
</table>

<h2>상태 값</h2>
<table>
  <tr><th>상태</th><th>의미</th></tr>
{status_rows}
</table>

<h2>조회 대상이 되는 두 종류의 작업</h2>
<p>
1. <b>실제 LLM run</b>(harness.cli run으로 실행된 것)<br>
2. <b>결정론적 산출물</b>(cloud-ops의 엑셀 견적처럼 LLM 호출 없이 코드로 직접 계산한 것, team_pattern=direct_output으로 표시)
</p>

<h2>조회 범위 옵션</h2>
<p>
기본은 현재 workspace 하나만. <code>--root &lt;경로&gt;</code>(반복 지정)로 여러
workspace를 지정하거나, <code>--all-domains</code>로 메인+모든 git worktree의
모든 도메인을 자동으로 찾아 합칠 수 있다.
</p>

<h2>포함 안 되는 것</h2>
<p>
코드/문서 편집 같은 일반 작업 내역, git 커밋 이력, PR 상태 등은 이 대시보드에
안 나온다 — 순수하게 "하네스가 실행한 작업"만 대상이다.
</p>
</body>
</html>
"""


_PROMPT_SUMMARY_LENGTH = 20  # 접었을 때 보여줄 "간략한 주제" 길이(2026-07-23 사용자 요청)


def _render_prompt_cell(prompt: Optional[str]) -> str:
    """요청 내용을 표에 바로 풀어 쓰지 않고, 짧은 주제만 보여주다가 클릭(<summary>)하면
    전체 내용이 펼쳐지게 한다 — 서버 없는 정적 HTML 원칙을 유지하면서(JS 불필요)
    `<details>`/`<summary>`만으로 구현."""
    if not prompt:
        return "-"
    escaped = html_lib.escape(prompt)
    if len(prompt) <= _PROMPT_SUMMARY_LENGTH:
        return escaped
    summary = html_lib.escape(prompt[:_PROMPT_SUMMARY_LENGTH]) + "..."
    return f"<details><summary>{summary}</summary><div class=\"full-text\">{escaped}</div></details>"


def _render_status_row(item: dict[str, Any]) -> str:
    label = STATUS_LABELS.get(item["status"], item["status"])
    color = _STATUS_COLORS.get(item["status"], "#757575")
    elapsed = format_elapsed(item["started_at"]) or "-"
    domain_raw = item.get("domain") or ""
    domain = html_lib.escape(domain_raw) if domain_raw else "-"
    task_id = html_lib.escape(item["task_id"]) if item.get("task_id") else "-"
    run_id = html_lib.escape(item["run_id"])
    team_pattern = html_lib.escape(item["team_pattern"])
    prompt_cell = _render_prompt_cell(item.get("prompt"))
    # data-* 속성은 필터 드롭다운(_render_filter_bar)이 클라이언트 사이드에서
    # 행을 보이거나 숨기는 데 쓴다(2026-07-23 사용자 요청: "항목별로 필터").
    return (
        f'    <tr data-domain="{html_lib.escape(domain_raw)}" '
        f'data-pattern="{html_lib.escape(item["team_pattern"])}" '
        f'data-status="{html_lib.escape(item["status"])}">'
        f"<td>{domain}</td>"
        f"<td>{task_id}</td>"
        f"<td>{run_id}</td>"
        f"<td>{team_pattern}</td>"
        f'<td><span class="status" style="background:{color}">{html_lib.escape(label)}</span></td>'
        f"<td>{elapsed}</td>"
        f"<td>{prompt_cell}</td>"
        "</tr>"
    )


def _render_filter_bar(statuses: list[dict[str, Any]]) -> str:
    """도메인/team_pattern/상태 세 항목을 드롭다운으로 필터링하는 바. 실제로
    데이터에 존재하는 값만 선택지로 보여준다(빈 목록이면 "전체"만 남아 사실상
    필터가 없는 것과 같음). 자바스크립트로 행의 표시 여부만 토글하므로
    서버 없는 정적 HTML 원칙은 그대로 유지."""
    domains = sorted({s["domain"] for s in statuses if s.get("domain")})
    patterns = sorted({s["team_pattern"] for s in statuses if s.get("team_pattern")})
    status_keys = sorted({s["status"] for s in statuses if s.get("status")})

    def _options(values: list[str], *, labels: Optional[dict[str, str]] = None) -> str:
        opts = ['<option value="">전체</option>']
        for value in values:
            text = html_lib.escape(labels.get(value, value)) if labels else html_lib.escape(value)
            opts.append(f'<option value="{html_lib.escape(value)}">{text}</option>')
        return "".join(opts)

    return f"""<div class="filter-bar">
  <label>도메인:
    <select id="filter-domain">{_options(domains)}</select>
  </label>
  <label>team_pattern:
    <select id="filter-pattern">{_options(patterns)}</select>
  </label>
  <label>상태:
    <select id="filter-status">{_options(status_keys, labels=STATUS_LABELS)}</select>
  </label>
</div>"""


def _result(
    run_dir: Path,
    team_pattern: str,
    status: str,
    started_at: Optional[str],
    task_id: Optional[str] = None,
    prompt: Optional[str] = None,
) -> dict[str, Any]:
    return {
        "run_id": run_dir.name,
        "task_id": task_id,
        "domain": _domain_label(run_dir),
        "team_pattern": team_pattern,
        "status": status,
        "started_at": started_at,
        "prompt": prompt,
    }


def _domain_label(run_dir: Path) -> str:
    """run_dir 경로 구조(`.../<도메인 폴더>/_workspace/runs/<run_id>`)에서 도메인
    이름을 뽑아낸다 — 여러 workspace를 한 번에 조회할 때 어디 소속인지 구분하기 위함.
    """
    resolved = run_dir.resolve()
    try:
        return resolved.parents[2].name
    except IndexError:
        return "unknown"


def _safe_read_json(run_dir: Path, name: str) -> Optional[dict]:
    if not (run_dir / name).exists():
        return None
    return run_store.read_json(run_dir, name)
