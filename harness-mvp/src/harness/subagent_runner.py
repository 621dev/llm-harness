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

**역할별 지시문 스코핑** (2026-07-27, server-engineering-learning 도메인 실제 e2e
검증 중 발견): 원래 각 스텝에 그대로 넘어가던 입력(첫 스텝은 `task.prompt` 원문, 이후
스텝은 이전 스텝 전체 출력)에는 "역할이 무엇인지" 정보가 전혀 없었다. 그래서 task
프롬프트가 "리서치해줘 ... 그 다음 학습 자료 초안을 만들고 검토해줘"처럼 여러 역할의
작업을 한 문장에 묶어서 쓰면, 첫 스텝(research)이 "너는 research만 해"라는 신호를
못 받고 프롬프트 전체(초안 작성+검토까지)를 자기 혼자 다 하려고 시도했다 — 실제로
codex CLI가 이 통짜 지시문을 통째로 처리하려다 120초 타임아웃을 반복해서 발견했다.
`_apply_role_instruction()`이 스텝마다 "당신의 역할은 X, 나머지는 다음 담당자가
한다"는 문장을 입력 앞에 덧붙여 스코핑한다. 역할 이름을 하드코딩하지 않고 스텝
위치(첫 스텝 vs 이어받는 스텝)로만 템플릿을 고르므로, 어떤 역할 이름이 오든(planner의
`_DEFAULT_DELEGATION_ROLES`에 없는 커스텀 역할이라도) 동일하게 적용된다.
"""
from __future__ import annotations

from pathlib import Path

from providers.base import Provider

from typing import Optional

from . import run_store
from .budget import BudgetTracker
from .model_runner import generate_with_retry
from .schemas import Candidate, DelegationStep, Observation

_SUMMARY_PREVIEW_CHARS = 120

# 체인의 첫 스텝은 원본 task.prompt(사람이 쓴 통짜 요청)를 받고, 이후 스텝은 이전
# 스텝의 전체 출력을 받는다 — 받는 내용의 성격이 다르므로 지시문도 둘로 나눈다.
#
# "파일을 만들거나 승인을 요청하지 말고..." 문구는 2026-07-27 content_finalization
# 역할 도입 직후 실제로 겪고 추가함: claude CLI(`ClaudeCliProvider`, 실제로는 Claude
# Code 자체)가 "최종 산출물을 완성했습니다 (linux-basics.md)... 게시를 승인해
# 주시면..." 같은 **작업 보고문**만 응답으로 내고, 완성된 문서 본문 자체는 응답
# 텍스트에 담지 않은 걸 실측으로 확인했다(호출이 격리된 임시 디렉터리에서 실행돼
# 실제 파일이 생기진 않았지만, 원하던 결과물도 응답에 안 남아 무용해짐). Claude
# Code는 본질적으로 파일/승인 흐름에 익숙한 코딩 에이전트라 텍스트 완성 요청에도
# 그 습성이 새어 나온 것으로 보임 — 모든 역할에 공통으로 "행동 대신 완성된 결과물
# 텍스트 자체를 달라"고 명시해 방지한다.
_NO_TOOL_USE_INSTRUCTION = (
    "파일을 만들거나 승인을 요청하지 마세요 — 결과물 전체를 이 응답 텍스트 자체로 "
    "직접 작성하세요."
)
_FIRST_STEP_INSTRUCTION_TEMPLATE = (
    "당신의 역할은 '{role}'입니다. 아래 요청 중 당신의 역할에 해당하는 작업만 "
    "수행하세요 — 이후 단계는 다른 담당자가 이어받아 처리하니 여기서 전부 끝내려고 "
    "하지 마세요. " + _NO_TOOL_USE_INSTRUCTION + "\n\n요청:\n{content}"
)
_CONTINUATION_INSTRUCTION_TEMPLATE = (
    "당신의 역할은 '{role}'입니다. 아래는 이전 단계 결과물입니다. 당신의 역할에 "
    "맞게 이어서 작업하세요. " + _NO_TOOL_USE_INSTRUCTION + "\n\n이전 단계 결과:\n{content}"
)


def _apply_role_instruction(role: str, content: str, *, is_first_step: bool) -> str:
    template = _FIRST_STEP_INSTRUCTION_TEMPLATE if is_first_step else _CONTINUATION_INSTRUCTION_TEMPLATE
    return template.format(role=role, content=content)


def delegate(
    step: DelegationStep,
    provider: Provider,
    input_text: str,
    run_dir: Path,
    step_index: int,
    *,
    budget: Optional[BudgetTracker] = None,
) -> Observation:
    """위임 단계 하나를 실행한다.

    성공/실패와 무관하게 provider의 전체 응답은 파일로만 저장하고, 반환하는
    Observation에는 요약과 파일 경로만 담는다. 실행 결과(성공/실패, 파일 경로)는
    `step`(DelegationStep)에도 그대로 반영한다 — Plan.delegation_chain에 들어있던
    step 객체를 그 자리에서 갱신해, 체인 전체의 진행 상황을 plan만 보고도 알 수 있게 한다.
    """
    step.input_ref = _preview(input_text)

    provider_input = _apply_role_instruction(step.role, input_text, is_first_step=step_index == 1)
    candidate = generate_with_retry(provider, provider_input, budget=budget)
    output_ref = f"artifacts/chain/step-{step_index}-{step.role}.md"
    run_store.write_markdown(run_dir, output_ref, _render_step_markdown(step, candidate))
    step.output_ref = output_ref
    step.latency_ms = candidate.latency_ms
    step.cost_usd = candidate.cost_usd
    step.subscription_calls = candidate.subscription_calls

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
    *,
    budget: Optional[BudgetTracker] = None,
) -> tuple[list[Observation], bool]:
    """delegation_chain을 순서대로 실행한다 (역할별 provider에 순차 위임).

    첫 스텝은 원본 요청(`initial_input`)을 그대로 받는다. 두 번째 스텝부터는
    **원본 요청 + 지금까지의 모든 스텝 결과를 누적한 히스토리**를 받는다 —
    바로 직전 스텝 결과만 넘기던 이전 방식은 2026-07-27 content_finalization
    역할을 3번째 스텝으로 추가하면서 문제가 됐다: content_finalization이
    design_review의 비평만 보고 research의 원본 초안을 못 보면, 비평이 가리키는
    원문이 뭔지 모른 채 최종 산출물을 써야 하는 모순이 생긴다. 누적 히스토리로
    바꿔서 몇 단계든 이전 결과 전부를 볼 수 있게 했다(2단계 체인의 두 번째
    스텝도 이제 원본 요청+첫 스텝 결과를 함께 받는데, 원래도 원본 요청 문맥이
    있는 편이 나으므로 회귀는 아님).

    한 스텝이 실패하면 그 시점에서 체인을 중단한다.

    반환값: (지금까지의 Observation 목록, 체인이 끝까지 완주했는지 여부).
    완주하지 못했다면(False) 마지막 Observation이 실패한 스텝을 가리키고, 그 뒤
    스텝들은 아예 실행되지 않는다.
    """
    observations: list[Observation] = []
    history: list[str] = [f"[원본 요청]\n{initial_input}"]

    for index, step in enumerate(steps, start=1):
        provider = providers[step.provider_id]
        # 첫 스텝만 원본 요청을 그대로(래핑 없이) 받는다 — 히스토리 헤더가 안 붙어야
        # delegate()의 input_ref 미리보기가 원본 텍스트 그대로 남는다(디버깅용).
        step_input = initial_input if index == 1 else "\n\n".join(history)
        obs = delegate(step, provider, step_input, run_dir, index, budget=budget)
        observations.append(obs)

        if obs.status == "error":
            return observations, False  # 체인 중단, 나머지 스텝은 실행하지 않음

        # 다음 스텝을 위해 히스토리에 이번 스텝 결과를 추가한다. **본문만** 넣는다 —
        # 스텝 파일을 통째로 넣으면 디버깅용 헤더("- status: success / - tokens: 43 /
        # - latency_ms: 10")까지 다음 모델의 프롬프트에 들어간다 — 토큰 낭비이자
        # "이 status가 내가 판단할 대상인가?" 같은 혼선 요인이다
        # (2026-07-28 체인 최종 산출물 구성을 고치다 테스트가 잡아냄).
        history.append(f"[{index}단계: {step.role} 결과]\n{read_step_content(run_dir, step)}")

    return observations, True


def _preview(text: str) -> str:
    """대용량 mock 출력을 짧은 미리보기로 압축한다 (실제 요약 로직은 이후 Phase 개선 대상)."""
    flattened = " ".join(text.split())
    if len(flattened) <= _SUMMARY_PREVIEW_CHARS:
        return flattened
    return f"{flattened[:_SUMMARY_PREVIEW_CHARS]}..."


def _render_step_markdown(step: DelegationStep, candidate: Candidate) -> str:
    # model_id를 남기는 이유(2026-07-27, QuotaFallbackProvider 도입과 함께 추가):
    # step.provider_id는 항상 고정된 "{role}-mock"이라 실제로 어떤 모델이 응답했는지
    # 안 보인다 — quota 폴백이 조용히 다른 모델로 전환해도 흔적이 안 남으면 "실패를
    # 숨기지 않는다"는 원칙이 무색해진다. model_id는 candidate가 실제로 그 모델에서
    # 왔음을 보여주는 유일한 필드다.
    return (
        f"# Chain Step {step.role} ({step.provider_id})\n\n"
        f"- model_id: {candidate.model_id}\n"
        f"- status: {candidate.status}\n"
        f"- tokens: {candidate.tokens}\n"
        f"- latency_ms: {candidate.latency_ms}\n\n"
        f"{candidate.content}\n"
    )


# 스텝 파일의 메타데이터 블록 마지막 줄. 본문은 그 뒤 빈 줄 다음부터다.
_STEP_META_LAST_FIELD = "\n- latency_ms: "


def read_step_content(run_dir: Path, step: DelegationStep) -> str:
    """스텝 산출물에서 메타데이터 헤더를 뺀 **본문만** 돌려준다.

    스텝 파일은 디버깅용이라 제목/status/tokens/latency 헤더를 달고 있는데
    (`_render_chain_step_markdown`), 이걸 그대로 최종 산출물에 넣으면 사용자가
    보는 final.md가 "# Chain Step design_review (design_review-mock) - status:
    success - tokens: 1661..."로 시작한다(2026-07-28 측정에서 실제로 관측).
    발행물에 내부 메타데이터가 섞이지 않게 여기서 걷어낸다.

    형식이 다르면(이 헤더 도입 이전 run 등) 통째로 돌려준다 — 본문을 잃느니
    메타데이터가 섞이는 편이 낫다.
    """
    raw = run_store.read_markdown(run_dir, step.output_ref)
    meta_end = raw.find(_STEP_META_LAST_FIELD)
    if meta_end == -1:
        return raw.strip()
    body_start = raw.find("\n\n", meta_end)
    return raw[body_start + 2 :].strip() if body_start != -1 else raw.strip()
