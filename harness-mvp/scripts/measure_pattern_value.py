"""패턴 부가가치 측정: 단일 호출(direct) vs hierarchical_delegation 체인.

배경(2026-07-27): 구조 효율성 검토에서 "체인의 검토 스텝이 단일 호출 대비
품질을 실제로 올리는지 한 번도 측정한 적 없다"는 갭을 확인했다. 이 스크립트는
그 첫 측정이다 — 같은 프롬프트를 두 조건으로 k회씩 실행하고, 동일한
rubric 합격 판정(judge.check_pass, ADR 0006에서 추가)으로 품질을 비교한다.

설계 결정:
- 모델을 전 조건 gemini로 고정한다 — "체인 구조 자체"의 효과만 분리하기
  위해서다(역할별 모델 특화 효과는 별도 변수라 이 측정에서 통제). 부수 효과로
  구독 CLI 한도를 소모하지 않고 모든 비용이 $로 집계된다.
- 품질 판정은 두 조건 산출물에 같은 evaluator를 blind로 적용한다(evaluator는
  어느 조건의 출력인지 모른다). 판정이 이분법(pass/fail)이라 미세한 품질 차이는
  feedback 텍스트로만 관찰된다 — 한계로 명시.
- 실제 Gemini API를 호출하므로 의도적으로 `pytest tests/` 밖에 둔다(작업 규칙:
  자동 테스트는 실제 API/CLI 미호출. `verify_judge_fault_injection.py` 선례).

사용법 (harness-mvp 디렉토리에서, GEMINI_API_KEY 필요):
  PYTHONPATH=src python scripts/measure_pattern_value.py [--k 3]

결과는 콘솔 표 + `_workspace/measurements/pattern_value_<UTC시각>.json`.
"""
from __future__ import annotations

import argparse
import io
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from harness import judge, model_runner, orchestrator, run_store  # noqa: E402
from harness.schemas import ProviderConfig, TaskInput  # noqa: E402
from providers.api_provider import GeminiApiProvider  # noqa: E402

# Gemini free tier는 짧은 롤링 윈도우 요청 한도(실측 2026-07-27: limit 20,
# "retry in ~20s")가 있다 — 첫 측정 시도가 무간격 연속 호출로 429를 맞아 전멸했다.
# 호출 사이에 이 간격을 둬서 한도 안에 머문다. 참고: 엔진의 generate_with_retry
# (즉시 1회 재시도)는 속도 제한에는 무력하다(즉시 재시도는 반드시 다시 429) —
# 백오프 재시도는 별도 검토 대상.
PACE_SECONDS = 25

# domains/server-engineering-learning의 실제 task(task.networking-basics.json)와
# 동일한 프롬프트 — 생태적 타당성(실사용 프롬프트로 측정) 확보 목적.
PROMPT = (
    "초급 엔지니어가 이해할 수 있도록 서버 네트워킹 기초(방화벽, 포트, DNS)를 "
    "리서치해줘. 그 다음 학습 자료 초안을 만들고 내용을 검토해줘."
)
# planner._DEFAULT_RUBRICS["research"]와 동일 — 체인 조건에서 planner가 고르는
# rubric을 단일 조건에도 똑같이 적용해 판정 기준을 통일한다.
RUBRIC = ["출처 신뢰성", "핵심 정보 커버리지"]


def _gemini(provider_id: str) -> GeminiApiProvider:
    return GeminiApiProvider(
        ProviderConfig(provider_id=provider_id, model_id="gemini-2.5-flash", auth_mode="api_key")
    )


def run_direct(attempt: int) -> dict:
    """조건 A: 단일 gemini 호출 1회 (적합성 게이트 탈락 시의 direct_call과 동일 경로)."""
    started = time.time()
    candidate = model_runner.direct_call(PROMPT, _gemini("direct"))
    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "condition": "direct",
        "attempt": attempt,
        "ok": candidate.status == "success",
        "content": candidate.content,
        "latency_ms": candidate.latency_ms if candidate.latency_ms is not None else elapsed_ms,
        "cost_usd": candidate.cost_usd,
        "llm_calls": 1,
    }


