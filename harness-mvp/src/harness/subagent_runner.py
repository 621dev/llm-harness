"""Subagent Runner: Hierarchical Delegation 패턴 실행 (Step 3).

harness-implementation-plan-ko.md Section 4(Action/Observation Contract),
Section 6(복구 전략), Section 7 Step 3을 구현한다.

`delegate()`는 체인 한 단계를 실행한다. provider 호출 결과(전체 내용)는
artifacts/chain/step-N-role.md 로만 저장하고, 호출부(메인 Orchestrator)에는 요약된
Observation(summary + output_ref)만 반환한다 — 이게 gaebalai/claude-code-orchestrator에서
가져온 "컨텍스트 격리" 시뮬레이션이다. 다만 이 격리는 "메인 Orchestrator에게 무엇을
보여주는가"의 문제이지 체인 내부 단계 간 정보 전달을 막는 게 아니라서, `run_chain()`이
다음 단계의 입력을 만들 때는 저장된 파일에서 이전 단계의 전체 내용을 다시 읽어 넘긴다.

`run_chain()`은 `plan.delegation_chain`을 순서대로 실행하는 다단계 체인이다. 한 단계가
재시도까지 실패하면 체인을 중단하고, 아직 실행하지 않은 나머지 단계는 건드리지 않는다
(Section 6: "체인 중단 시 마지막으로 성공한 스텝 결과를 partial로 승격, ask_user" —
실제 partial 승격/ask_user 연결은 Step 8 orchestrator 완성 단계에서 다룬다).
"""
from __future__ import annotations

from pathlib import Path

from providers.base import Provider

from . import run_store
from .model_runner import generate_with_retry
from .schemas import Candidate, DelegationStep, Observation

_SUMMARY_PREVIEW_CHARS = 120


def delegate(
    step: DelegationStep,
    provider: Provider,
    input_text: str,
    run_dir: Path,
    step_index: int,
) -> Observation:
    """위임 단계 하나를 실행한다.

    성공/실패와 무관하게 provider의 전체 응답은 파일로만 저장하고, 반환하는
    Observation에는 요약과 파일 경로만 담는다. 실행 결과(성공/실패, 파일 경로)는
    `step`(DelegationStep)에도 그대로 반영한다 — Plan.delegation_chain에 들어있던
    step 객체를 그 자리에서 갱신해, 체인 전체의 진행 상황을 plan만 보고도 알 수 있게 한다.
    """
    step.input_ref = _preview(input_text)

    candidate = generate_with_retry(provider, input_text)
    output_ref = f"artifacts/chain/step-{step_index}-{step.role}.md"
    run_store.write_markdown(run_dir, output_ref, _render_step_markdown(step, candidate))
    step.output_ref = output_ref
    step.latency_ms = candidate.latency_ms
    step.cost_usd = candidate.cost_usd

    if candidate.status == "error":
        step.status = "error"
        return Observation(
            status="error",
            summary=f"[{step.role}] 실패: {candidate.content}",
            artifacts=[output_ref],
            next_actions=["ask_user"],
        )

    step.status = "success"
    return Observation(
        status="success",
        summary=f"[{step.role}] {_preview(candidate.content)}",
        artifacts=[output_ref],
        next_actions=["continue"],
    )


def run_chain(
    steps: list[DelegationStep],
    providers: dict[str, Provider],
    initial_input: str,
    run_dir: Path,
) -> tuple[list[Observation], bool]:
    """delegation_chain을 순서대로 실행한다 (역할별 provider에 순차 위임).

    이전 스텝의 output_ref 파일에서 전체 내용을 다시 읽어 다음 스텝의 입력으로 넘긴다.
    한 스텝이 실패하면 그 시점에서 체인을 중단한다.

    반환값: (지금까지의 Observation 목록, 체인이 끝까지 완주했는지 여부).
    완주하지 못했다면(False) 마지막 Observation이 실패한 스텝을 가리키고, 그 뒤
    스텝들은 아예 실행되지 않는다.
    """
    observations: list[Observation] = []
    current_input = initial_input

    for index, step in enumerate(steps, start=1):
        provider = providers[step.provider_id]
        obs = delegate(step, provider, current_input, run_dir, index)
        observations.append(obs)

        if obs.status == "error":
            return observations, False  # 체인 중단, 나머지 스텝은 실행하지 않음

        current_input = run_store.read_markdown(run_dir, step.output_ref)

    return observations, True


def _preview(text: str) -> str:
    """대용량 mock 출력을 짧은 미리보기로 압축한다 (실제 요약 로직은 이후 Phase 개선 대상)."""
    flattened = " ".join(text.split())
    if len(flattened) <= _SUMMARY_PREVIEW_CHARS:
        return flattened
    return f"{flattened[:_SUMMARY_PREVIEW_CHARS]}..."


def _render_step_markdown(step: DelegationStep, candidate: Candidate) -> str:
    return (
        f"# Chain Step {step.role} ({step.provider_id})\n\n"
        f"- status: {candidate.status}\n"
        f"- tokens: {candidate.tokens}\n"
        f"- latency_ms: {candidate.latency_ms}\n\n"
        f"{candidate.content}\n"
    )
