"""새 도메인 worktree에 sparse-checkout을 자동 적용하는 스크립트 (2026-07-24,
"워크트리 관리 자동화" 사용자 요청).

Claude Code 앱이 새 worktree를 만들면(예: `.claude/worktrees/<설명>-<해시>/`) 그
안에서 이 스크립트를 실행한다 — harness-mvp/docs는 공통으로, domains/<도메인 이름>
하나만 실제로 보이도록 sparse-checkout(cone 모드)을 설정해 다른 도메인을 실수로
건드리는 걸 막는다(docs/00_작업규칙/harness-project-conventions-ko.md에 있던 수동
절차 — `git sparse-checkout init --cone` + `set` — 를 스크립트화해서 사람이 매번
기억해서 칠 필요가 없게 함).

**메인 체크아웃에는 절대 적용하면 안 됨** — 메인은 전체 저장소를 봐야 하므로, 지금
실행 위치가 `git worktree list`의 첫 항목(메인)과 같으면 거부한다.

사용법 (새로 만든 worktree 디렉터리 안에서):
  python <저장소>/harness-mvp/scripts/setup_worktree.py <도메인 이름>
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.cli import _parse_worktree_porcelain_output  # noqa: E402


class NotAWorktreeError(Exception):
    """현재 디렉터리가 메인 체크아웃이라 sparse-checkout 대상이 아닐 때."""


class DomainNotFoundError(Exception):
    """domains/<name> 폴더가 없을 때 — 도메인 이름 오타 방지."""


def sparse_checkout_paths(domain_name: str) -> list[str]:
    return ["harness-mvp", "docs", f"domains/{domain_name}"]


def discover_main_worktree_path(cwd: Path) -> Path:
    """`git worktree list --porcelain`의 첫 항목(항상 메인 체크아웃)을 반환한다."""
    result = subprocess.run(
        ["git", "worktree", "list", "--porcelain"],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    worktrees = _parse_worktree_porcelain_output(result.stdout)
    return worktrees[0]


def apply_sparse_checkout(domain_name: str, *, cwd: Path) -> None:
    """cwd(지금 실행 중인 worktree)에 sparse-checkout(cone 모드)을 적용한다."""
    domain_dir = cwd / "domains" / domain_name
    # sparse-checkout 적용 전이라 domains/ 전체가 아직 그대로 보이는 상태 -> 여기서
    # 존재 여부를 확인할 수 있다(적용 후에는 이 검사 자체가 무의미해짐).
    if not domain_dir.is_dir():
        raise DomainNotFoundError(f"domains/{domain_name} 폴더를 찾을 수 없습니다 — 도메인 이름 오타 확인")

    main_worktree = discover_main_worktree_path(cwd)
    if cwd.resolve() == main_worktree.resolve():
        raise NotAWorktreeError(
            "메인 체크아웃에는 sparse-checkout을 적용하지 않습니다 — 새로 만든 도메인 "
            "worktree 디렉터리 안에서 실행하세요."
        )

    subprocess.run(["git", "sparse-checkout", "init", "--cone"], cwd=cwd, check=True)
    subprocess.run(
        ["git", "sparse-checkout", "set", *sparse_checkout_paths(domain_name)],
        cwd=cwd,
        check=True,
    )


def main() -> int:
    # Windows 기본 콘솔 코드페이지(cp949 등)는 이 스크립트가 쓰는 em-dash(—) 같은
    # 문자를 인코딩하지 못해 UnicodeEncodeError로 죽는다(cli.py main()과 동일한
    # 문제 — 2026-07-24 실제로 재현/확인). UTF-8을 강제해서 플랫폼 무관하게 만든다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("domain_name", help="이 worktree가 담당할 domains/ 폴더 이름")
    args = parser.parse_args()

    try:
        apply_sparse_checkout(args.domain_name, cwd=Path.cwd())
    except (NotAWorktreeError, DomainNotFoundError) as exc:
        print(f"[fatal] {exc}")
        return 1

    print(f"[ok] sparse-checkout 적용 완료: {', '.join(sparse_checkout_paths(args.domain_name))}")
    print("(git status로 기존 커밋 안 된 변경사항이 그대로인지 한 번 확인하는 걸 권장)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
