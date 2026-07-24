"""pass@k Runner (Phase 2).

harness-implementation-plan-ko.md Section 8(Phase 2)을 구현한다. 동일 `EvalCase`를
k번 반복 실행해서 pass_rate/pass_at_k/pass_pow_k와 cost/latency per success를
측정한다 (패턴 무관 — orchestrator.run()이 어떤 team_pattern을 고르든 이 러너는
final.md와 metrics.json만 본다).

지금 provider가 전부 결정적 MockProvider라, 매번 똑같은 provider를 쓰면 k번이
전부 동일한 결과로 나와 pass@k가 의미 없어진다. 그래서 `providers_factory`는
"몇 번째 시도인가(attempt_index)"를 받아 시도마다 다른 provider 구성(예: 실패 주입)을
만들 수 있게 했다 — 실제 LLM을 쓰게 되면 provider 자체의 비결정성이 이 역할을 대신한다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterable, Optional

from providers.base import Provider

from evals import graders
from harness import orchestrator, run_store
from harness.schemas import AttemptResult, EvalCase, EvalReport


def run_case_k_times(
    case: EvalCase,
    providers_factory: Callable[[int], dict[str, Provider]],
    k: int,
    root: Optional[Path] = None,
) -> EvalReport:
    """EvalCase를 k번 독립적으로 실행하고 채점해서 EvalReport를 만든다.

    시도마다 task_id를 다르게 줘서(run_store가 task_id로 run 디렉토리를 만들기 때문에)
    서로 다른 run 디렉토리에 저장되게 한다 — 시도 간 결과가 서로 덮어써지지 않는다.
    """
    if k < 1:
        raise ValueError(f"k는 1 이상이어야 한다: {k}")

    attempts: list[AttemptResult] = []
    for attempt_index in range(k):
        task = case.task.model_copy(update={"task_id": f"{case.task.task_id}-attempt-{attempt_index + 1}"})
        providers = providers_factory(attempt_index)

        observation = orchestrator.run(task, providers, root=root)
        run_dir = run_store.existing_run_dir(f"run-{task.task_id}", root=root)
        grade_result = graders.grade(run_dir, case, observation.status)
        metrics = _read_metrics(run_dir)

        attempts.append(
            AttemptResult(
                run_id=run_dir.name,
                run_status=observation.status,
                grade=grade_result,
                latency_ms=metrics.get("latency_ms"),
                cost_usd=metrics.get("estimated_cost_usd"),
            )
        )

    return _build_report(case.name, attempts)


def _read_metrics(run_dir: Path) -> dict:
    metrics_path = run_dir / "metrics.json"
    if not metrics_path.exists():
        return {}
    return run_store.read_json(run_dir, "metrics.json")


def _build_report(case_name: str, attempts: list[AttemptResult]) -> EvalReport:
    k = len(attempts)
    passed_attempts = [a for a in attempts if a.grade.passed]

    pass_rate = len(passed_attempts) / k
    pass_at_k = 1.0 if passed_attempts else 0.0
    pass_pow_k = 1.0 if len(passed_attempts) == k else 0.0

    return EvalReport(
        case_name=case_name,
        attempts=attempts,
        pass_rate=pass_rate,
        pass_at_k=pass_at_k,
        pass_pow_k=pass_pow_k,
        cost_per_success=_average_optional(a.cost_usd for a in passed_attempts),
        latency_per_success=_average_optional(a.latency_ms for a in passed_attempts),
    )


def _average_optional(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    if not present:
        return None
    return round(sum(present) / len(present), 6)
