"""Orchestrator: team_pattern 에 따라 flow 를 분기 실행하는 dispatcher (Step 8 완성).

harness-implementation-plan-ko.md Section 5(패턴 분기), Section 6(복구 전략),
Section 12.1/12.2(적합성 게이트, 승인 체크포인트), Phase 4(Safety Release Gate)를
구현한다.

`run(task, providers)`이 진입점이다. 순서:
1. 적합성 게이트(router.check_fitness) — 탈락하면 패턴 분기 없이 direct_call만 수행
2. Planner가 Plan 생성(team_pattern 확정)
3. risk_level="high"면 approval.json을 "pending"으로 쓰고 여기서 멈춘다
   (사람이 `resume(run_id, "approved"/"rejected", ...)`로 이어가야 함 — cli.py의
   `approve`/`reject` 명령이 이걸 호출한다)
4. team_pattern에 따라 fan_out_judge/hierarchical_delegation 실행
5. Safety 체크(항상 실행, 절대 생략하지 않음). 통과하면 final.md 기록. 실패하면
   즉시 차단하는 게 아니라 safety_review.json을 "pending"으로 쓰고 내용을
   pending_review_content.md에 보관한 채 "사람 검토 대기" 상태로 멈춘다 — Safety를
   "release gate"로 쓴다는 건 기계 판정이 최종 결정이 아니라 사람이 오탐 여부를
   확인할 수 있어야 한다는 뜻이다. `resolve_safety_review(run_id, "approved"/
   "rejected", ...)`가 이어받는다 (cli.py의 `safety-approve`/`safety-reject` 명령).
   승인 체크포인트(Approval)와 같은 pending/approved/rejected 상태 모델을 그대로
   재사용한다 — "approved" = 오탐으로 판단해 공개(release), "rejected" = 위험하다고
   확정해 계속 보류(block).

`replay(run_id)`는 저장된 run의 산출물을 다시 읽어 보여줄 뿐 재실행하지 않는다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, Literal, Optional

from providers.base import Provider

from . import judge, live_status, model_runner, planner, router, run_store, safety, subagent_runner, synthesizer
from .schemas import Approval, DelegationStep, Observation, Plan, RefinementRound, RunMetrics, TaskInput

MIN_CANDIDATES = 2  # Section 6: fan_out_judge, 이 이상 성공해야 평가를 진행한다

# fan_out_judge가 judge용 provider를 찾는 예약 키 (ADR 0004). providers dict에
# 이 키로 등록해두면 candidate 선택(_candidate_providers)에서는 제외되고
# judge.evaluate()에만 전달된다.
JUDGE_PROVIDER_KEY = "__judge__"

# iterative_refinement의 라운드 상한 (Section 6 "무한 재시도 금지"와 같은 철학).
# 라운드마다 generator+evaluator 2회 호출이 발생하므로 최악의 경우 LLM 호출 수는
# MAX_REFINEMENT_ROUNDS * 2 (+재시도)다. 상한 도달 시 마지막 시도를 partial로 승격한다.
# 비용 직결 값이라 config.json의 max_refinement_rounds로 조정 가능 — cli.py가
# MAX_SUBSCRIPTION_CANDIDATES와 같은 방식으로 실행 시 반영한다.
MAX_REFINEMENT_ROUNDS = 3

# Section 9 "구독 한도 초과 방지": cli_subscription provider(claude/codex CLI 등)는
# 5시간/주간 롤링 사용량 한도가 있다. fan_out_judge가 매 run마다 여러 구독 CLI를
# 동시에 호출하면 한도를 몇 배로 빨리 소모하므로, run당 최대 이 개수까지만 선택한다.
# api_key provider(종량제)는 한도 걱정이 없어 개수 제한을 받지 않는다.
MAX_SUBSCRIPTION_CANDIDATES = 1

_REPLAY_FILES = (
    "plan.md",
    "fitness_check.json",
    "approval.json",
    "judging.json",
    "refinement.json",
    "final.md",
    "safety.md",
    "safety_review.json",
    "pending_review_content.md",
    "metrics.json",
    "errors.json",
)


def run(task: TaskInput, providers: dict[str, Provider], *, root: Optional[Path] = None) -> Observation:
    """task 하나를 처음부터 실행한다. providers는 provider_id로 조회 가능한 mock/실제 provider 모음이다.

    root: 테스트에서 임시 디렉토리로 워크스페이스를 격리할 때 사용한다.
    """
    run_dir = run_store.create_run(run_id=f"run-{task.task_id}", root=root)
    live_status.write_run_meta(run_dir)
    run_store.write_json(run_dir, "input.json", task.model_dump(mode="json"))

    fitness = router.check_fitness(task)
    run_store.write_json(run_dir, "fitness_check.json", fitness.model_dump(mode="json"))

    if not fitness.passed:
        return _run_direct_call(task, providers, run_dir)

    plan = planner.create_plan(task)
    _write_plan(run_dir, plan)

    if plan.risk_level == "high":
        return _await_approval(run_dir, plan)

    return _run_pattern(task, plan, providers, run_dir)


def resume(
    run_id: str,
    decision: Literal["approved", "rejected"],
    providers: dict[str, Provider],
    *,
    root: Optional[Path] = None,
) -> Observation:
    """approval.json이 "pending"인 run을 승인/반려로 이어간다 (cli.py approve/reject).

    승인(approved)이면 저장된 input.json/plan.json을 다시 읽어 패턴 실행을 이어간다.
    반려(rejected)면 candidate/chain을 전혀 실행하지 않고 run을 종료한다 (Section 6).
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f'decision은 "approved" 또는 "rejected"만 가능하다: {decision!r}')

    run_dir = run_store.existing_run_dir(run_id, root=root)
    approval = Approval.model_validate(run_store.read_json(run_dir, "approval.json"))
    if approval.status != "pending":
        raise ValueError(f"이미 처리된 approval이다 (status={approval.status!r}) — run_id={run_id}")

    approval = Approval(status=decision, decided_at=datetime.now(timezone.utc))
    run_store.write_json(run_dir, "approval.json", approval.model_dump(mode="json"))

    if decision == "rejected":
        run_store.write_json(
            run_dir, "errors.json", [{"stage": "approval", "message": "사용자가 반려함"}]
        )
        return Observation(
            status="error",
            summary="사용자가 반려하여 run을 종료함 (candidate/chain 실행 없음)",
            artifacts=["approval.json"],
            next_actions=[],
        )

    task = TaskInput.model_validate(run_store.read_json(run_dir, "input.json"))
    plan = Plan.model_validate(run_store.read_json(run_dir, "plan.json"))
    # 승인 대기 중이던 run은 원래 프로세스가 이미 종료됐을 수 있다(별도 CLI 호출로
    # 재개하는 경우가 흔함) — 지금 실제로 패턴을 실행하는 이 프로세스로 pid를 갱신.
    live_status.write_run_meta(run_dir)
    return _run_pattern(task, plan, providers, run_dir)


