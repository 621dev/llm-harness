"""적합성 게이트 탈락률 집계 (비용 0 — 디스크에 쌓인 fitness_check.json만 읽는다).

목적: 등급 라우팅(난이도로 패턴/모델을 가른다)의 이득 크기를 추정한다. 게이트가 이미
쉬운 작업을 잘 걸러내고 있으면 라우팅의 여지가 작고, 거의 통과시키고 있으면 여지가 크다.

**측정 run과 실사용 run을 분리한다** — 측정은 같은 프롬프트를 k회 반복하고 패턴을
constraints로 강제하므로, 섞으면 비율이 프롬프트 1종의 성질로 왜곡된다.
"""
import io
import json
import sys
from collections import Counter
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

# **저장소 루트로 고정한다**(cwd 기준이 아니다). 첫 구현은 `Path(".")`이라 실행 위치에
# 따라 결과가 달라졌다 — `harness-mvp/`에서 돌리면 `domains/`와 워크트리가 아예 안 보여서
# "실사용 6건"이 나오고, 루트에서 돌리면 다른 숫자가 나온다. 집계 도구가 실행 위치로
# 답이 바뀌면 그 숫자는 인용할 수 없다.
ROOT = Path(__file__).resolve().parents[2]
buckets: dict[str, list[tuple[Path, dict]]] = {
    "실사용": [], "실사용(워크트리)": [], "측정": [], "mock 검증": []
}

for path in ROOT.rglob("fitness_check.json"):
    parts = path.parts
    in_worktree = ".claude" in parts and "worktrees" in parts
    # **워크트리를 빼지 않는다**(2026-08-03 정정). 첫 구현은 "워크트리는 본 저장소 run의
    # 복사본이라 중복"이라고 보고 제외했는데 **틀렸다** — run 산출물은 `_workspace/`
    # (gitignore)에 있어서 워크트리 간에 공유되지 않고, **도메인 작업은 애초에 워크트리에서
    # 한다.** 그래서 `server-engineering-learning`의 실제 e2e run 6건을 통째로 놓치고
    # "도메인 run 0건"이라는 틀린 결론을 문서 네 곳에 적었다. 저장소가 이미 그 run을
    # 기록하고 있었는데(`planner.py`, 체크리스트) 집계를 더 믿은 것이 원인이다.
    if "_verify_mock" in parts:
        bucket = "mock 검증"
    elif "measurements" in parts:
        bucket = "측정"
    elif in_worktree:
        bucket = "실사용(워크트리)"
    else:
        bucket = "실사용"
    try:
        buckets[bucket].append((path, json.loads(path.read_text(encoding="utf-8"))))
    except (OSError, json.JSONDecodeError) as exc:
        print(f"[skip] {path}: {exc}")

for name, rows in buckets.items():
    if not rows:
        continue
    failed = [(p, d) for p, d in rows if not d.get("passed")]
    print(f"\n## {name} — run {len(rows)}건, 탈락 {len(failed)}건 "
          f"({len(failed) / len(rows) * 100:.0f}%)")
    reasons = Counter(d.get("reason", "(없음)") for _, d in rows)
    for reason, count in reasons.most_common():
        print(f"  {count:3}건  {reason[:100]}")
    for p, d in failed:
        # 탈락한 run은 무슨 task였는지 함께 본다 — 비율만으로는 "어떤 종류가 걸리는가"를
        # 알 수 없고, 라우팅 규칙을 만들려면 그게 필요하다.
        run_dir = p.parent
        task_id = run_dir.name
        plan = run_dir / "plan.json"
        task_type = ""
        if plan.exists():
            try:
                task_type = json.loads(plan.read_text(encoding="utf-8")).get("task_type", "")
            except (OSError, json.JSONDecodeError):
                pass
        print(f"    [탈락] {task_id} (task_type={task_type or '?'})")
