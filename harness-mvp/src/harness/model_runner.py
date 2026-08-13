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

from concurrent import futures
from pathlib import Path
from typing import Optional

from providers.base import Provider, ProviderError

from . import run_store
from .budget import BudgetTracker
from .schemas import Candidate

MAX_RETRIES = 1  # Section 6 복구 전략: 1회 재시도, 상한 고정(무한 재시도 금지)

# fan-out 후보를 동시에 몇 개까지 만들 것인가 (2026-08-13). 1이면 지금까지처럼 순차.
#
# **왜 병렬로 바꿨나**: fan-out 후보는 **정의상 서로 독립**이다 — 같은 프롬프트를 받고,
# 서로의 결과를 보지 않는다(누적 전달을 하는 체인과 정반대다). 그런데 구현이 순차라
# 실측 지연이 direct의 4.5배였다: fan_out 238초 vs direct 53초(측정 15회/27회 평균).
# 판정 호출은 run 지연의 6%뿐이라, 남은 시간은 거의 전부 "다음 후보를 기다리는 시간"이다.
#
# **토큰은 1비트도 안 줄어든다.** 보내는 프롬프트와 받는 응답이 그대로이므로 비용도
# 동일하고, 줄어드는 건 벽시계 시간뿐이다. 그래서 이건 비용 절감이 아니라 대기 시간
# 절감이고, 품질/판정에 영향이 없다.
#
# **호출 한도가 한곳에 몰리지 않는다**: fan-out은 서로 다른 provider를 쓰므로(같은
# 모델 두 개를 후보로 두지 않는다) 동시 호출이 한 API의 rate limit에 겹치지 않는다.
# 그래도 겹치는 구성을 만들 수 있으니, 1로 두면 예전 동작으로 완전히 돌아간다.
MAX_PARALLEL_CANDIDATES = 4


def run_all(
    prompt: str,
    providers: list[Provider],
    run_dir: Path,
    *,
    temperature: float = 0.7,
    budget: Optional[BudgetTracker] = None,
) -> list[Candidate]:
    """등록된 provider를 순회하며 독립적으로 후보를 생성한다 (fan_out_judge 전용).

    후보 수는 providers 길이로 결정된다 (Planner가 아직 없어 지금은 호출부가 provider
    목록을 직접 구성한다 — Step 4에서 plan.num_candidates 기반으로 자동 구성될 예정).

    예산 상한에 걸리면 **남은 provider는 호출하지 않고 루프를 끝낸다**(2026-07-29).
    상한 도달 후에도 계속 돌면서 provider마다 실패 후보를 만들면 errors.json이 같은
    이유로 도배된다 — 첫 한 건만 남기고 멈추는 게 읽기 쉽다.

    후보는 서로 독립이라 기본적으로 병렬로 만든다(`MAX_PARALLEL_CANDIDATES`). **단
    예산 상한이 걸려 있으면 순차로 돈다** — 이유는 `_worker_count` 참고. 어느 경로든
    반환 순서는 `providers` 순서와 같다(재현성).
    """
    workers = _worker_count(providers, budget)
    if workers == 1:
        return _generate_sequentially(prompt, providers, run_dir, temperature=temperature, budget=budget)
    return _generate_in_parallel(
        prompt, providers, run_dir, workers=workers, temperature=temperature, budget=budget
    )


def _worker_count(providers: list[Provider], budget: Optional[BudgetTracker]) -> int:
    """후보를 몇 개 동시에 만들 것인가. 1이면 순차 실행이다.

    **예산 상한이 걸려 있으면 무조건 1이다.** 상한이 가진 힘은 "다음 호출을 시작하지
    않는 것" 하나뿐인데(`budget.py` 첫머리), 병렬로 던지면 그 다음 호출들이 **이미
    날아간 뒤**라 막을 대상이 없어진다. 상한 $0.02에 후보 4개를 동시에 던지면 4개
    값을 다 쓰고 나서 "상한에 걸렸다"고 보고하는 셈이다 — 금액이 걸린 안전장치를
    지연 단축과 바꾸지 않는다.

    상한이 없으면(`unlimited`) `exhausted`가 영원히 False라 순차/병렬의 호출 수가
    정확히 같다. 그래서 이 조건에서만 병렬로 간다.
    """
    if MAX_PARALLEL_CANDIDATES <= 1 or len(providers) < 2:
        return 1
    if budget is not None and not budget.unlimited:
        return 1
    return min(len(providers), MAX_PARALLEL_CANDIDATES)


def _generate_sequentially(
    prompt: str,
    providers: list[Provider],
    run_dir: Path,
    *,
    temperature: float,
    budget: Optional[BudgetTracker],
) -> list[Candidate]:
    candidates: list[Candidate] = []
    for provider in providers:
        if budget is not None and budget.exhausted:
            break
        candidate = generate_with_retry(provider, prompt, temperature=temperature, budget=budget)
        candidates.append(candidate)
        _write_candidate(run_dir, candidate)
    return candidates