def resolve_safety_review(
    run_id: str,
    decision: Literal["approved", "rejected"],
    *,
    root: Optional[Path] = None,
) -> Observation:
    """safety_review.json이 "pending"인 run을 사람이 검토한 뒤 결론짓는다
    (cli.py safety-approve/safety-reject).

    "approved"(오탐으로 판단)면 보류해뒀던 내용을 그대로 final.md로 공개(release)한다.
    "rejected"(위험하다고 확정)면 계속 보류(block)하고, final.md는 끝내 생성되지 않는다.
    """
    if decision not in ("approved", "rejected"):
        raise ValueError(f'decision은 "approved" 또는 "rejected"만 가능하다: {decision!r}')

    run_dir = run_store.existing_run_dir(run_id, root=root)
    review = Approval.model_validate(run_store.read_json(run_dir, "safety_review.json"))
    if review.status != "pending":
        raise ValueError(f"이미 처리된 safety review다 (status={review.status!r}) — run_id={run_id}")

    review = Approval(status=decision, note=review.note, decided_at=datetime.now(timezone.utc))
    run_store.write_json(run_dir, "safety_review.json", review.model_dump(mode="json"))

    if decision == "rejected":
        return Observation(
            status="error",
            summary="사람이 검토 후 위험하다고 확정 — 계속 보류함 (final.md 생성 안 됨)",
            artifacts=["safety_review.json"],
            next_actions=[],
        )

    pending_content = run_store.read_markdown(run_dir, "pending_review_content.md")
    run_store.write_markdown(run_dir, "final.md", pending_content)
    return Observation(
        status="warning",
        summary="사람이 검토 후 오탐으로 판단 — 보류했던 내용을 공개함",
        artifacts=["final.md", "safety_review.json"],
        next_actions=["continue"],
    )


