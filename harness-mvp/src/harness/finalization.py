"""run 종료 경로: Safety 게이트 -> metrics/errors/final.md 기록 (2026-07-29 분리).

`orchestrator.py`에서 옮겨왔다. **동작은 그대로**이고 위치만 바뀌었다 — 절제 원칙
개정(구조 유지 보수는 트리거 없이)의 첫 적용이다. orchestrator가 3주에 367 -> 987줄로
자라서, 패턴과 무관한 공용 경로부터 뺐다.

여기 있는 것은 **어떤 패턴으로 실행했든 똑같이 지나가는 마무리 경로**다:

- `finalize()` — 정상 종료. Safety 검사 -> metrics -> (통과 시) errors/final.md.
  Safety에 걸리면 `_enter_safety_review()`로 보류시킨다
- `finalize_partial_chain()` — 체인 중단 시 그때까지 성공한 스텝을 partial로 승격
- `finalize_without_output()` — final.md 없이 종료(예: min_candidates 미달)
- `sum_optional_*()` — None이 섞인 지연/비용 합산

**Safety는 어떤 경로에서도 생략하지 않는다**(Section 12.1). 세 함수가 전부
`finalize()`로 모이게 해서 그 규칙이 한 곳에서만 구현되게 했다.

패턴별 핸들러(`_run_*`)는 orchestrator에 남겼다. 그중 `fan_out_judge`와
`hierarchical_delegation`은 폐기 검토 대상이라(측정 대기 —
`docs/01_개념설명/harness-vs-ecc-decision-2026-07-ko.md`) 분리하면 낭비가 될 수 있다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import learning, run_store, safety
from .schemas import Approval, DelegationStep, Observation, RunMetrics

def sum_optional_int(values: Iterable[Optional[int]]) -> Optional[int]:
    present = [v for v in values if v is not None]
    return sum(present) if present else None
def sum_optional_float(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present), 6) if present else None
def _check_safety(content: str, extra_scan_texts: Sequence[str]) -> Observation:
    """final.md 본문과 추가 대상(agentic_task 생성 파일)을 모두 스캔해 하나의 판정으로 합친다.

    스캔을 나눠서 하는 이유: 파일들을 그냥 이어붙여 한 번에 검사하면 어느 파일이
    걸렸는지 알 수 없어 사람 검토(safety_review.json의 note)가 무의미해진다.
    """
    observations = [safety.check(content)] + [safety.check(text) for text in extra_scan_texts]
    failures = [obs for obs in observations if obs.status == "error"]
    if not failures:
        return observations[0]

    return Observation(
        status="error",
        summary="; ".join(obs.summary for obs in failures),
        artifacts=[],
        next_actions=["ask_user"],
    )
def _enter_safety_review(
    run_dir: Path, content: str, *, errors: list[dict[str, str]], safety_obs: Observation
) -> Observation:
    """Safety 체크 실패 시 즉시 차단하는 대신 "검토 대기" 상태로 멈춘다 (Phase 4 Release Gate).

    승인 체크포인트(Approval)와 같은 pending/approved/rejected 상태 모델을 재사용한다.
    보류된 내용은 final.md가 아니라 pending_review_content.md에 저장해서, 검토 전까지는
    final.md 자체가 존재하지 않게 한다("출력이 아직 없다"는 걸 파일 존재 여부로도
    명확히 드러냄).
    """
    run_store.write_markdown(run_dir, "pending_review_content.md", content)
    review = Approval(status="pending", note=safety_obs.summary)
    run_store.write_json(run_dir, "safety_review.json", review.model_dump(mode="json"))
    run_store.write_json(run_dir, "errors.json", errors + [{"stage": "safety", "message": safety_obs.summary}])
    return Observation(
        status="warning",
        summary=f"Safety 점검 실패 — 사람 검토 대기 중 (run_id={run_dir.name})",
        artifacts=["safety.md", "pending_review_content.md"],
        next_actions=["ask_user"],
    )
def finalize(
    run_dir: Path,
    final_content: str,
    *,
    errors: list[dict[str, str]],
    latency_ms: Optional[int],
    cost_usd: Optional[float],
    subscription_calls: int = 0,
    completed: int,
    failed: int,
    stage: str,
    content_prefix: str = "",
    success_summary: Optional[str] = None,
    extra_scan_texts: Sequence[str] = (),
) -> Observation:
    """Safety 체크(항상 실행) 후 final.md/metrics.json/errors.json을 기록한다.

    errors에는 개별 후보/스텝이 재시도까지 실패한 기록이 담겨있을 수 있다 — run 전체가
    성공하더라도 그 실패 기록은 지우지 않고 그대로 errors.json에 남긴다 (DoD 요구사항).

    Safety 체크가 실패하면 즉시 차단하지 않고 사람 검토 대기 상태로 멈춘다(Phase 4
    Release Gate, `_enter_safety_review` 참고) — content_prefix(예: "(partial) ")는
    검토 후 공개될 때도 그대로 남아있어야 하므로, Safety 체크 전에 미리 적용해서
    저장해둔다.

    extra_scan_texts: final.md 본문 외에 추가로 스캔할 텍스트(agentic_task가 생성한
    파일 내용). 이 패턴은 진짜 산출물이 텍스트가 아니라 파일이라, 파일을 안 보면
    "Safety는 어떤 경로에서도 생략하지 않는다"(Section 12.1)가 형해화된다. 기본값이
    비어 있어 나머지 세 패턴의 동작은 그대로다.
    """
    formatted_content = f"{content_prefix}{final_content.rstrip()}\n"
    safety_obs = _check_safety(formatted_content, extra_scan_texts)
    run_store.write_markdown(
        run_dir, "safety.md", f"# Safety Check\n\n- status: {safety_obs.status}\n\n{safety_obs.summary}\n"
    )

    metrics = RunMetrics(
        latency_ms=latency_ms or 0,
        completed_candidates_or_steps=completed,
        failed_candidates_or_steps=failed,
        estimated_cost_usd=cost_usd,
        subscription_calls=subscription_calls,
    )
    run_store.write_json(run_dir, "metrics.json", metrics.model_dump(mode="json"))

    if safety_obs.status == "error":
        return _enter_safety_review(run_dir, formatted_content, errors=errors, safety_obs=safety_obs)

    run_store.write_json(run_dir, "errors.json", errors)
    run_store.write_markdown(run_dir, "final.md", formatted_content)
    # 여기서만 학습을 기록한다(2026-07-29). Safety 검토 대기로 빠진 run은 아직 끝난 게
    # 아니라(resume으로 이어짐) 기록하면 이중 계상된다. 실패해도 run을 망가뜨리지 않게
    # 감싼다 — 학습은 부가 기능인데 그것 때문에 완성된 산출물을 잃으면 배보다 배꼽이 크다.
    try:
        learning.record_run(run_dir)
    except Exception as exc:  # noqa: BLE001 - 학습 실패가 run 실패가 되면 안 된다
        errors = [*errors, {"stage": "learning", "message": f"학습 기록 실패(run은 정상): {exc}"}]
        run_store.write_json(run_dir, "errors.json", errors)

    status = "warning" if errors else "success"
    summary = success_summary or (
        f"{stage} run 완료" + (f" (경고 {len(errors)}건 — 나머지로 계속 진행함)" if errors else "")
    )
    return Observation(status=status, summary=summary, artifacts=["final.md"], next_actions=["continue"])
def finalize_partial_chain(
    run_dir: Path,
    executed_steps: list[DelegationStep],
    composed_content: str,
    *,
    latency_ms: Optional[int],
    cost_usd: Optional[float],
    subscription_calls: int = 0,
    completed: int,
    failed: int,
) -> Observation:
    """Section 6: 체인 중단 시 마지막 성공 스텝을 partial final로 승격.

    partial로 승격되는 내용도 실제로 final.md에 쓰이는 출력이므로, Safety 체크를 절대
    생략하지 않는다(Section 12.1: "Safety 체크는 어떤 경로에서도 생략하지 않는다") —
    실제 검사/보류 로직은 `finalize()`에 위임해서 두 경로(정상 완주/partial)가
    Safety 처리를 중복 구현하지 않게 한다.
    """
    failed_step = executed_steps[-1]
    last_success = next((s for s in reversed(executed_steps[:-1]) if s.status == "success"), None)

    errors = [
        {
            "stage": f"chain step '{failed_step.role}'",
            "message": f"재시도까지 실패해 체인 중단 (provider={failed_step.provider_id})",
        }
    ]

    if last_success is None:
        run_store.write_json(
            run_dir,
            "metrics.json",
            RunMetrics(
                latency_ms=latency_ms or 0,
                completed_candidates_or_steps=completed,
                failed_candidates_or_steps=failed,
                estimated_cost_usd=cost_usd,
                subscription_calls=subscription_calls,
            ).model_dump(mode="json"),
        )
        run_store.write_json(run_dir, "errors.json", errors)
        return Observation(
            status="error",
            summary=f"체인이 첫 단계('{failed_step.role}')부터 실패해 승격할 결과가 없음",
            artifacts=["errors.json"],
            next_actions=["ask_user"],
        )

    # 성공한 스텝을 전부 엮는다 — 정상 완주 경로와 같은 구성 규칙을 쓴다
    # (마지막 성공 스텝만 올리면 그 앞 단계의 산출물이 final.md에서 사라진다).
    return finalize(
        run_dir, composed_content, errors=errors,
        latency_ms=latency_ms, cost_usd=cost_usd,
        subscription_calls=subscription_calls,
        completed=completed, failed=failed, stage="hierarchical_delegation_partial",
        content_prefix="(partial) ",
        success_summary=(
            f"체인이 '{failed_step.role}' 단계에서 중단됨 — 그 앞까지 성공한 단계"
            f"({completed}개) 결과를 partial로 승격"
        ),
    )
def finalize_without_output(
    run_dir: Path,
    *,
    errors: list[dict[str, str]],
    latency_ms: Optional[int],
    cost_usd: Optional[float],
    subscription_calls: int = 0,
    completed: int,
    failed: int,
) -> Observation:
    """final.md 없이 종료한다 (예: fan_out_judge min_candidates 미달)."""
    run_store.write_json(run_dir, "errors.json", errors)
    run_store.write_json(
        run_dir,
        "metrics.json",
        RunMetrics(
            latency_ms=latency_ms or 0,
            completed_candidates_or_steps=completed,
            failed_candidates_or_steps=failed,
            estimated_cost_usd=cost_usd,
            subscription_calls=subscription_calls,
        ).model_dump(mode="json"),
    )
    reason = errors[-1]["message"] if errors else "알 수 없는 이유로 출력이 생성되지 않음"
    return Observation(status="error", summary=reason, artifacts=["errors.json"], next_actions=["ask_user"])