def run_chain(attempt: int, root: Path) -> dict:
    """조건 B: orchestrator를 통해 research→design_review 체인 실행 (역할 전부 gemini)."""
    providers = {
        "research-mock": _gemini("research-mock"),
        "design_review-mock": _gemini("design_review-mock"),
        "implementation_review-mock": _gemini("implementation_review-mock"),
        orchestrator.JUDGE_PROVIDER_KEY: _gemini("judge"),
    }
    task = TaskInput(task_id=f"measure-chain-{attempt}", prompt=PROMPT)
    observation = orchestrator.run(task, providers, root=root)
    run_dir = root / f"run-measure-chain-{attempt}"
    ok = observation.status in ("success", "warning") and (run_dir / "final.md").exists()
    content = run_store.read_markdown(run_dir, "final.md") if ok else ""
    metrics = run_store.read_json(run_dir, "metrics.json")
    return {
        "condition": "chain",
        "attempt": attempt,
        "ok": ok,
        "content": content,
        "latency_ms": metrics["latency_ms"],
        "cost_usd": metrics["estimated_cost_usd"],
        "llm_calls": 2,
    }


def evaluate(result: dict) -> dict:
    """두 조건 공통 blind 판정 — evaluator는 조건 라벨을 모른다."""
    if not result["ok"]:
        result.update({"passed": False, "feedback": "(실행 실패 — 판정 생략)", "eval_cost_usd": None})
        return result
    verdict = judge.check_pass(result["content"], RUBRIC, _gemini("evaluator"))
    result.update(
        {"passed": verdict.passed, "feedback": verdict.feedback, "eval_cost_usd": verdict.cost_usd}
    )
    return result


def summarize(results: list[dict], condition: str) -> dict:
    rows = [r for r in results if r["condition"] == condition]
    passed = [r for r in rows if r["passed"]]
    costs = [r["cost_usd"] for r in rows if r["cost_usd"] is not None]
    latencies = [r["latency_ms"] for r in rows if r["latency_ms"] is not None]
    return {
        "condition": condition,
        "attempts": len(rows),
        "pass_rate": round(len(passed) / len(rows), 2) if rows else None,
        "avg_latency_ms": int(sum(latencies) / len(latencies)) if latencies else None,
        "avg_run_cost_usd": round(sum(costs) / len(costs), 6) if costs else None,
        "llm_calls_per_attempt": rows[0]["llm_calls"] if rows else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="단일 호출 vs 체인 품질/비용 측정")
    parser.add_argument("--k", type=int, default=3, help="조건당 반복 횟수 (기본 3)")
    args = parser.parse_args()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    measure_root = Path("_workspace/measurements")
    chain_runs_root = measure_root / f"chain_runs_{stamp}"
    chain_runs_root.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    first = True
    for i in range(1, args.k + 1):
        for label, runner in (("direct", lambda: run_direct(i)), ("chain", lambda: run_chain(i, chain_runs_root))):
            if not first:
                time.sleep(PACE_SECONDS)  # free tier 롤링 윈도우 한도 회피 (위 주석)
            first = False
            print(f"[{i}/{args.k}] {label} 실행 중...")
            result = runner()
            time.sleep(PACE_SECONDS)
            results.append(evaluate(result))

    summaries = [summarize(results, "direct"), summarize(results, "chain")]

    print("\n## 결과 요약")
    for s in summaries:
        print(
            f"- {s['condition']}: 합격률 {s['pass_rate']}, 평균 지연 {s['avg_latency_ms']}ms, "
            f"평균 run 비용 ${s['avg_run_cost_usd']}, 시도당 호출 {s['llm_calls_per_attempt']}회"
        )
    print("\n## 판정 상세 (조건/시도/합격 — 불합격 사유 앞부분)")
    for r in results:
        head = (r["feedback"] or "")[:120].replace("\n", " ")
        print(f"- {r['condition']} #{r['attempt']}: passed={r['passed']} {head}")

    out_path = measure_root / f"pattern_value_{stamp}.json"
    out_path.write_text(
        json.dumps({"prompt": PROMPT, "rubric": RUBRIC, "k": args.k, "summaries": summaries,
                    "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[ok] 전체 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