def list_safety_review_queue(*, root: Optional[Path] = None) -> list[dict]:
    """검토 대기 중(safety_review.json status="pending")인 run 목록을 반환한다 (cli.py safety-queue)."""
    queue_root = root if root is not None else run_store.DEFAULT_WORKSPACE_ROOT
    queue: list[dict] = []
    for run_id in run_store.list_runs(root=queue_root):
        review_path = queue_root / run_id / "safety_review.json"
        if not review_path.exists():
            continue
        review = Approval.model_validate(run_store.read_json(queue_root / run_id, "safety_review.json"))
        if review.status == "pending":
            queue.append({"run_id": run_id, "reason": review.note})
    return queue


def replay(run_id: str, *, root: Optional[Path] = None) -> dict[str, str]:
    """저장된 run의 주요 산출물을 다시 읽어 반환한다 (재실행이 아니라 조회)."""
    run_dir = run_store.existing_run_dir(run_id, root=root)
    result: dict[str, str] = {}
    for name in _REPLAY_FILES:
        path = run_dir / name
        if path.exists():
            result[name] = path.read_text(encoding="utf-8")
    return result


def _await_approval(run_dir: Path, plan: Plan) -> Observation:
    approval = Approval(status="pending")
    run_store.write_json(run_dir, "approval.json", approval.model_dump(mode="json"))
    run_store.write_json(
        run_dir,
        "metrics.json",
        RunMetrics(latency_ms=0, completed_candidates_or_steps=0, failed_candidates_or_steps=0).model_dump(
            mode="json"
        ),
    )
    run_store.write_json(run_dir, "errors.json", [])
    return Observation(
        status="warning",
        summary=f"risk_level=high — 사람 승인 대기 중 (run_id={run_dir.name})",
        artifacts=["approval.json"],
        next_actions=["ask_user"],
    )


def _run_pattern(task: TaskInput, plan: Plan, providers: dict[str, Provider], run_dir: Path) -> Observation:
    if plan.team_pattern == "fan_out_judge":
        return _run_fan_out_judge(task, plan, providers, run_dir)
    if plan.team_pattern == "hierarchical_delegation":
        return _run_hierarchical_delegation(task, plan, providers, run_dir)
    if plan.team_pattern == "iterative_refinement":
        return _run_iterative_refinement(task, plan, providers, run_dir)
    raise ValueError(f"unknown team_pattern: {plan.team_pattern!r}")


def _candidate_providers(providers: dict[str, Provider]) -> dict[str, Provider]:
    """judge 전용으로 예약된 키(JUDGE_PROVIDER_KEY)를 제외한, 실제 후보/역할
    호출용 provider만 추린다 — direct_call/fan_out_judge가 judge provider를
    후보로 잘못 집어가지 않도록 방어한다."""
    return {key: p for key, p in providers.items() if key != JUDGE_PROVIDER_KEY}


