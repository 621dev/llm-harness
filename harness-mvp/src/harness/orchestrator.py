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
4. team_pattern에 따라 fan_out_judge/hierarchical_delegation/iterative_refinement/
   agentic_task 실행
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
from typing import Literal, Optional

from providers.base import Provider

from . import (
    agent_runner,
    judge,
    live_status,
    model_runner,
    planner,
    router,
    run_store,
    safety,
    subagent_runner,
    learning,
    synthesizer,
)
from . import finalization
from .budget import BudgetTracker
from .schemas import Approval, DelegationStep, Observation, Plan, RefinementRound, RunMetrics, TaskInput

MIN_CANDIDATES = 2  # Section 6: fan_out_judge, 이 이상 성공해야 평가를 진행한다

# fan_out_judge가 judge용 provider를 찾는 예약 키 (ADR 0004). providers dict에
# 이 키로 등록해두면 candidate 선택(_candidate_providers)에서는 제외되고
# judge.evaluate()에만 전달된다.
JUDGE_PROVIDER_KEY = "__judge__"

# agentic_task가 에이전트 provider를 찾는 예약 키 (ADR 0007, JUDGE_PROVIDER_KEY와 대칭).
# 이 키로 등록된 provider는 일반 후보/역할 호출에서 제외되고 agentic_task에서만 쓰인다 —
# 도구 사용 권한을 가진 provider가 다른 패턴에 실수로 섞여 들어가지 않게 하는 안전장치이기도 하다.
AGENT_PROVIDER_KEY = "__agent__"

# agentic_task의 턴 상한. 에이전트가 도구를 호출하며 진행하는 횟수를 묶는다
# (Section 6 "무한 재시도 금지"와 같은 철학, 여기서는 무한 루프 금지).
# config.json의 max_agent_turns로 조정 가능 — cli.py가 실행 시 반영한다.
# 기본값 8: 첫 e2e(2026-07-27) 실측에서 에이전트가 초반 2~3턴을 방향 파악
# (허용 안 된 도구 시도 포함)에 쓰는 걸 보고 5에서 올렸다 — 5로는 파일 3개짜리
# 작업이 매번 상한에 걸려 partial로 끝났다.
MAX_AGENT_TURNS = 8

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

# run 하나의 예산 상한 (2026-07-29, ECC `cost-aware-llm-pipeline` 재분석). 둘 다 None이면
# 아무것도 막지 않는다 — 기존 동작 유지가 기본값이다. 금액과 구독 한도는 서로 다른
# 자원이라 합칠 수 없어 따로 둔다(`budget.py` 참고). 비용 직결 값이라 config.json의
# budget_usd / budget_subscription_calls로 조정 — cli.py가 MAX_AGENT_TURNS와 같은
# 방식으로 실행 시 반영한다.
BUDGET_USD: Optional[float] = None
BUDGET_SUBSCRIPTION_CALLS: Optional[int] = None

# agentic_task 에이전트에 주입할 시스템 프롬프트. None이면 provider 기본값
# (`DEFAULT_AGENT_SYSTEM_PROMPT` — 작업공간 격리/사용 가능 도구/산출물이 파일이라는
# 것을 미리 알려줘 초반 턴 낭비를 줄인다). config.json의 `agent_system_prompt`로
# 도메인별로 바꿀 수 있고, 빈 문자열을 주면 주입을 끈다.
AGENT_SYSTEM_PROMPT: Optional[str] = None

# run 간 학습 (2026-07-29). 기록은 항상 자동이고, **주입은 사람이 `learned.md`를
# 썼을 때만** 일어난다 — 파일을 쓰는 행위 자체가 승인이다(`learning.py` 참고).
# config.json의 `use_learned_notes: false`로 주입만 끌 수 있다(기록은 계속된다).
USE_LEARNED_NOTES = True

