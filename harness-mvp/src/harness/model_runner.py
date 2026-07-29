"""Model Runner: Fan-out/Judge 패턴의 독립 후보 생성 (Step 2).

harness-implementation-plan-ko.md Section 4(Action/Observation Contract),
Section 6(복구 전략), Section 7 Step 2를 구현한다.

run_all()은 여러 provider를 독립적으로 호출해 Candidate 목록을 만들고, 각 후보를
artifacts/candidates/<model_id>.md로 저장한다. provider 호출이 실패하면 1회
재시도(Section 6: "무한 재시도 금지")하고, 그래도 실패하면 status="error" Candidate를
기록한 뒤 나머지 provider로 계속 진행한다. 몇 개 이상 성공해야 run을 유효하다고 볼지
(min_candidates)는 이 모듈이 아니라 orchestrator/recovery 쪽 책임이다 — model_runner는
"후보를 만든다"만 담당한다.
"""
from __future__ import annotations

from pathlib import Path

from providers.base import Provider, ProviderError

from . import run_store
from .schemas import Candidate

MAX_RETRIES = 1  # Section 6 복구 전략: 1회 재시도, 상한 고정(무한 재시도 금지)


def run_all(
    prompt: str,
    providers: list[Provider],
    run_dir: Path,
    *,
    temperature: float = 0.7,
) -> list[Candidate]:
    """등록된 provider를 순회하며 독립적으로 후보를 생성한다 (fan_out_judge 전용).

    후보 수는 providers 길이로 결정된다 (Planner가 아직 없어 지금은 호출부가 provider
    목록을 직접 구성한다 — Step 4에서 plan.num_candidates 기반으로 자동 구성될 예정).
    """
    candidates: list[Candidate] = []
    for provider in providers:
        candidate = generate_with_retry(provider, prompt, temperature=temperature)
        candidates.append(candidate)
        run_store.write_markdown(
            run_dir,
            f"artifacts/candidates/{candidate.model_id}.md",
            _render_candidate_markdown(candidate),
        )
    return candidates


def direct_call(prompt: str, provider: Provider, *, temperature: float = 0.7) -> Candidate:
    """적합성 게이트(router.check_fitness) 탈락 시 쓰는 단일 모델 호출 경로 (Section 12.1).

    패턴 분기(fan_out_judge/hierarchical_delegation) 자체를 건너뛰므로 후보 비교나
    체인 위임 없이 provider 하나만 호출한다. 재시도 규칙은 run_all과 동일하다.
    """
    return generate_with_retry(provider, prompt, temperature=temperature)


def generate_with_retry(provider: Provider, prompt: str, *, temperature: float = 0.7) -> Candidate:
    """1회 재시도(Section 6, 상한 고정) 후에도 실패하면 status="error" Candidate를 반환한다.

    fan_out_judge(run_all)와 hierarchical_delegation(subagent_runner.delegate) 양쪽이
    공유하는 공통 복구 로직이라 이 모듈에 두고 재사용한다 — "1회 재시도, 무한 재시도
    금지"는 패턴과 무관한 공통 계약이기 때문이다.

    **재시도가 무의미한 실패는 재시도하지 않는다**(2026-07-29, ECC 재분석에서 확인한
    결함 수정). 한도 초과(429)에 재시도를 얹으면 이미 소진된 한도에 호출을 한 번 더
    던지고, 인증 실패에 재시도를 얹으면 지연만 2배가 된다. 판정은 발생 지점이 표시한
    `ProviderError.is_retryable`만 본다 — 여기서 에러 메시지를 다시 해석하지 않는다.

    시도별 오류는 **전부** 남긴다. 예전엔 `last_error`로 덮어써서 1차 실패 원인이
    사라졌는데, 1차가 한도 초과이고 2차가 파싱 오류면 최종 기록에 한도 얘기가 아예
    안 나와 원인 추적이 끊긴다.
    """
    errors: list[Exception] = []
    attempts = 0
    for _attempt in range(MAX_RETRIES + 1):
        attempts += 1
        try:
            candidate = provider.generate(prompt, temperature=temperature)
            # 구독 호출은 cost_usd가 None이라 비용 지표에 안 잡히므로 횟수로 남긴다
            # (Section 9 Cost Blindness 방지). 실패한 시도도 한도를 소모하니 attempts를
            # 그대로 쓴다 — 여기가 모든 generate() 호출이 지나는 유일한 지점이라
            # 재시도까지 정확히 셀 수 있는 자리다.
            candidate.subscription_calls = attempts if _is_subscription(provider) else 0
            return candidate
        except Exception as exc:  # noqa: BLE001 - provider 구현체마다 예외 타입이 다를 수 있음
            errors.append(exc)
            if not _is_retryable(exc):
                break

    return Candidate(
        model_id=provider.model_id,
        content=f"(error) {_render_errors(errors)}",
        tokens=None,
        latency_ms=None,
        cost_usd=None,
        status="error",
        # 끝내 실패했어도 시도한 만큼 구독 한도는 이미 소모됐다.
        subscription_calls=attempts if _is_subscription(provider) else 0,
    )


def _is_retryable(exc: Exception) -> bool:
    """재시도가 한도/시간만 낭비하는 실패를 걸러낸다.

    `ProviderError`가 아닌 예외는 재시도한다(기존 동작 유지) — provider 계약상 실패는
    `ProviderError`여야 하므로 다른 타입이 올라온 건 이미 이상 상황이고, 판단 근거가
    없는 상태에서 재시도를 없애면 일시적 네트워크 오류가 래핑 없이 새는 경우까지
    같이 잃는다. 상한이 1회라 낭비 폭도 제한된다.
    """
    return exc.is_retryable if isinstance(exc, ProviderError) else True


def _render_errors(errors: list[Exception]) -> str:
    """시도가 여러 번이면 몇 차 시도의 실패인지까지 남긴다."""
    if len(errors) == 1:
        return str(errors[0])
    return " | ".join(f"{index}차: {exc}" for index, exc in enumerate(errors, start=1))


def _is_subscription(provider: Provider) -> bool:
    """auth_mode를 못 읽는 provider 대역(테스트 fake 등)은 구독이 아닌 것으로 본다."""
    return getattr(provider, "auth_mode", None) == "cli_subscription"


def _render_candidate_markdown(candidate: Candidate) -> str:
    return (
        f"# Candidate: {candidate.model_id}\n\n"
        f"- status: {candidate.status}\n"
        f"- tokens: {candidate.tokens}\n"
        f"- latency_ms: {candidate.latency_ms}\n"
        f"- cost_usd: {candidate.cost_usd}\n\n"
        f"{candidate.content}\n"
    )