def _limit_subscription_candidates(candidate_providers: list[Provider]) -> list[Provider]:
    """구독 한도 초과 방지 (Section 9): auth_mode="cli_subscription"인 provider가
    MAX_SUBSCRIPTION_CANDIDATES개를 넘게 선택되면 초과분을 제외한다 —
    fan_out_judge가 매 run마다 여러 구독 CLI(claude+codex 등)의 5시간/주간
    롤링 한도를 동시에 소모하지 않도록 한다. api_key provider는 종량제라
    한도 걱정이 없으므로 전부 유지한다.

    이 제한을 적용했을 때 남는 후보 수가 MIN_CANDIDATES 미만으로 떨어지면
    적용하지 않는다 — 한도 보호보다 "run이 아예 실패하는 것"이 더 나쁘다.
    원래 순서는 최대한 보존한다(재현성/가독성 목적).
    """
    subscription = [p for p in candidate_providers if p.config.auth_mode == "cli_subscription"]
    if len(subscription) <= MAX_SUBSCRIPTION_CANDIDATES:
        return candidate_providers

    kept_subscription_ids = {id(p) for p in subscription[:MAX_SUBSCRIPTION_CANDIDATES]}
    limited = [
        p for p in candidate_providers if p.config.auth_mode != "cli_subscription" or id(p) in kept_subscription_ids
    ]
    if len(limited) < MIN_CANDIDATES:
        return candidate_providers

    return limited


def _run_direct_call(task: TaskInput, providers: dict[str, Provider], run_dir: Path) -> Observation:
    provider = next(iter(_candidate_providers(providers).values()))
    candidate = model_runner.direct_call(task.prompt, provider)
    errors = (
        []
        if candidate.status == "success"
        else [{"stage": "direct_call", "message": f"재시도까지 실패: {candidate.content}"}]
    )
    return _finalize(
        run_dir,
        candidate.content,
        errors=errors,
        latency_ms=candidate.latency_ms,
        cost_usd=candidate.cost_usd,
        completed=1 if candidate.status == "success" else 0,
        failed=0 if candidate.status == "success" else 1,
        stage="direct_call",
    )


def _run_fan_out_judge(task: TaskInput, plan: Plan, providers: dict[str, Provider], run_dir: Path) -> Observation:
    judge_provider = providers.get(JUDGE_PROVIDER_KEY)
    if judge_provider is None:
        raise ValueError(
            f"fan_out_judge에는 judge용 provider가 필요하다 "
            f"(providers[{JUDGE_PROVIDER_KEY!r}]에 등록, ADR 0004 참고)"
        )

    candidate_providers = _limit_subscription_candidates(list(_candidate_providers(providers).values()))
    num_candidates = plan.num_candidates if plan.num_candidates is not None else len(candidate_providers)
    selected = candidate_providers[:num_candidates]
    candidates = model_runner.run_all(task.prompt, selected, run_dir)

    # 후보 하나가 재시도까지 실패해도 run 전체가 성공할 수 있다 — 그래도 DoD/Section 6에
    # 따라 그 실패는 errors.json에 반드시 남긴다 (전체 성공 여부와 무관하게).
    errors = [
        {"stage": f"candidate '{c.model_id}'", "message": f"재시도까지 실패: {c.content}"}
        for c in candidates
        if c.status == "error"
    ]

    successful = [c for c in candidates if c.status == "success"]
    completed, failed = len(successful), len(candidates) - len(successful)
    latency_ms = _sum_optional_int(c.latency_ms for c in candidates)
    cost_usd = _sum_optional_float(c.cost_usd for c in candidates)

    if len(successful) < MIN_CANDIDATES:
        errors.append(
            {
                "stage": "fan_out_judge",
                "message": f"성공한 후보가 {len(successful)}개로 min_candidates({MIN_CANDIDATES}) 미만",
            }
        )
        return _finalize_without_output(
            run_dir, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd, completed=completed, failed=failed
        )

    try:
        judging = judge.evaluate(candidates, plan.rubric, judge_provider)
    except judge.JudgeError as exc:
        errors.append({"stage": "judge", "message": str(exc)})
        return _finalize_without_output(
            run_dir, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd, completed=completed, failed=failed
        )

    run_store.write_json(run_dir, "judging.json", judging.model_dump(mode="json"))
    final_content = synthesizer.synthesize(candidates, judging)
    # judge 호출 자체의 지연/비용도 합산한다 (Cost Blindness 방지, ADR 0004).
    total_latency_ms = _sum_optional_int([latency_ms, judging.latency_ms])
    total_cost_usd = _sum_optional_float([cost_usd, judging.cost_usd])

    return _finalize(
        run_dir, final_content, errors=errors, latency_ms=total_latency_ms, cost_usd=total_cost_usd,
        completed=completed, failed=failed, stage="fan_out_judge",
    )