def _generate_in_parallel(
    prompt: str,
    providers: list[Provider],
    run_dir: Path,
    *,
    workers: int,
    temperature: float,
    budget: Optional[BudgetTracker],
) -> list[Candidate]:
    """후보를 동시에 만든다. 여기 오는 건 상한이 없는 경우뿐이라 중간 중단이 없다.

    provider 호출은 subprocess(구독 CLI) 또는 HTTP(종량제)라 둘 다 GIL을 놓는다 —
    프로세스를 띄우지 않고 스레드로 충분한 이유다.

    **파일은 메인 스레드에서만 쓴다.** 완료 순서대로 바로 남겨서(`as_completed`) 도중에
    죽어도 그때까지 만든 후보가 디스크에 남는 건 순차 때와 같다. 반환은 provider
    순서로 다시 맞춘다 — 완료 순서는 실행마다 달라지므로 그걸 그대로 돌려주면
    산출물이 실행마다 흔들린다.
    """
    done: dict[int, Candidate] = {}
    with futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="candidate") as pool:
        submitted = {
            pool.submit(generate_with_retry, provider, prompt, temperature=temperature, budget=budget): index
            for index, provider in enumerate(providers)
        }
        for future in futures.as_completed(submitted):
            index = submitted[future]
            # generate_with_retry는 provider 실패를 error Candidate로 바꿔 돌려주므로
            # 여기서 예외가 올라오면 provider가 아니라 하네스 결함이다 — 삼키지 않는다.
            candidate = future.result()
            done[index] = candidate
            _write_candidate(run_dir, candidate)
    return [done[index] for index in range(len(providers))]


def _write_candidate(run_dir: Path, candidate: Candidate) -> None:
    run_store.write_markdown(
        run_dir,
        f"artifacts/candidates/{candidate.model_id}.md",
        _render_candidate_markdown(candidate),
    )


def direct_call(
    prompt: str,
    provider: Provider,
    *,
    temperature: float = 0.7,
    budget: Optional[BudgetTracker] = None,
) -> Candidate:
    """적합성 게이트(router.check_fitness) 탈락 시 쓰는 단일 모델 호출 경로 (Section 12.1).

    패턴 분기(fan_out_judge/hierarchical_delegation) 자체를 건너뛰므로 후보 비교나
    체인 위임 없이 provider 하나만 호출한다. 재시도 규칙은 run_all과 동일하다.
    """
    return generate_with_retry(provider, prompt, temperature=temperature, budget=budget)


def generate_with_retry(
    provider: Provider,
    prompt: str,
    *,
    temperature: float = 0.7,
    budget: Optional[BudgetTracker] = None,
) -> Candidate:
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
    if budget is not None and budget.exhausted:
        # 호출을 시작하지 않는다 — 이미 쓴 돈은 되돌릴 수 없으니 상한이 할 수 있는 일은
        # 이것뿐이다. provider 실패와 구분되게 이유를 명시한다(마스킹 금지 원칙).
        return Candidate(
            model_id=provider.model_id,
            content=f"(budget) {budget.reason}",
            tokens=None,
            latency_ms=None,
            cost_usd=None,
            status="error",
            subscription_calls=0,  # 호출하지 않았으므로 소모 0
        )

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
            if budget is not None:
                budget.add(candidate)
            return candidate
        except Exception as exc:  # noqa: BLE001 - provider 구현체마다 예외 타입이 다를 수 있음
            errors.append(exc)
            if not _is_retryable(exc):
                break

    failed = Candidate(
        model_id=provider.model_id,
        content=f"(error) {_render_errors(errors)}",
        tokens=None,
        latency_ms=None,
        cost_usd=None,
        status="error",
        # 끝내 실패했어도 시도한 만큼 구독 한도는 이미 소모됐다.
        subscription_calls=attempts if _is_subscription(provider) else 0,
    )
    if budget is not None:
        budget.add(failed)  # 실패한 시도도 한도를 깎았으므로 예산에서도 빠져야 한다
    return failed


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
        # 입력 토큰도 남긴다(2026-07-29). `Candidate.input_tokens`를 추가할 때 체인 스텝
        # 파일에만 반영하고 여기를 빼먹어서, fan_out의 입력 규모를 산출물에서 읽을 수
        # 없었다 — 같은 정보인데 패턴에 따라 있고 없는 건 비대칭이다(측정 스크립트
        # mock 검증이 잡았다).
        f"- input_tokens: {candidate.input_tokens}\n"
        f"- latency_ms: {candidate.latency_ms}\n"
        f"- cost_usd: {candidate.cost_usd}\n\n"
        f"{candidate.content}\n"
    )