_REPLAY_FILES = (
    "plan.md",
    "fitness_check.json",
    "approval.json",
    "judging.json",
    "refinement.json",
    "agent_turns.json",
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
    task = _apply_learned_notes(task, run_dir)

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
    # 예산 추적은 run 하나에 스코프된다 — 패턴 핸들러를 거쳐 모든 LLM 호출 경로로
    # 전달되고, 상한이 없으면(기본값) 아무 동작도 하지 않는다.
    budget = BudgetTracker(limit_usd=BUDGET_USD, limit_calls=BUDGET_SUBSCRIPTION_CALLS)
    if plan.team_pattern == "fan_out_judge":
        return _run_fan_out_judge(task, plan, providers, run_dir, budget)
    if plan.team_pattern == "hierarchical_delegation":
        return _run_hierarchical_delegation(task, plan, providers, run_dir, budget)
    if plan.team_pattern == "iterative_refinement":
        return _run_iterative_refinement(task, plan, providers, run_dir, budget)
    if plan.team_pattern == "agentic_task":
        return _run_agentic_task(task, providers, run_dir, budget)
    raise ValueError(f"unknown team_pattern: {plan.team_pattern!r}")


def _candidate_providers(providers: dict[str, Provider]) -> dict[str, Provider]:
    """예약 키(judge/agent)를 제외한, 실제 후보/역할 호출용 provider만 추린다 —
    direct_call/fan_out_judge가 judge provider를 후보로 잘못 집어가지 않도록
    방어한다. 에이전트 provider(AGENT_PROVIDER_KEY)도 같이 제외한다: 도구 사용
    권한을 가진 provider가 일반 텍스트 생성 자리에 섞여 들어가면 안 된다."""
    return {key: p for key, p in providers.items() if key not in (JUDGE_PROVIDER_KEY, AGENT_PROVIDER_KEY)}


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
    candidate = model_runner.direct_call(task.prompt, provider)  # 단발 호출이라 상한 대상 아님
    errors = (
        []
        if candidate.status == "success"
        else [{"stage": "direct_call", "message": f"재시도까지 실패: {candidate.content}"}]
    )
    return finalization.finalize(
        run_dir,
        candidate.content,
        errors=errors,
        latency_ms=candidate.latency_ms,
        cost_usd=candidate.cost_usd,
        subscription_calls=candidate.subscription_calls,
        completed=1 if candidate.status == "success" else 0,
        failed=0 if candidate.status == "success" else 1,
        stage="direct_call",
    )


def _run_fan_out_judge(
    task: TaskInput, plan: Plan, providers: dict[str, Provider], run_dir: Path, budget: BudgetTracker
) -> Observation:
    judge_provider = providers.get(JUDGE_PROVIDER_KEY)
    if judge_provider is None:
        raise ValueError(
            f"fan_out_judge에는 judge용 provider가 필요하다 "
            f"(providers[{JUDGE_PROVIDER_KEY!r}]에 등록, ADR 0004 참고)"
        )

    candidate_providers = _limit_subscription_candidates(list(_candidate_providers(providers).values()))
    num_candidates = plan.num_candidates if plan.num_candidates is not None else len(candidate_providers)
    selected = candidate_providers[:num_candidates]
    candidates = model_runner.run_all(task.prompt, selected, run_dir, budget=budget)

    # 후보 하나가 재시도까지 실패해도 run 전체가 성공할 수 있다 — 그래도 DoD/Section 6에
    # 따라 그 실패는 errors.json에 반드시 남긴다 (전체 성공 여부와 무관하게).
    errors = [
        # `kind`/`provider`는 소비자가 문구를 파싱하지 않게 하려고 붙인다(2026-07-29,
        # learning 집계가 첫 소비자). stage/message는 사람이 읽는 용도로 그대로 둔다.
        {
            "kind": "candidate_failure",
            "provider": c.model_id,
            "stage": f"candidate '{c.model_id}'",
            "message": f"재시도까지 실패: {c.content}",
        }
        for c in candidates
        if c.status == "error"
    ]
    if len(candidates) < len(selected):
        # run_all이 예산 상한에 걸려 남은 provider를 호출하지 않고 멈췄다.
        # 후보가 아예 안 만들어진 provider는 error Candidate조차 없으므로 여기서 남긴다.
        errors.append({"kind": "budget", "stage": "fan_out_judge", "message": budget.reason})

    successful = [c for c in candidates if c.status == "success"]
    completed, failed = len(successful), len(candidates) - len(successful)
    latency_ms = finalization.sum_optional_int(c.latency_ms for c in candidates)
    cost_usd = finalization.sum_optional_float(c.cost_usd for c in candidates)
    subscription_calls = sum(c.subscription_calls for c in candidates)

    if len(successful) < MIN_CANDIDATES:
        errors.append(
            {
                "stage": "fan_out_judge",
                "message": f"성공한 후보가 {len(successful)}개로 min_candidates({MIN_CANDIDATES}) 미만",
            }
        )
        return finalization.finalize_without_output(
            run_dir, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
            subscription_calls=subscription_calls, completed=completed, failed=failed
        )

    try:
        judging = judge.evaluate(candidates, plan.rubric, judge_provider, budget=budget)
    except judge.JudgeError as exc:
        errors.append({"stage": "judge", "message": str(exc)})
        return finalization.finalize_without_output(
            run_dir, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
            subscription_calls=subscription_calls, completed=completed, failed=failed
        )

    run_store.write_json(run_dir, "judging.json", judging.model_dump(mode="json"))
    final_content = synthesizer.synthesize(candidates, judging)
    # judge 호출 자체의 지연/비용도 합산한다 (Cost Blindness 방지, ADR 0004).
    total_latency_ms = finalization.sum_optional_int([latency_ms, judging.latency_ms])
    total_cost_usd = finalization.sum_optional_float([cost_usd, judging.cost_usd])

    return finalization.finalize(
        run_dir, final_content, errors=errors, latency_ms=total_latency_ms, cost_usd=total_cost_usd,
        subscription_calls=subscription_calls + judging.subscription_calls,
        completed=completed, failed=failed, stage="fan_out_judge",
    )


def _run_hierarchical_delegation(
    task: TaskInput, plan: Plan, providers: dict[str, Provider], run_dir: Path, budget: BudgetTracker
) -> Observation:
    observations, chain_completed = subagent_runner.run_chain(
        plan.delegation_chain, providers, task.prompt, run_dir, budget=budget
    )
    executed_steps = plan.delegation_chain[: len(observations)]
    completed = sum(1 for step in executed_steps if step.status == "success")
    failed = sum(1 for step in executed_steps if step.status == "error")
    latency_ms = finalization.sum_optional_int(step.latency_ms for step in executed_steps)
    cost_usd = finalization.sum_optional_float(step.cost_usd for step in executed_steps)
    subscription_calls = sum(step.subscription_calls for step in executed_steps)

    if not chain_completed:
        return finalization.finalize_partial_chain(
            run_dir, executed_steps, _render_chain_final(run_dir, executed_steps),
            latency_ms=latency_ms, cost_usd=cost_usd,
            subscription_calls=subscription_calls, completed=completed, failed=failed
        )

    return finalization.finalize(
        run_dir, _render_chain_final(run_dir, executed_steps), errors=[],
        latency_ms=latency_ms, cost_usd=cost_usd,
        subscription_calls=subscription_calls, completed=completed,
        failed=failed, stage="hierarchical_delegation",
    )


def _render_chain_final(run_dir: Path, steps: list[DelegationStep]) -> str:
    """성공한 스텝들의 본문을 엮어 최종 산출물을 만든다.

    **왜 마지막 스텝만 쓰지 않는가**(2026-07-28 패턴 부가가치 측정에서 발견):
    예전에는 `steps[-1]`의 출력을 그대로 final.md로 삼았는데, `[research,
    design_review]`처럼 마지막이 "검토" 역할인 체인에서는 최종 산출물이 사용자가
    요청한 내용이 아니라 **그것에 대한 리뷰 코멘트**가 됐다. 요청한 내용은
    중간 산출물(`artifacts/chain/step-1-*.md`)에만 남아 final.md에서는 보이지
    않았고, 그래서 direct_call과 비교 측정했을 때 체인이 불리하게 나왔다 —
    품질 문제가 아니라 "다른 물건을 내놓고 있던" 문제였다.

    LLM을 한 번 더 불러 합성하지 않고 규칙 기반으로 엮는다: fan_out_judge의
    synthesizer도 규칙 기반이고, 체인 run마다 호출을 추가하는 건 "필요할 때만
    만든다" 원칙(ADR 0003/0005)에 어긋난다. 스텝이 하나뿐이면 역할 제목 없이
    본문만 — 1스텝 체인에까지 구조를 씌우면 소음이다.
    """
    successful = [step for step in steps if step.status == "success"]
    if not successful:
        return ""
    if len(successful) == 1:
        return subagent_runner.read_step_content(run_dir, successful[0])

    sections = [
        f"## {index}. {step.role}\n\n{subagent_runner.read_step_content(run_dir, step)}"
        for index, step in enumerate(successful, start=1)
    ]
    return "\n\n---\n\n".join(sections)


def _run_iterative_refinement(
    task: TaskInput, plan: Plan, providers: dict[str, Provider], run_dir: Path, budget: BudgetTracker
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
    subscription_calls = 0
    generation_failed = False
    passed = False
    prompt = task.prompt

    for round_index in range(1, MAX_REFINEMENT_ROUNDS + 1):
        if budget.exhausted:
            # 라운드마다 생성+판정 2회를 쓰므로 시작 전에 막는 게 의미가 크다.
            # 이미 만든 산출물은 아래에서 partial로 승격된다.
            errors.append(
                {"kind": "budget", "stage": f"refinement round {round_index}", "message": budget.reason}
            )
            break
        candidate = model_runner.generate_with_retry(generator, prompt, budget=budget)
        latency_parts.append(candidate.latency_ms)
        cost_parts.append(candidate.cost_usd)
        subscription_calls += candidate.subscription_calls

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
            # 원본 요청을 함께 준다(2026-07-29) — 안 주면 evaluator가 "요청이 시켜서
            # 들어간 섹션"을 결함으로 오판한다(2차 측정에서 실제로 관측).
            verdict = judge.check_pass(
                candidate.content, plan.rubric, judge_provider, request=task.prompt, budget=budget
            )
        except judge.JudgeError as exc:
            errors.append({"stage": f"refinement round {round_index} evaluator", "message": str(exc)})
            break
        latency_parts.append(verdict.latency_ms)
        cost_parts.append(verdict.cost_usd)
        subscription_calls += verdict.subscription_calls

        rounds.append(
            RefinementRound(
                round_index=round_index,
                content=candidate.content,
                passed=verdict.passed,
                feedback=verdict.feedback,
                latency_ms=finalization.sum_optional_int([candidate.latency_ms, verdict.latency_ms]),
                cost_usd=finalization.sum_optional_float([candidate.cost_usd, verdict.cost_usd]),
                subscription_calls=candidate.subscription_calls + verdict.subscription_calls,
            )
        )

        if verdict.passed:
            passed = True
            break
        prompt = _build_refinement_prompt(task.prompt, candidate.content, verdict.feedback)

    run_store.write_json(run_dir, "refinement.json", [r.model_dump(mode="json") for r in rounds])

    failed = 1 if generation_failed else 0
    latency_ms = finalization.sum_optional_int(latency_parts)
    cost_usd = finalization.sum_optional_float(cost_parts)

    if last_content is None:
        return finalization.finalize_without_output(
            run_dir, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
            subscription_calls=subscription_calls, completed=0, failed=failed
        )

    if passed:
        return finalization.finalize(
            run_dir, last_content, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
            subscription_calls=subscription_calls,
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
    return finalization.finalize(
        run_dir, last_content, errors=errors, latency_ms=latency_ms, cost_usd=cost_usd,
        subscription_calls=subscription_calls,
        completed=generated_count, failed=failed, stage="iterative_refinement_partial",
        content_prefix="(partial) ",
        success_summary=(
            f"rubric을 통과하지 못한 채 루프 종료 — 마지막 생성 결과를 partial로 승격"
            f" (실행 {generated_count}라운드)"
        ),
    )


def _run_agentic_task(
    task: TaskInput, providers: dict[str, Provider], run_dir: Path, budget: BudgetTracker
) -> Observation:
    """자율 에이전트에게 작업을 맡기고, 그 실행을 감싸서 기록한다 (ADR 0007).

    다른 세 패턴과 근본적으로 다른 점: 여기서는 하네스가 루프를 돌리지 않는다.
    에이전트가 스스로 도구를 호출하며 진행하고, 하네스는 경계(작업공간/도구/턴
    상한 — provider가 CLI 인자로 강제)와 사후 검증(실제 산출물 확인, Safety,
    기록)을 맡는다. 이 패턴은 planner가 risk_level="high"를 강제하므로 여기
    도달했다는 건 이미 사람 승인을 통과했다는 뜻이다.

    최종 출력은 에이전트의 요약 + 실제로 만들어진 파일 목록이다. 진짜 산출물은
    텍스트가 아니라 `artifacts/agent_workspace/` 안의 파일이고, final.md는 그
    작업에 대한 보고서 역할을 한다.
    """
    agent_provider = providers.get(AGENT_PROVIDER_KEY)
    if agent_provider is None:
        raise ValueError(
            f"agentic_task에는 에이전트 provider가 필요하다 "
            f"(providers[{AGENT_PROVIDER_KEY!r}]에 등록, ADR 0007 참고)"
        )

    if budget.exhausted:
        # 에이전트는 턴 상한까지 여러 번 호출하므로 시작 자체를 막는다 — 중간에
        # 끊으면 파일을 쓰다 만 상태가 남는다.
        return finalization.finalize_without_output(
            run_dir,
            errors=[{"kind": "budget", "stage": "agentic_task", "message": budget.reason}],
            latency_ms=None,
            cost_usd=None,
            subscription_calls=0,
            completed=0,
            failed=1,
        )

    try:
        result = agent_runner.run_agent_task(
            agent_provider,
            task.prompt,
            run_dir,
            max_turns=MAX_AGENT_TURNS,
            system_prompt_append=AGENT_SYSTEM_PROMPT,
        )
    except Exception as exc:  # noqa: BLE001 - provider 구현체마다 예외 타입이 다를 수 있음
        # 에이전트가 시작조차 못 한 경우(바이너리 없음/타임아웃 등). 다른 패턴의
        # "재시도까지 실패" 경로와 달리 재시도하지 않는다 — 부분적으로 파일을
        # 쓰다 만 상태에서 처음부터 다시 실행하면 같은 작업을 두 번 하게 된다.
        return finalization.finalize_without_output(
            run_dir,
            errors=[{"stage": "agentic_task", "message": f"에이전트 실행 실패: {exc}"}],
            latency_ms=None,
            cost_usd=None,
            completed=0,
            failed=1,
        )

    errors: list[dict[str, str]] = []
    if result.blocked_tool_uses:
        # 경계가 막아낸 시도는 run 실패가 아니지만(정상 방어) 반드시 기록에 남긴다 —
        # 에이전트가 반복적으로 경계 밖을 노리는 패턴은 사람이 봐야 할 신호다.
        blocked = ", ".join(sorted({use.tool for use in result.blocked_tool_uses}))
        errors.append(
            {
                "stage": "agentic_task",
                "message": f"안전 경계가 차단한 도구 사용 {len(result.blocked_tool_uses)}건: {blocked}",
            }
        )
    if result.stop_reason == "error":
        errors.append({"stage": "agentic_task", "message": f"에이전트가 오류로 종료함: {result.final_text[:200]}"})
    elif result.stop_reason == "max_turns":
        errors.append(
            {
                "stage": "agentic_task",
                "message": f"턴 상한({MAX_AGENT_TURNS}회) 도달로 중단 — 그때까지 만든 파일만 남음",
            }
        )

    if not result.produced_files and result.stop_reason != "completed":
        # 남긴 것도 없고 정상 종료도 아니면 승격할 결과 자체가 없다
        # (체인이 첫 단계부터 실패한 경우와 같은 처리).
        return finalization.finalize_without_output(
            run_dir, errors=errors, latency_ms=result.latency_ms, cost_usd=result.cost_usd,
            subscription_calls=result.subscription_calls, completed=0, failed=1
        )

    workspace = agent_runner.agent_workspace(run_dir)
    # 생성된 파일 내용도 Safety 스캔 대상에 넣는다 — 이 패턴의 진짜 산출물은
    # final.md 텍스트가 아니라 파일이라, 파일을 안 보면 Safety가 사실상 무력해진다.
    produced_texts = agent_runner.read_produced_texts(workspace, result.produced_files)

    is_partial = result.stop_reason != "completed"
    return finalization.finalize(
        run_dir,
        _render_agent_report(result),
        errors=errors,
        latency_ms=result.latency_ms,
        cost_usd=result.cost_usd,
        subscription_calls=result.subscription_calls,
        completed=len(result.produced_files),
        failed=1 if is_partial else 0,
        stage="agentic_task_partial" if is_partial else "agentic_task",
        content_prefix="(partial) " if is_partial else "",
        success_summary=(
            f"에이전트가 {len(result.turns)}턴 동안 파일 {len(result.produced_files)}개 생성"
            + (f" (중단 사유: {result.stop_reason})" if is_partial else "")
        ),
        extra_scan_texts=produced_texts,
    )


def _render_agent_report(result) -> str:
    """final.md에 들어갈 보고서 — 에이전트 요약 + 실제 산출물/행동 기록 요약.

    파일 본문은 넣지 않는다(진짜 산출물은 워크스페이스에 있고, 여기 복사하면
    같은 내용이 두 곳에 생겨 어느 쪽이 정본인지 흐려진다).
    """
    file_lines = "\n".join(f"- `{name}`" for name in result.produced_files) or "- (생성된 파일 없음)"
    tool_counts: dict[str, int] = {}
    for turn in result.turns:
        for use in turn.tool_uses:
            tool_counts[use.tool] = tool_counts.get(use.tool, 0) + 1
    tool_summary = ", ".join(f"{tool} {count}회" for tool, count in sorted(tool_counts.items())) or "도구 사용 없음"
    blocked_summary = (
        ", ".join(sorted({use.tool for use in result.blocked_tool_uses}))
        if result.blocked_tool_uses
        else "없음"
    )

    return (
        f"{result.final_text.strip()}\n\n"
        "---\n\n"
        "## 생성된 파일 (artifacts/agent_workspace/)\n"
        f"{file_lines}\n\n"
        "## 실행 요약\n"
        f"- 턴 수: {result.num_turns if result.num_turns is not None else len(result.turns)}\n"
        f"- 도구 사용: {tool_summary}\n"
        f"- 안전 경계가 차단한 시도: {blocked_summary}\n"
        f"- 종료 사유: {result.stop_reason}\n"
        "- 턴별 상세: `agent_turns.json`\n"
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












def _apply_learned_notes(task: TaskInput, run_dir: Path) -> TaskInput:
    """사람이 쓴 `learned.md`가 있으면 프롬프트에 참고 자료로 붙인다 (2026-07-29).

    **파일이 없으면 아무것도 하지 않는다** — 자동으로 축적된 관측
    (`learned/observations.jsonl`)은 여기로 들어오지 않는다. 사람이 그것을 읽고
    판단해서 `learned.md`에 쓴 것만 반영된다("기록은 자동, 반영은 명시적").

    주입한 내용은 run 안에 복사해둔다. `learned.md`는 시간이 지나며 바뀌므로,
    기록이 없으면 "그때 무엇을 학습한 상태였나"를 몰라 run을 다시 해석할 수 없다.
    """
    if not USE_LEARNED_NOTES:
        return task
    notes = learning.load_notes()
    if not notes:
        return task

    run_store.write_markdown(run_dir, learning.INJECTED_FILENAME, notes)
    return task.model_copy(update={"prompt": learning.apply_to_prompt(task.prompt, notes)})


def _write_plan(run_dir: Path, plan: Plan) -> None:
    run_store.write_json(run_dir, "plan.json", plan.model_dump(mode="json"))
    run_store.write_markdown(
        run_dir, "plan.md", f"# Plan\n\n```json\n{run_store.json_dumps(plan.model_dump(mode='json'))}\n```\n"
    )