def _run_hierarchical_delegation(
    task: TaskInput, plan: Plan, providers: dict[str, Provider], run_dir: Path
) -> Observation:
    observations, chain_completed = subagent_runner.run_chain(
        plan.delegation_chain, providers, task.prompt, run_dir
    )
    executed_steps = plan.delegation_chain[: len(observations)]
    completed = sum(1 for step in executed_steps if step.status == "success")
    failed = sum(1 for step in executed_steps if step.status == "error")
    latency_ms = _sum_optional_int(step.latency_ms for step in executed_steps)
    cost_usd = _sum_optional_float(step.cost_usd for step in executed_steps)

    if not chain_completed:
        return _finalize_partial_chain(
            run_dir, executed_steps, latency_ms=latency_ms, cost_usd=cost_usd, completed=completed, failed=failed
        )

    final_content = run_store.read_markdown(run_dir, executed_steps[-1].output_ref)
    return _finalize(
        run_dir, final_content, errors=[], latency_ms=latency_ms, cost_usd=cost_usd, completed=completed,
        failed=failed, stage="hierarchical_delegation",
    )


def _run_iterative_refinement(
    task: TaskInput, plan: Plan, providers: dict[str, Provider], run_dir: Path
) -> Observation:
    """생성 → 합격 판정 → 피드백 반영 재생성을 반복한다 (반복 개선 루프).

    fan_out_judge가 "여러 후보를 병렬로 만들어 비교"라면, 이 패턴은 "한 계보를
    피드백으로 반복 개선"이다(revfactory/harness v2의 모드 B에 대응). evaluator는
    fan_out_judge와 같은 JUDGE_PROVIDER_KEY 등록 인프라를 재사용한다.

    실패 처리 철학은 hierarchical_delegation의 partial 승격과 동일하다 — 라운드
    상한 도달/중간 실패 시 마지막으로 생성에 성공한 내용을 버리지 않고 partial로
    승격하고, 미통과/실패 기록은 errors.json에 남긴다.
    """
    judge_provider = providers.get(JUDGE_PROVIDER_KEY)
    if judge_provider is None:
        raise ValueError(
            f"iterative_refinement에는 evaluator용 provider가 필요하다 "
            f"(providers[{JUDGE_PROVIDER_KEY!r}]에 등록, fan_out_judge의 judge와 동일)"
        )

    candidate_providers = _candidate_providers(providers)
    if not candidate_providers:
        raise ValueError("iterative_refinement에는 generator용 provider가 최소 1개 필요하다")
    generator = next(iter(candidate_providers.values()))

    rounds: list[RefinementRound] = []
    errors: list[dict[str, str]] = []
    latency_parts: list[Optional[int]] = []
    cost_parts: list[Optional[float]] = []
    last_content: Optional[str] = None
    generated_count = 0
    generation_failed = False
    passed = False
    prompt = task.prompt

    for round_index in range(1, MAX_REFINEMENT_ROUNDS + 1):
        candidate = model_runner.generate_with_retry(generator, prompt)
        latency_parts.append(candidate.latency_ms)
        cost_parts.append(candidate.cost_usd)

        if candidate.status == "error":
            generation_failed = True
            errors.append(
                {
                    "stage": f"refinement round {round_index}",
                    "message": f"생성이 재시도까지 실패해 루프 중단: {candidate.content}",
                }
            )
            break
        last_content = candidate.content
        generated_count += 1

        try:
            verdict = judge.check_pass(candidate.content, plan.rubric, judge_provider)
        except judge.JudgeError as exc:
            errors.append({"stage": f"refinement round {round_index} evaluator", "message": str(exc)})
            break
        latency_parts.append(verdict.latency_ms)
        cost_parts.append(verdict.cost_usd)

        rounds.append(
            RefinementRound(
                round_index=round_index,
                content=candidate.content,
                passed=verdict.passed,
                feedback=verdict.feedback,
                latency_ms=_sum_optional_int([candidate.latency_ms, verdict.latency_ms]),
                cost_usd=_sum_optional_float([candidate.cost_usd, verdict.cost_usd]),
            )
        )

        if verdict.passed:
            passed = True
            break
        prompt = _build_refinement_prompt(task.prompt, candidate.content, verdict.feedback)

    run_store.write_json(run_dir, "refinement.json", [r.model_dump(mode="json") for r in rounds])

    failed = 1 if generation_failed else 0
    latency_ms = _sum_optional_int(latency_parts)
    cost_usd = _sum_optional_float(cost_parts)

    if last_content is None:
        return _finalize_without_output(
            run_dir, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd, completed=0, failed=failed
        )

    if passed:
        return _finalize(
            run_dir, last_content, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
            completed=generated_count, failed=failed, stage="iterative_refinement",
            success_summary=f"iterative_refinement run 완료 — {len(rounds)}라운드 만에 rubric 통과",
        )

    if not errors:
        errors.append(
            {
                "stage": "iterative_refinement",
                "message": f"라운드 상한({MAX_REFINEMENT_ROUNDS}회)까지 rubric을 통과하지 못함",
            }
        )
    return _finalize(
        run_dir, last_content, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
        completed=generated_count, failed=failed, stage="iterative_refinement_partial",
        content_prefix="(partial) ",
        success_summary=(
            f"rubric을 통과하지 못한 채 루프 종료 — 마지막 생성 결과를 partial로 승격"
            f" (실행 {generated_count}라운드)"
        ),
    )


