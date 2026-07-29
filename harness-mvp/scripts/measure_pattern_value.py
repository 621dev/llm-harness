"""패턴 부가가치 측정: 단일 호출(direct) vs hierarchical_delegation 체인.

배경(2026-07-27): 구조 효율성 검토에서 "체인의 검토 스텝이 단일 호출 대비
품질을 실제로 올리는지 한 번도 측정한 적 없다"는 갭을 확인했다. 이 스크립트는
그 첫 측정이다 — 같은 프롬프트를 두 조건으로 k회씩 실행하고, 동일한
rubric 합격 판정(judge.check_pass, ADR 0006에서 추가)으로 품질을 비교한다.

설계 결정:
- **두 조건의 모델을 같게 고정한다** — "체인 구조 자체"의 효과만 분리하기
  위해서다(역할별 모델 특화 효과는 별도 변수라 이 측정에서 통제).
  `--generator`/`--evaluator`로 백엔드를 고를 수 있다:
  - `gemini`(기본): 종량제라 비용이 $로 집계된다. 다만 free tier는 일 20회
    한도라 k를 키우기 어렵고, 한도를 쓰면 다음 날까지 막힌다.
  - `claude`/`codex`: 구독이라 $ 비용은 안 잡히는 대신 **구독 한도를 소모**한다.
    금액 축이 사라지지만 `subscription_calls`(호출 횟수)로 비교는 된다.
    Gemini 한도에 막혔을 때 쓰는 우회로.
- 품질 판정은 두 조건 산출물에 같은 evaluator를 blind로 적용한다(evaluator는
  어느 조건의 출력인지 모른다). 판정이 이분법(pass/fail)이라 미세한 품질 차이는
  feedback 텍스트로만 관찰된다 — 한계로 명시.
- 실제 Gemini API를 호출하므로 의도적으로 `pytest tests/` 밖에 둔다(작업 규칙:
  자동 테스트는 실제 API/CLI 미호출. `verify_judge_fault_injection.py` 선례).

사용법 (harness-mvp 디렉토리에서):
  PYTHONPATH=src python scripts/measure_pattern_value.py [--k 3]
  PYTHONPATH=src python scripts/measure_pattern_value.py --generator claude --evaluator codex
gemini를 쓰면 GEMINI_API_KEY, claude/codex를 쓰면 해당 CLI 로그인이 필요하다.

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

from harness import cli as cli_module  # noqa: E402  (체인 역할 목록 재사용)
from harness import judge, model_runner, orchestrator, run_store  # noqa: E402
from harness.schemas import ProviderConfig, TaskInput  # noqa: E402
from providers.api_provider import GeminiApiProvider  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402
from providers.cli_subscription_provider import ClaudeCliProvider, CodexCliProvider  # noqa: E402
from providers.fallback_provider import QuotaFallbackProvider  # noqa: E402

# Gemini free tier는 짧은 롤링 윈도우 요청 한도(실측 2026-07-27: limit 20,
# "retry in ~20s")가 있다 — 첫 측정 시도가 무간격 연속 호출로 429를 맞아 전멸했다.
# 호출 사이에 이 간격을 둬서 한도 안에 머문다. 참고: 엔진의 generate_with_retry
# (즉시 1회 재시도)는 속도 제한에는 무력하다(즉시 재시도는 반드시 다시 429) —
# 백오프 재시도는 별도 검토 대상.
PACE_SECONDS = 25

# 구독 CLI(claude/codex)는 Gemini free tier 같은 짧은 윈도우 속도 제한이 없어서
# pacing이 불필요하다 — 그대로 25초씩 쉬면 측정만 몇 배 느려진다.
SUBSCRIPTION_PACE_SECONDS = 0

# domains/server-engineering-learning의 실제 task(task.networking-basics.json)와
# 동일한 프롬프트 — 생태적 타당성(실사용 프롬프트로 측정) 확보 목적.
PROMPT = (
    "초급 엔지니어가 이해할 수 있도록 서버 네트워킹 기초(방화벽, 포트, DNS)를 "
    "리서치해줘. 그 다음 학습 자료 초안을 만들고 내용을 검토해줘."
)
# planner._DEFAULT_RUBRICS["research"]와 동일 — 체인 조건에서 planner가 고르는
# rubric을 단일 조건에도 똑같이 적용해 판정 기준을 통일한다.
RUBRIC = ["출처 신뢰성", "핵심 정보 커버리지"]


# 쓸 수 있는 백엔드. gemini는 종량제(비용이 $로 보임), claude/codex는 구독
# (cost_usd가 None이라 금액 대신 subscription_calls 횟수로 보인다).
_BACKENDS: dict[str, tuple[str, str]] = {
    "gemini": ("gemini-2.5-flash", "api_key"),
    "claude": ("claude-cli", "cli_subscription"),
    "codex": ("codex-cli", "cli_subscription"),
}

# 실행 중에 정해진다(main에서 --generator/--evaluator로 설정).
_GENERATOR_BACKEND = "gemini"
_EVALUATOR_BACKEND = "gemini"


def _make(backend: str, provider_id: str) -> Provider:
    """백엔드 이름으로 provider 하나를 만든다(대체 없음)."""
    model_id, auth_mode = _BACKENDS[backend]
    config = ProviderConfig(provider_id=provider_id, model_id=model_id, auth_mode=auth_mode)
    if backend == "gemini":
        return GeminiApiProvider(config)
    if backend == "claude":
        return ClaudeCliProvider(config)
    return CodexCliProvider(config)


# 실행 중 대체가 일어났는지 확인하려고 만들어진 wrapper를 모아둔다.
_FALLBACK_WRAPPERS: list[QuotaFallbackProvider] = []


def _with_safety_net(backend: str, provider_id: str) -> Provider:
    """gemini로 확정된 경우에만 claude 안전망을 씌운다 — 측정 도중 한도가 소진돼도
    run 전체가 날아가지 않게.

    엔진의 `QuotaFallbackProvider`를 그대로 쓴다(2026-07-28 교체). 처음엔 이
    스크립트 안에 자체 wrapper를 만들었는데, 같은 날 엔진에 정식 버전이 들어오면서
    중복이 됐다 — 게다가 자체 wrapper는 **모든** ProviderError에 대체를 걸어서
    인증 실패 같은 진짜 버그도 조용히 넘겨버렸다. 정식 버전은 quota 오류만 골라
    대체하고 나머지는 전파하므로 이 측정에도 더 정확하다.

    대체가 일어났는지는 `used_fallback`으로 확인해 main이 측정을 중단한다 —
    조건마다 모델이 달라지면 비교가 성립하지 않기 때문이다.
    """
    primary = _make(backend, provider_id)
    if backend != "gemini":
        return primary
    wrapper = QuotaFallbackProvider(
        primary=primary,
        fallback=_make("claude", provider_id),
        config=ProviderConfig(provider_id=provider_id, model_id=primary.model_id),
    )
    _FALLBACK_WRAPPERS.append(wrapper)
    return wrapper


def _generator(provider_id: str) -> Provider:
    return _with_safety_net(_GENERATOR_BACKEND, provider_id)


def _evaluator(provider_id: str) -> Provider:
    return _with_safety_net(_EVALUATOR_BACKEND, provider_id)


def _probe_gemini() -> bool:
    """gemini가 지금 응답하는지 1회 호출로 확인한다(한도 소진 여부 판정)."""
    try:
        _make("gemini", "probe").generate("1+1은? 숫자만 답해", temperature=0.0)
        return True
    except ProviderError as exc:
        print(f"  gemini 응답 없음 → claude로 대체한다: {str(exc)[:110]}")
        return False


def _resolve_backend(requested: str) -> str:
    """`auto`면 gemini를 먼저 확인하고, 안 되면 claude로 정한다.

    호출마다 갈아타는 게 아니라 **측정 시작 전에 한 번 정해서 끝까지 고정**한다 —
    조건마다 모델이 달라지면 비교가 성립하지 않기 때문이다.
    """
    if requested != "auto":
        return requested
    return "gemini" if _probe_gemini() else "claude"


def _uses_subscription() -> bool:
    return any(_BACKENDS[b][1] == "cli_subscription" for b in (_GENERATOR_BACKEND, _EVALUATOR_BACKEND))


def _pace_seconds() -> int:
    """gemini(종량제 free tier)만 속도 제한 회피용 간격이 필요하다."""
    return SUBSCRIPTION_PACE_SECONDS if _uses_subscription() else PACE_SECONDS


def run_direct(attempt: int) -> dict:
    """조건 A: 단일 gemini 호출 1회 (적합성 게이트 탈락 시의 direct_call과 동일 경로)."""
    started = time.time()
    candidate = model_runner.direct_call(PROMPT, _generator("direct"))
    elapsed_ms = int((time.time() - started) * 1000)
    return {
        "condition": "direct",
        "attempt": attempt,
        "ok": candidate.status == "success",
        "content": candidate.content,
        "latency_ms": candidate.latency_ms if candidate.latency_ms is not None else elapsed_ms,
        "cost_usd": candidate.cost_usd,
        "subscription_calls": candidate.subscription_calls,
        "llm_calls": 1,
    }


# planner가 만드는 체인의 역할 provider를 전부 등록해야 한다 — 하나라도 빠지면
# subagent_runner가 `providers[step.provider_id]`에서 KeyError로 죽는다.
# content_finalization은 2026-07-28 merge된 PR #64로 research 체인에 3번째
# 역할로 추가됐다(그때 이 스크립트가 실제로 깨졌다). 엔진의 역할 목록을 직접
# 가져와서, 앞으로 역할이 늘어도 여기 손 안 대게 한다.
_CHAIN_ROLE_PROVIDER_IDS = tuple(f"{role}-mock" for role in cli_module._DELEGATION_ROLES)


def run_chain(attempt: int, root: Path) -> dict:
    """조건 B: orchestrator를 통해 hierarchical_delegation 체인 실행(역할 전부 같은 백엔드)."""
    providers: dict = {pid: _generator(pid) for pid in _CHAIN_ROLE_PROVIDER_IDS}
    providers[orchestrator.JUDGE_PROVIDER_KEY] = _evaluator("judge")
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
        "subscription_calls": metrics.get("subscription_calls", 0),
        # 체인 스텝 수 + evaluator 1회. 역할이 늘면 자동으로 반영된다.
        "llm_calls": len(run_store.read_json(run_dir, "plan.json")["delegation_chain"]) + 1,
    }


def evaluate(result: dict) -> dict:
    """두 조건 공통 blind 판정 — evaluator는 조건 라벨을 모른다."""
    if not result["ok"]:
        result.update({"passed": False, "feedback": "(실행 실패 — 판정 생략)", "eval_cost_usd": None})
        return result
    # PROMPT를 함께 넘긴다(2026-07-29) — 2차 측정의 direct #3 불합격이 "요청이 시켜서
    # 들어간 검토 섹션"을 evaluator가 결함으로 오판한 것이었다. blind 판정은 **조건
    # 라벨**(direct/chain)을 감추는 것이고, 원본 요청을 감추는 게 아니다.
    verdict = judge.check_pass(result["content"], RUBRIC, _evaluator("evaluator"), request=PROMPT)
    result.update(
        {
            "passed": verdict.passed,
            "feedback": verdict.feedback,
            # 판정이 rubric 항목에 묶였는지 결과에 남긴다 — 비어 있는 불합격은
            # 품질 신호가 아니라 판정 신뢰도 문제다.
            "unmet_rubric_items": verdict.unmet_rubric_items,
            "eval_cost_usd": verdict.cost_usd,
        }
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
        "total_subscription_calls": sum(r.get("subscription_calls", 0) for r in rows),
        "llm_calls_per_attempt": rows[0]["llm_calls"] if rows else None,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="단일 호출 vs 체인 품질/비용 측정")
    parser.add_argument("--k", type=int, default=3, help="조건당 반복 횟수 (기본 3)")
    parser.add_argument(
        "--generator", default="auto", choices=sorted(_BACKENDS) + ["auto"],
        help="후보/체인 스텝을 생성할 백엔드 (기본 auto: gemini 확인 후 안 되면 claude)",
    )
    parser.add_argument(
        "--evaluator", default="auto", choices=sorted(_BACKENDS) + ["auto"],
        help="합격 판정에 쓸 백엔드 (기본 auto). 생성 모델과 다르게 두면 self-preference 완화",
    )
    args = parser.parse_args()

    global _GENERATOR_BACKEND, _EVALUATOR_BACKEND
    # auto면 시작 전에 gemini를 한 번 확인해서 백엔드를 정한다 — 호출마다 갈아타면
    # 조건별 모델이 달라져 비교가 성립하지 않는다(_resolve_backend 참고).
    if "auto" in (args.generator, args.evaluator):
        print("백엔드 자동 선택: gemini 응답 확인 중...")
    resolved = _resolve_backend("auto") if "auto" in (args.generator, args.evaluator) else None
    _GENERATOR_BACKEND = resolved if args.generator == "auto" else args.generator
    _EVALUATOR_BACKEND = resolved if args.evaluator == "auto" else args.evaluator
    pace = _pace_seconds()
    print(f"generator={_GENERATOR_BACKEND} / evaluator={_EVALUATOR_BACKEND} / 호출 간격 {pace}초")
    if _uses_subscription():
        print("주의: 구독(claude/codex) 호출이라 $ 비용 대신 구독 한도를 소모한다.")
    print()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    measure_root = Path("_workspace/measurements")
    chain_runs_root = measure_root / f"chain_runs_{stamp}"
    chain_runs_root.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    first = True
    for i in range(1, args.k + 1):
        for label, runner in (("direct", lambda: run_direct(i)), ("chain", lambda: run_chain(i, chain_runs_root))):
            if not first:
                time.sleep(pace)  # free tier 롤링 윈도우 한도 회피 (위 주석)
            first = False
            print(f"[{i}/{args.k}] {label} 실행 중...")
            result = runner()
            time.sleep(pace)
            results.append(evaluate(result))

            # 측정 도중 백엔드가 바뀌면 조건마다 다른 모델로 돌아가 "구조 효과"와
            # "모델 효과"가 섞인다 — 그런 데이터는 비교에 쓸 수 없으므로 **더 돌리지
            # 않고 즉시 중단**한다. 경고만 붙여 저장하면 나중에 누군가 그 숫자를
            # 그대로 인용할 위험이 있다(2026-07-28 실제로 그런 run이 하나 나옴).
            if any(w.used_fallback for w in _FALLBACK_WRAPPERS):
                print(
                    f"\n[중단] gemini가 측정 도중 응답하지 못해 claude로 대체됐다"
                    f" (진행 {len(results)}/{args.k * 2}회).\n"
                    "        조건마다 모델이 달라져 비교가 성립하지 않으므로 결과를"
                    " 저장하지 않고 멈춘다.\n"
                    "        한 모델로 고정해 다시 돌릴 것:\n"
                    "          PYTHONPATH=src python scripts/measure_pattern_value.py"
                    " --generator claude --evaluator claude\n"
                    "        (또는 gemini 한도가 회복된 뒤 --generator gemini --evaluator gemini)"
                )
                raise SystemExit(1)

    summaries = [summarize(results, "direct"), summarize(results, "chain")]

    print("\n## 결과 요약")
    for s in summaries:
        print(
            f"- {s['condition']}: 합격률 {s['pass_rate']}, 평균 지연 {s['avg_latency_ms']}ms, "
            f"평균 run 비용 ${s['avg_run_cost_usd']}, 시도당 호출 {s['llm_calls_per_attempt']}회"
            + (f", 구독 호출 누적 {s['total_subscription_calls']}회" if _uses_subscription() else "")
        )
    print("\n## 판정 상세 (조건/시도/합격 — 불합격 사유 앞부분)")
    for r in results:
        head = (r["feedback"] or "")[:120].replace("\n", " ")
        print(f"- {r['condition']} #{r['attempt']}: passed={r['passed']} {head}")

    out_path = measure_root / f"pattern_value_{stamp}.json"
    out_path.write_text(
        json.dumps({"prompt": PROMPT, "rubric": RUBRIC, "k": args.k,
                    "generator": _GENERATOR_BACKEND, "evaluator": _EVALUATOR_BACKEND,
                    "requested_generator": args.generator, "requested_evaluator": args.evaluator,
                    "fallback_used": any(w.used_fallback for w in _FALLBACK_WRAPPERS),
                    "summaries": summaries, "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[ok] 전체 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