def _build_refinement_prompt(original_prompt: str, previous_content: str, feedback: str) -> str:
    return (
        "다음 요청에 대한 이전 답변이 심사에서 통과하지 못했다. 심사 피드백을 반영해 "
        "답변을 다시 작성하라.\n\n"
        "## 원래 요청\n"
        f"{original_prompt}\n\n"
        "## 이전 답변\n"
        f"{previous_content}\n\n"
        "## 심사 피드백\n"
        f"{feedback}\n\n"
        "## 지시\n"
        "피드백에서 지적된 문제를 모두 해결한, 완성된 답변만 출력하라 (수정 과정 설명 없이).\n"
    )


def _finalize_partial_chain(
    run_dir: Path,
    executed_steps: list[DelegationStep],
    *,
    latency_ms: Optional[int],
    cost_usd: Optional[float],
    completed: int,
    failed: int,
) -> Observation:
    """Section 6: 체인 중단 시 마지막 성공 스텝을 partial final로 승격.

    partial로 승격되는 내용도 실제로 final.md에 쓰이는 출력이므로, Safety 체크를 절대
    생략하지 않는다(Section 12.1: "Safety 체크는 어떤 경로에서도 생략하지 않는다") —
    실제 검사/보류 로직은 `_finalize()`에 위임해서 두 경로(정상 완주/partial)가
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
            ).model_dump(mode="json"),
        )
        run_store.write_json(run_dir, "errors.json", errors)
        return Observation(
            status="error",
            summary=f"체인이 첫 단계('{failed_step.role}')부터 실패해 승격할 결과가 없음",
            artifacts=["errors.json"],
            next_actions=["ask_user"],
        )

    partial_content = run_store.read_markdown(run_dir, last_success.output_ref)
    return _finalize(
        run_dir, partial_content, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
        completed=completed, failed=failed, stage="hierarchical_delegation_partial",
        content_prefix="(partial) ",
        success_summary=(
            f"체인이 '{failed_step.role}' 단계에서 중단됨 — 마지막 성공 단계"
            f"('{last_success.role}') 결과를 partial로 승격"
        ),
    )


def _finalize_without_output(
    run_dir: Path,
    *,
    errors: list[dict[str, str]],
    latency_ms: Optional[int],
    cost_usd: Optional[float],
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
        ).model_dump(mode="json"),
    )
    reason = errors[-1]["message"] if errors else "알 수 없는 이유로 출력이 생성되지 않음"
    return Observation(status="error", summary=reason, artifacts=["errors.json"], next_actions=["ask_user"])


def _finalize(
    run_dir: Path,
    final_content: str,
    *,
    errors: list[dict[str, str]],
    latency_ms: Optional[int],
    cost_usd: Optional[float],
    completed: int,
    failed: int,
    stage: str,
    content_prefix: str = "",
    success_summary: Optional[str] = None,
) -> Observation:
    """Safety 체크(항상 실행) 후 final.md/metrics.json/errors.json을 기록한다.

    errors에는 개별 후보/스텝이 재시도까지 실패한 기록이 담겨있을 수 있다 — run 전체가
    성공하더라도 그 실패 기록은 지우지 않고 그대로 errors.json에 남긴다 (DoD 요구사항).

    Safety 체크가 실패하면 즉시 차단하지 않고 사람 검토 대기 상태로 멈춘다(Phase 4
    Release Gate, `_enter_safety_review` 참고) — content_prefix(예: "(partial) ")는
    검토 후 공개될 때도 그대로 남아있어야 하므로, Safety 체크 전에 미리 적용해서
    저장해둔다.
    """
    formatted_content = f"{content_prefix}{final_content.rstrip()}\n"
    safety_obs = safety.check(formatted_content)
    run_store.write_markdown(
        run_dir, "safety.md", f"# Safety Check\n\n- status: {safety_obs.status}\n\n{safety_obs.summary}\n"
    )

    metrics = RunMetrics(
        latency_ms=latency_ms or 0,
        completed_candidates_or_steps=completed,
        failed_candidates_or_steps=failed,
        estimated_cost_usd=cost_usd,
    )
    run_store.write_json(run_dir, "metrics.json", metrics.model_dump(mode="json"))

    if safety_obs.status == "error":
        return _enter_safety_review(run_dir, formatted_content, errors=errors, safety_obs=safety_obs)

    run_store.write_json(run_dir, "errors.json", errors)
    run_store.write_markdown(run_dir, "final.md", formatted_content)
    status = "warning" if errors else "success"
    summary = success_summary or (
        f"{stage} run 완료" + (f" (경고 {len(errors)}건 — 나머지로 계속 진행함)" if errors else "")
    )
    return Observation(status=status, summary=summary, artifacts=["final.md"], next_actions=["continue"])


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


def _write_plan(run_dir: Path, plan: Plan) -> None:
    run_store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))
    run_store.write_markdown(
        run_dir, "plan.md", f"# Plan\n\n```json\n{run_store.json_dumps(plan.model_dump(mode='json'))}\n```\n"
    )


def _sum_optional_int(values: Iterable[Optional[int]]) -> Optional[int]:
    present = [v for v in values if v is not None]
    return sum(present) if present else None


def _sum_optional_float(values: Iterable[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return round(sum(present), 6) if present else None
