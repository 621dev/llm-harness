"""패턴 부가가치 측정: 단일 호출(direct) vs 체인 vs fan_out_judge.

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
  어느 조건의 출력인지 모른다).
- **축이 둘이다**(2026-07-29 추가): rubric 합격률 + **조건 간 blind 정면 비교**.
  합격률 한 축만으로는 3차에서 바닥 효과(전부 탈락), 4차에서 천장 효과(9/9)에
  걸려 **조건이 구분되지 않았다** — 품질 차이가 없다고 입증한 게 아니라 차이를
  잴 수 없는 측정을 두 번 한 것이다. 정면 비교는 천장이 없다(`head_to_head`).
- 실제 Gemini API를 호출하므로 의도적으로 `pytest tests/` 밖에 둔다(작업 규칙:
  자동 테스트는 실제 API/CLI 미호출. `verify_judge_fault_injection.py` 선례).
  로직 자체는 `verify_measure_script.py`가 mock으로 확인한다(비용 0).

사용법 (harness-mvp 디렉토리에서):
  PYTHONPATH=src python scripts/measure_pattern_value.py [--k 3]
  PYTHONPATH=src python scripts/measure_pattern_value.py --conditions direct,fan_out
  PYTHONPATH=src python scripts/measure_pattern_value.py --conditions direct,fan_out --prompt diagnostic
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
from harness.schemas import Candidate, ProviderConfig, TaskInput  # noqa: E402
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

# 프롬프트는 고르는 값이다(2026-07-29). 예전 측정은 `basic` 하나만 썼는데, 5차에서
# **난이도가 측정의 병목**이라는 게 드러났다 — direct가 3/3 만점이라 합격률로는 어떤
# 패턴도 그것을 넘을 수 없고(천장), 정면 비교에서도 3쌍 중 2쌍이 근소한 차였다.
# 쉬운 프롬프트에서는 단발 호출로도 충분해서 **구조의 값이 나타날 자리가 없다.**
#
# 이전 프롬프트를 지우지 않고 남기는 이유: 1~5차 결과가 전부 `basic` 기준이라,
# 지우면 과거 숫자와 새 숫자를 비교할 수 없게 된다.
_PROMPTS: dict[str, str] = {
    # domains/server-engineering-learning의 실제 task(task.networking-basics.json)와
    # 동일한 프롬프트 — 생태적 타당성(실사용 프롬프트로 측정) 확보 목적.
    # 1~5차 측정이 전부 이걸 썼다.
    "basic": (
        "초급 엔지니어가 이해할 수 있도록 서버 네트워킹 기초(방화벽, 포트, DNS)를 "
        "리서치해줘. 그 다음 학습 자료 초안을 만들고 내용을 검토해줘."
    ),
    # 6차용(2026-07-29 신설). 같은 도메인(서버 엔지니어링)을 유지하면서 난이도를 올린다.
    #
    # **어디에 압력을 주는지가 설계의 핵심이다** — rubric은 planner와 일치해야 해서
    # (`MeasurementRubricConsistencyTest`) 바꿀 수 없으므로, 그 두 항목이 실제로
    # 걸리도록 프롬프트를 만든다:
    #   - `핵심 정보 커버리지` ← 다뤄야 할 축을 5개로 못 박는다. 단발 호출이 한두 개를
    #     빠뜨릴 여지가 생긴다(빠뜨리면 판정자가 항목을 지목할 수 있다)
    #   - `구체성` ← 축마다 실행 명령 + 출력 해석 + **배제 조건**까지 요구한다.
    #     "방화벽을 확인하세요" 수준으로는 못 넘어간다
    #
    # 바닥 효과(3차)를 피하려고 두 가지를 지켰다: (1) 웹 접근이 필요한 요구는 없다 —
    # 표준 리눅스 도구 지식만으로 답할 수 있다, (2) 요구가 많을 뿐 각 요구 자체는
    # 평범하다. 3차가 무의미해진 건 요구가 많아서가 아니라 **달성 불가능한 항목**이
    # 있어서였다.
    "diagnostic": (
        "사내 웹 서비스가 특정 시간대에만 간헐적으로 접속 실패한다. 원인을 좁혀 나가는 "
        "진단 절차서를 작성해줘. 방화벽, 포트/리스닝 상태, DNS, 커넥션 한도, TLS 인증서 "
        "만료 다섯 축을 모두 다루고, 축마다 (1) 실행할 구체적인 명령, (2) 그 출력을 "
        "어떻게 읽는지, (3) 어떤 결과가 나오면 그 축은 원인이 아니라고 배제할 수 있는지를 "
        "써라. 간헐적 실패라는 조건에서 한 번의 정상 결과가 왜 무죄 증명이 되지 못하는지도 "
        "짚어줘. 초급 엔지니어가 그대로 따라 실행할 수 있어야 한다."
    ),
}

# 실행 중에 정해진다(main에서 --prompt로 설정). 조건 함수들이 호출 시점에 읽는다.
PROMPT = _PROMPTS["basic"]
# planner._DEFAULT_RUBRICS["research"]와 동일 — 체인 조건에서 planner가 고르는
# rubric을 단일 조건에도 똑같이 적용해 판정 기준을 통일한다.
RUBRIC = ["핵심 정보 커버리지", "구체성"]


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
        return ClaudeCliProvider(config, timeout_sec=SUBSCRIPTION_TIMEOUT_SEC)
    return CodexCliProvider(config, timeout_sec=SUBSCRIPTION_TIMEOUT_SEC)


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


class _LabeledProvider(Provider):
    """같은 백엔드를 여러 후보 슬롯으로 쓸 때 `model_id`만 구분해주는 위임 wrapper.

    `fan_out` 조건은 후보 N개를 **같은 백엔드**로 만든다(구조 효과와 모델 효과 분리 —
    이 스크립트의 기본 원칙). 그런데 엔진은 `Candidate.model_id`를 두 곳에서 식별자로
    쓴다:

    - `artifacts/candidates/{model_id}.md` 파일명 → 같으면 **서로 덮어쓴다**
    - `JudgingScore.candidate`와 `Judging.winner` → 같으면 **누가 이겼는지 구분 불가**

    그래서 슬롯 번호를 붙여 구분한다. 판정은 여전히 blind다 — `judge._build_prompt`는
    레이블(A/B/…)과 본문만 쓰고 `model_id`를 프롬프트에 넣지 않는다(확인 후 사용).

    **`auth_mode`를 반드시 위임한다.** wrapper 자신의 config를 보고하면 구독 provider가
    종량제로 보여서 `_limit_subscription_candidates`와 `subscription_calls` 집계가
    둘 다 조용히 틀린다 — 2026-07-28과 07-29에 각각 다른 자리에서 같은 버그를 냈다.
    """

    def __init__(self, inner: Provider, slot: int) -> None:
        super().__init__(
            ProviderConfig(
                provider_id=f"{inner.provider_id}-c{slot}",
                model_id=f"{inner.model_id}-c{slot}",
                auth_mode=inner.auth_mode,
            )
        )
        self._inner = inner

    @property
    def auth_mode(self) -> str:
        return self._inner.auth_mode

    def generate(self, prompt: str, *, temperature: float = 0.7):
        # **반환된 Candidate의 model_id까지 바꿔야 한다.** model_runner는 provider가
        # 만든 Candidate를 그대로 돌려주므로(`generate_with_retry`), 여기서 갈아주지
        # 않으면 안쪽 provider의 이름이 그대로 남아 위 두 충돌이 그대로 재현된다.
        candidate = self._inner.generate(prompt, temperature=temperature)
        return candidate.model_copy(update={"model_id": self.model_id})


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
        "roles": [],
        # 체인 조건과 같은 키로 남긴다 — direct의 입력 토큰이 **증폭의 기준선**이라,
        # 이게 없으면 "체인이 몇 배 쓰는가"를 계산할 수 없다(2026-07-29 mock 검증에서
        # direct만 이 키가 없어 비교가 불완전한 걸 발견).
        "step_input_tokens": [candidate.input_tokens],
    }


# planner가 만드는 체인의 역할 provider를 전부 등록해야 한다 — 하나라도 빠지면
# subagent_runner가 `providers[step.provider_id]`에서 KeyError로 죽는다.
# content_finalization은 2026-07-28 merge된 PR #64로 research 체인에 3번째
# 역할로 추가됐다(그때 이 스크립트가 실제로 깨졌다). 엔진의 역할 목록을 직접
# 가져와서, 앞으로 역할이 늘어도 여기 손 안 대게 한다.
_CHAIN_ROLE_PROVIDER_IDS = tuple(f"{role}-mock" for role in cli_module._DELEGATION_ROLES)


# 조건 C(부서형 체인)의 역할 구성. 2026-07-29 `delegation_roles:` override로 추가됐다 —
# **역할을 3개에서 5개로 늘리는 것 자체에 값이 있는지**를 보는 조건이다. 3단계 체인이
# 2차 측정에서 direct_call과 동률이었으므로, 여기서도 동률이면 "역할 세분화로는 값이
# 안 나온다"가 되고 DAG 패턴(분업 병렬/분기)을 만들 근거가 사라진다.
DEPARTMENT_ROLES = ("research", "drafting", "compliance_review", "editing", "content_finalization")


def run_chain(attempt: int, root: Path, *, roles: tuple[str, ...] | None = None, label: str = "chain") -> dict:
    """조건 B/C: orchestrator를 통해 hierarchical_delegation 실행(역할 전부 같은 백엔드).

    `roles`를 주면 `delegation_roles:` override로 그 부서 구성을 쓴다(조건 C).
    안 주면 planner의 기본 체인 3단계(조건 B).
    """
    providers: dict = {pid: _generator(pid) for pid in _CHAIN_ROLE_PROVIDER_IDS}
    providers[orchestrator.JUDGE_PROVIDER_KEY] = _evaluator("judge")
    # **패턴을 항상 명시한다.** 예전엔 `roles`가 없으면 constraints를 비워서 planner의
    # 키워드 라우팅에 맡겼는데, ADR 0009(2026-07-29)로 **키워드 라우팅이 전부
    # `fan_out_judge`로 바뀌면서 이 조건이 체인을 재지 않게 됐다** — mock 검증이 그날
    # 바로 잡았다. 안 잡혔으면 `chain` 라벨로 fan_out 숫자를 측정해 체인 결과로
    # 기록했을 것이다. 측정 조건은 라우팅 규칙 변경에 흔들려선 안 된다.
    constraints = ["team_pattern:hierarchical_delegation"]
    if roles:
        constraints.append(f"delegation_roles:{','.join(roles)}")
    task = TaskInput(task_id=f"measure-{label}-{attempt}", prompt=PROMPT, constraints=constraints)
    observation = orchestrator.run(task, providers, root=root)
    run_dir = root / f"run-measure-{label}-{attempt}"
    ok = observation.status in ("success", "warning") and (run_dir / "final.md").exists()
    content = run_store.read_markdown(run_dir, "final.md") if ok else ""
    metrics = run_store.read_json(run_dir, "metrics.json")
    plan = run_store.read_json(run_dir, "plan.json")
    return {
        "condition": label,
        "attempt": attempt,
        "ok": ok,
        "content": content,
        "latency_ms": metrics["latency_ms"],
        "cost_usd": metrics["estimated_cost_usd"],
        "subscription_calls": metrics.get("subscription_calls", 0),
        # 체인 스텝 수 + evaluator 1회. 역할이 늘면 자동으로 반영된다.
        "llm_calls": len(plan["delegation_chain"]) + 1,
        "roles": [step["role"] for step in plan["delegation_chain"]],
        # 스텝별 입력 토큰 (2026-07-29). 체인은 스텝마다 이전 결과를 전부 받아 입력이
        # 계단식으로 커진다 — 어디서 얼마나 커지는지 안 남기면 "요약 전달이 필요한
        # 시점"을 추측으로 판단하게 된다.
        "step_input_tokens": _step_input_tokens(run_dir, plan),
    }


def _step_input_tokens(run_dir: Path, plan: dict) -> list[int | None]:
    """스텝 파일 헤더의 `- input_tokens:` 값을 순서대로 뽑는다.

    **`plan.json`의 `output_ref`를 쓰면 안 된다** — plan은 체인 실행 *전에* 저장되므로
    거기 `output_ref`는 전부 null이다(실행 중 step 객체만 갱신된다). 파일명 규칙으로
    직접 구성한다(`run_store.chain_step_path`와 같은 규칙 — 2026-07-29 mock 검증에서
    전부 None이 나와서 발견했다).

    파싱 실패는 None으로 남기고 측정을 계속한다 — 부가 관측이 측정 자체를 죽이면
    배보다 배꼽이 크다(`learning.record_run`과 같은 판단).
    """
    values: list[int | None] = []
    for index, step in enumerate(plan["delegation_chain"], start=1):
        try:
            text = run_store.read_markdown(run_dir, f"artifacts/chain/step-{index}-{step['role']}.md")
            line = next(ln for ln in text.splitlines() if ln.startswith("- input_tokens:"))
            values.append(int(line.split(":", 1)[1].strip()))
        except (OSError, KeyError, StopIteration, ValueError):
            values.append(None)
    return values


# 조건 D(fan_out_judge)의 후보 수. 2026-07-29에 추가 — 이 패턴은 기계 장치가 가장
# 많은데(judge + synthesizer + blind 익명화 + 구독 후보 상한) 그때까지 **비교 측정
# 0회**였다.
#
# **후보를 전부 같은 백엔드로 만든다** — 다른 조건들과 같은 원칙(구조 효과와 모델
# 효과 분리)이다. 그래서 이 조건이 답하는 질문은 "**여러 모델을 섞는 게 좋은가**"가
# 아니라 "**같은 모델의 후보 N개를 만들어 judge가 고르고 합성하는 게 단발보다
# 나은가**"다. `run_all`이 temperature=0.7로 부르므로 후보는 실제로 서로 다르다.
#
# 모델 다양성 효과는 **별개의 미측정 변수로 남는다** — 운영 config는 claude+codex를
# 섞는데(§8) 이 측정은 그걸 검증하지 않는다. 결과를 인용할 때 반드시 함께 말할 것.
FAN_OUT_CANDIDATES = 2


# 조건 E(모델 다양성)의 후보 백엔드. `--fan-out-models`로 바꾼다.
#
# **이 조건만이 운영 config가 실제로 쓰는 구성을 검증한다.** 5·6차(ADR 0010)는 후보를
# 전부 같은 백엔드로 만들어 "후보 N개 + judge + 합성"의 값까지만 확인했고, `fan_out_judge`
# 라는 이름이 원래 주장하는 **다모델 비교**는 미검증으로 남았다.
#
# 비교 상대는 `direct`가 아니라 **`fan_out`(동일 모델 후보)**이다 — 구조를 고정하고
# 모델 구성만 바꿔야 다양성의 효과가 분리된다. `direct`와 비교하면 구조 효과와 다양성
# 효과가 섞여서 어느 쪽이 기여했는지 말할 수 없다.
MIXED_FAN_OUT_BACKENDS: tuple[str, ...] = ("claude", "codex")

# 구독 CLI 측정용 타임아웃.
#
# 원래는 엔진 기본값(당시 120초)이 측정을 죽여서 여기서만 우회한 값이었다. **2026-08-03에
# 엔진 기본값이 420초로 올라가면서 같은 값이 됐다** — 이 측정이 남긴 실측(8차: 시도당
# 268~432초, 구독 1회당 200초 초과)이 그 인상의 근거였다.
#
# 그래도 명시를 남기는 이유: 측정은 엔진 기본값이 바뀌어도 **같은 조건으로 재현돼야 한다.**
# 기본값에 묶어두면 나중에 누군가 기본값을 낮출 때 과거 측정과 비교가 깨진다.
# (원래 의도도 같았다 — 타임아웃으로 죽은 시도는 데이터가 아니라 잡음이다.)
SUBSCRIPTION_TIMEOUT_SEC = 420.0


def run_fan_out(
    attempt: int,
    root: Path,
    *,
    backends: tuple[str, ...] | None = None,
    label: str = "fan_out",
) -> dict:
    """조건 D/E: orchestrator를 통해 fan_out_judge 실행.

    `backends`를 주면 후보를 그 백엔드들로 만든다(조건 E — 모델 다양성). 안 주면
    `--generator` 백엔드를 후보 수만큼 복제한다(조건 D — 구조 효과만).

    후보 provider를 `_LabeledProvider`로 감싸는 이유는 그쪽 docstring 참고 —
    같은 백엔드를 N개 슬롯으로 쓰면 `model_id`가 겹쳐서 산출물 파일과 승자 판정이
    둘 다 망가진다. 백엔드가 서로 다르면 겹치지 않지만, **조건 간 산출물 이름 규칙을
    같게 유지**하려고 양쪽 다 감싼다.
    """
    slots = backends if backends is not None else (_GENERATOR_BACKEND,) * FAN_OUT_CANDIDATES
    providers: dict = {
        f"cand-{i}": _LabeledProvider(_with_safety_net(backend, f"cand-{i}"), i)
        for i, backend in enumerate(slots, start=1)
    }
    providers[orchestrator.JUDGE_PROVIDER_KEY] = _evaluator("judge")
    # fan_out_judge는 기본 패턴이지만 측정에서는 명시한다 — 라우팅 규칙이 나중에
    # 바뀌어도 이 조건이 조용히 다른 패턴을 재게 되는 일이 없어야 한다.
    task = TaskInput(
        task_id=f"measure-{label}-{attempt}",
        prompt=PROMPT,
        constraints=["team_pattern:fan_out_judge"],
    )
    observation = orchestrator.run(task, providers, root=root)
    run_dir = root / f"run-measure-{label}-{attempt}"
    ok = observation.status in ("success", "warning") and (run_dir / "final.md").exists()
    metrics = run_store.read_json(run_dir, "metrics.json")
    return {
        "condition": label,
        "attempt": attempt,
        "ok": ok,
        "content": run_store.read_markdown(run_dir, "final.md") if ok else "",
        "latency_ms": metrics["latency_ms"],
        "cost_usd": metrics["estimated_cost_usd"],
        "subscription_calls": metrics.get("subscription_calls", 0),
        "llm_calls": len(slots) + 1,  # 후보 N + judge 1
        # 어느 백엔드 조합으로 잰 건지 남긴다 — 다양성 조건은 이게 곧 측정 대상이다.
        "roles": list(slots),
        # 체인과 달리 fan_out은 입력이 **누적되지 않는다** — 후보마다 같은 프롬프트를
        # 독립적으로 받는다. 그 구조 차이가 숫자로 보이도록 같은 키에 담는다.
        "step_input_tokens": _candidate_input_tokens(run_dir),
    }


def _candidate_input_tokens(run_dir: Path) -> list[int | None]:
    """후보 산출물 헤더의 `- input_tokens:`를 전부 뽑는다(파일명은 model_id)."""
    candidates_dir = run_dir / "artifacts" / "candidates"
    if not candidates_dir.is_dir():
        return []
    values: list[int | None] = []
    for path in sorted(candidates_dir.glob("*.md")):
        try:
            line = next(
                ln for ln in path.read_text(encoding="utf-8").splitlines()
                if ln.startswith("- input_tokens:")
            )
            values.append(int(line.split(":", 1)[1].strip()))
        except (OSError, StopIteration, ValueError):
            values.append(None)
    return values


# 측정할 수 있는 조건. 새 조건을 추가하려면 여기와 `_run_condition`만 손대면 된다.
CONDITIONS = ("direct", "chain", "departments", "fan_out", "fan_out_mixed")


# 조건별 시도당 LLM 호출 수(생성 + 판정 1회). 시작 전에 규모를 보여주기 위한 추정이다.
_CALLS_PER_ATTEMPT = {
    "direct": 1 + 1,
    "chain": 3 + 1,
    "departments": len(DEPARTMENT_ROLES) + 1,
    "fan_out": FAN_OUT_CANDIDATES + 1 + 1,  # 후보 N + judge 1 + 합격 판정 1
    "fan_out_mixed": len(MIXED_FAN_OUT_BACKENDS) + 1 + 1,
}


def _estimate_total_calls(conditions: list[str], k: int) -> int:
    return k * sum(_CALLS_PER_ATTEMPT.get(c, 0) for c in conditions)


def _run_condition(label: str, attempt: int, chain_root: Path) -> dict:
    if label == "direct":
        return run_direct(attempt)
    if label == "chain":
        return run_chain(attempt, chain_root)
    if label == "departments":
        return run_chain(attempt, chain_root, roles=DEPARTMENT_ROLES, label="departments")
    if label == "fan_out":
        return run_fan_out(attempt, chain_root)
    if label == "fan_out_mixed":
        return run_fan_out(
            attempt, chain_root, backends=MIXED_FAN_OUT_BACKENDS, label="fan_out_mixed"
        )
    raise ValueError(f"알 수 없는 조건: {label!r} (사용 가능: {list(CONDITIONS)})")


def evaluate(result: dict) -> dict:
    """모든 조건 공통 blind 판정 — evaluator는 조건 라벨을 모른다."""
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
        "roles": rows[0].get("roles") if rows else None,
        # 입력 토큰 평균 (2026-07-29). 종량제 키에서는 입력도 실제 청구 대상이고,
        # 체인은 스텝마다 이전 결과를 전부 받아 입력이 계단식으로 커진다 —
        # 조건 간 입력 규모 차이를 여기서 바로 볼 수 있게 한다.
        "avg_input_tokens": _avg_input_tokens(rows),
    }


def head_to_head(results: list[dict], conditions: list[str], *, pace: int) -> list[dict]:
    """기준 조건과 나머지를 **blind로 정면 비교**한다. 합격률과 별개의 축이다.

    **왜 필요한가**: 판정 기준(`harness-vs-ecc-decision-2026-07-ko.md` §6)은
    "fan_out 합격률 > direct면 값 입증, ≈면 축소 결정"인데 **4차 측정에서 direct가
    이미 3/3(천장)이었다.** 그 상태로 조건만 추가하면 `>`가 원리적으로 불가능하고
    `≈`가 확정적으로 나온다 — **rubric 인공물이 엔진 핵심을 지우는 결정을 촉발한다.**
    3차는 바닥 효과, 4차는 천장 효과였으니 pass/fail 한 축만으로는 세 번째 측정도
    같은 운명이다.

    정면 비교는 천장이 없다 — 둘 다 "합격"이어도 어느 쪽이 나은지는 갈린다.
    **엔진 자신의 judge를 그대로 쓴다**(`judge.evaluate`): blind 레이블(A/B) + 무작위
    순서로 position/identity bias를 이미 다루고 있고, 측정용으로 판정자를 새로 만들면
    "측정에 쓴 판정자와 엔진이 쓰는 판정자가 다르다"는 문제가 생긴다.

    한계: n이 작으면 2-1 같은 결과는 신호가 아니다. 그리고 이건 **선호 비교**라
    "얼마나 더 나은가"는 말하지 않는다.
    """
    baseline = "direct" if "direct" in conditions else conditions[0]
    others = [c for c in conditions if c != baseline]
    if not others:
        return []

    by_attempt: dict[tuple[str, int], dict] = {(r["condition"], r["attempt"]): r for r in results}
    attempts = sorted({r["attempt"] for r in results})
    pairs: list[dict] = []
    for attempt in attempts:
        base_row = by_attempt.get((baseline, attempt))
        for other in others:
            other_row = by_attempt.get((other, attempt))
            if not (base_row and other_row and base_row["ok"] and other_row["ok"]):
                continue
            time.sleep(pace)
            print(f"[정면 비교] {baseline} vs {other} #{attempt}")
            # model_id를 조건 이름으로 둔다 — judge 프롬프트에는 레이블(A/B)과 본문만
            # 들어가므로(`judge._build_prompt` 확인) blind는 유지된다. 판정 결과를
            # 조건으로 되돌리기 위한 식별자일 뿐이다.
            pair_candidates = [
                Candidate(model_id=baseline, content=base_row["content"], status="success"),
                Candidate(model_id=other, content=other_row["content"], status="success"),
            ]
            try:
                judging = judge.evaluate(pair_candidates, RUBRIC, _evaluator("h2h"))
            except (judge.JudgeError, ValueError) as exc:
                pairs.append({"attempt": attempt, "baseline": baseline, "other": other,
                              "winner": None, "error": str(exc)[:200]})
                continue
            scores = {s.candidate: s.score for s in judging.scores}
            pairs.append({
                "attempt": attempt,
                "baseline": baseline,
                "other": other,
                "winner": judging.winner,
                "scores": scores,
                # 점수가 붙어 있으면 "이겼다"는 말의 무게가 다르다. 엔진 자신의 판단을
                # 그대로 읽는다 — 예전엔 `recommended_strategy == "merge_top_candidates"`로
                # 알아냈는데, ADR 0011로 그 전략이 없어지면서 전용 필드로 옮겨졌다.
                "near_tie": judging.top_scores_near_tie,
                "cost_usd": judging.cost_usd,
                "subscription_calls": judging.subscription_calls,
            })
    return pairs


def summarize_head_to_head(pairs: list[dict]) -> list[dict]:
    """정면 비교를 조건별 승/패로 접는다.

    **승패는 엔진의 병합 임계값을 쓰지 않는다.** `_MERGE_THRESHOLD`(0.1)는 "후보들을
    합칠까"를 정하는 값이라 10점 미만 차이를 전부 근접으로 본다 — 그걸 승패 판정에
    그대로 쓰면 웬만한 비교가 다 무승부가 되어 **천장 효과가 다른 형태로 재현된다**
    (mock 검증에서 전 쌍이 무승부로 나와 확인했다). 여기서는 점수가 높은 쪽을 그대로
    승자로 세고, 임계값 안에 든 쌍은 **별도 칸에 참고로만** 남긴다 — 승패가 근소한지
    아닌지는 읽는 사람이 판단할 정보이고, 미리 뭉개면 정보가 사라진다.
    """
    summaries = []
    for other in sorted({p["other"] for p in pairs}):
        rows = [p for p in pairs if p["other"] == other and p.get("winner")]
        if not rows:
            continue
        baseline = rows[0]["baseline"]
        summaries.append({
            "baseline": baseline,
            "other": other,
            "compared": len(rows),
            "other_wins": sum(1 for p in rows if p["winner"] == other),
            "baseline_wins": sum(1 for p in rows if p["winner"] == baseline),
            # 참고용: 엔진이 "합칠 만큼 근접"으로 본 쌍 수(승패 계산에는 안 쓴다)
            "within_engine_tie_band": sum(1 for p in rows if p["near_tie"]),
            "avg_score_baseline": round(
                sum(p["scores"].get(baseline, 0) for p in rows) / len(rows), 3),
            "avg_score_other": round(
                sum(p["scores"].get(other, 0) for p in rows) / len(rows), 3),
        })
    return summaries


def _avg_input_tokens(rows: list[dict]) -> int | None:
    per_attempt = [
        sum(t for t in row["step_input_tokens"] if t is not None)
        for row in rows
        if row.get("step_input_tokens")
    ]
    return int(sum(per_attempt) / len(per_attempt)) if per_attempt else None


def main() -> None:
    # 선언이 첫 사용보다 위여야 한다 — argparse 기본값이 MIXED_FAN_OUT_BACKENDS를 읽는다.
    global _GENERATOR_BACKEND, _EVALUATOR_BACKEND, PROMPT, MIXED_FAN_OUT_BACKENDS

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
    parser.add_argument(
        "--conditions",
        default="direct,chain",
        help=(
            f"측정할 조건, 콤마 구분 (사용 가능: {','.join(CONDITIONS)}). "
            "기본 'direct,chain' — 1·2차 측정과 같은 조건이라 그대로 비교할 수 있다. "
            "'departments'를 넣으면 5부서 체인까지 본다(호출이 시도당 6회 더 늘어난다). "
            f"'fan_out'은 후보 {FAN_OUT_CANDIDATES}개 + judge로 fan_out_judge를 잰다"
        ),
    )
    parser.add_argument(
        "--prompt", default="basic", choices=sorted(_PROMPTS),
        help=(
            "측정에 쓸 프롬프트. 기본 'basic'(1~5차와 동일 — 과거 숫자와 비교 가능). "
            "'diagnostic'은 난이도를 올린 6차용 — 5차에서 direct가 만점이라 어떤 패턴도 "
            "합격률로 그것을 넘을 수 없었고, 쉬운 과제에서는 구조의 값이 나타날 자리가 없다"
        ),
    )
    parser.add_argument(
        "--fan-out-models", default=",".join(MIXED_FAN_OUT_BACKENDS),
        help=(
            "'fan_out_mixed' 조건의 후보 백엔드, 콤마 구분 (기본 "
            f"'{','.join(MIXED_FAN_OUT_BACKENDS)}' — 운영 config의 candidate_models와 같다). "
            "이 조건만이 '모델을 섞는 게 값이 있나'를 잰다. 비교 상대는 direct가 아니라 "
            "fan_out(동일 모델 후보)이어야 구조 효과와 다양성 효과가 분리된다"
        ),
    )
    parser.add_argument(
        "--no-head-to-head", action="store_true",
        help=(
            "기준 조건과의 blind 정면 비교를 생략한다. 기본은 실행 — 합격률만으로는 "
            "천장/바닥 효과에 걸려 조건이 구분되지 않는다는 게 3·4차 측정에서 드러났다"
            " (호출이 (조건수-1)×k회 늘어난다)"
        ),
    )
    parser.add_argument(
        "--pace-seconds", type=int, default=None,
        help=(
            f"호출 간격(초). 기본은 백엔드에 따라 자동(gemini {PACE_SECONDS}초 / "
            f"구독 {SUBSCRIPTION_PACE_SECONDS}초). gemini 종량제 키는 무료 티어보다 "
            "속도 한도가 넉넉하므로 낮춰서 측정 시간을 줄일 수 있다 — 429가 나면 올릴 것"
        ),
    )
    args = parser.parse_args()

    conditions = [c.strip() for c in args.conditions.split(",") if c.strip()]
    unknown = [c for c in conditions if c not in CONDITIONS]
    if unknown:
        raise SystemExit(f"[fatal] 알 수 없는 조건: {unknown} (사용 가능: {list(CONDITIONS)})")

    PROMPT = _PROMPTS[args.prompt]
    MIXED_FAN_OUT_BACKENDS = tuple(b.strip() for b in args.fan_out_models.split(",") if b.strip())
    unknown_backends = [b for b in MIXED_FAN_OUT_BACKENDS if b not in _BACKENDS]
    if unknown_backends:
        raise SystemExit(f"[fatal] 알 수 없는 백엔드: {unknown_backends} (사용 가능: {sorted(_BACKENDS)})")
    _CALLS_PER_ATTEMPT["fan_out_mixed"] = len(MIXED_FAN_OUT_BACKENDS) + 1 + 1

    # **구독 후보 상한을 측정 의도에 맞춰 명시한다.** 이 스크립트는 `cli.py`를 지나지
    # 않으므로 `orchestrator.MAX_SUBSCRIPTION_CANDIDATES`의 모듈 기본값(1)이 적용된다.
    # 구독 백엔드로 후보 2개를 만들면 상한이 1개로 깎으려 하고, 그러면 MIN_CANDIDATES(2)
    # 미만이 되어 **가드가 상한을 무시해준 덕에** 어쩌다 2개가 도는 상태가 된다.
    # 우연에 기대면 가드가 바뀌는 순간 측정이 조용히 후보 1개로 줄어든다 — 그건 fan_out이
    # 아니라 direct다.
    orchestrator.MAX_SUBSCRIPTION_CANDIDATES = max(
        FAN_OUT_CANDIDATES, len(MIXED_FAN_OUT_BACKENDS)
    )
    # auto면 시작 전에 gemini를 한 번 확인해서 백엔드를 정한다 — 호출마다 갈아타면
    # 조건별 모델이 달라져 비교가 성립하지 않는다(_resolve_backend 참고).
    if "auto" in (args.generator, args.evaluator):
        print("백엔드 자동 선택: gemini 응답 확인 중...")
    resolved = _resolve_backend("auto") if "auto" in (args.generator, args.evaluator) else None
    _GENERATOR_BACKEND = resolved if args.generator == "auto" else args.generator
    _EVALUATOR_BACKEND = resolved if args.evaluator == "auto" else args.evaluator
    pace = args.pace_seconds if args.pace_seconds is not None else _pace_seconds()
    print(
        f"generator={_GENERATOR_BACKEND} / evaluator={_EVALUATOR_BACKEND} / "
        f"호출 간격 {pace}초 / 조건 {conditions} / 프롬프트 {args.prompt!r}"
        + (f" / 다양성 후보 {list(MIXED_FAN_OUT_BACKENDS)}" if "fan_out_mixed" in conditions else "")
    )
    # 호출 수를 미리 보여준다 — 종량제 키로 바뀐 뒤에는 "몇 번 부르는지"가 곧 금액이라,
    # 시작 전에 규모를 알고 중단할 수 있어야 한다.
    h2h_calls = 0 if args.no_head_to_head else args.k * max(0, len(conditions) - 1)
    print(
        f"예상 LLM 호출: 약 {_estimate_total_calls(conditions, args.k) + h2h_calls}회 (판정 포함"
        + (f", 정면 비교 {h2h_calls}회 포함)" if h2h_calls else ")")
    )
    if _uses_subscription():
        print("주의: 구독(claude/codex) 호출이라 $ 비용 대신 구독 한도를 소모한다.")
    print()

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    measure_root = Path("_workspace/measurements")
    chain_runs_root = measure_root / f"chain_runs_{stamp}"
    chain_runs_root.mkdir(parents=True, exist_ok=True)

    results: list[dict] = []
    first = True
    total_steps = args.k * len(conditions)
    for i in range(1, args.k + 1):
        for label in conditions:
            if not first:
                time.sleep(pace)  # free tier 롤링 윈도우 한도 회피 (위 주석)
            first = False
            print(f"[{i}/{args.k}] {label} 실행 중...")
            result = _run_condition(label, i, chain_runs_root)
            time.sleep(pace)
            results.append(evaluate(result))

            # 측정 도중 백엔드가 바뀌면 조건마다 다른 모델로 돌아가 "구조 효과"와
            # "모델 효과"가 섞인다 — 그런 데이터는 비교에 쓸 수 없으므로 **더 돌리지
            # 않고 즉시 중단**한다. 경고만 붙여 저장하면 나중에 누군가 그 숫자를
            # 그대로 인용할 위험이 있다(2026-07-28 실제로 그런 run이 하나 나옴).
            if any(w.used_fallback for w in _FALLBACK_WRAPPERS):
                print(
                    f"\n[중단] gemini가 측정 도중 응답하지 못해 claude로 대체됐다"
                    f" (진행 {len(results)}/{total_steps}회).\n"
                    "        조건마다 모델이 달라져 비교가 성립하지 않으므로 결과를"
                    " 저장하지 않고 멈춘다.\n"
                    "        한 모델로 고정해 다시 돌릴 것:\n"
                    "          PYTHONPATH=src python scripts/measure_pattern_value.py"
                    " --generator claude --evaluator claude\n"
                    "        (또는 gemini 한도가 회복된 뒤 --generator gemini --evaluator gemini)"
                )
                raise SystemExit(1)

    summaries = [summarize(results, label) for label in conditions]

    # 정면 비교는 모든 조건이 끝난 뒤에 한다 — 같은 시도끼리 짝지어야 하기 때문이다.
    h2h_pairs = [] if args.no_head_to_head else head_to_head(results, conditions, pace=pace)
    h2h_summaries = summarize_head_to_head(h2h_pairs)

    print("\n## 결과 요약")
    for s in summaries:
        print(
            f"- {s['condition']}: 합격률 {s['pass_rate']}, 평균 지연 {s['avg_latency_ms']}ms, "
            f"평균 run 비용 ${s['avg_run_cost_usd']}, 시도당 호출 {s['llm_calls_per_attempt']}회"
            + (f", 입력 토큰 평균 {s['avg_input_tokens']}" if s["avg_input_tokens"] else "")
            + (f", 구독 호출 누적 {s['total_subscription_calls']}회" if _uses_subscription() else "")
        )
    print("\n## 판정 상세 (조건/시도/합격 — 불합격 사유 앞부분)")
    for r in results:
        head = (r["feedback"] or "")[:120].replace("\n", " ")
        # unmet_rubric_items가 비어 있는 불합격은 품질 신호가 아니라 판정 신뢰도 문제다
        # (2026-07-29 judge 프롬프트 개편의 확인 지표).
        flag = "" if r.get("passed") or r.get("unmet_rubric_items") else " [rubric 미지정!]"
        print(f"- {r['condition']} #{r['attempt']}: passed={r['passed']}{flag} {head}")

    if h2h_summaries:
        print("\n## 정면 비교 (blind, 기준 조건 대비 — 합격률의 천장을 우회하는 축)")
        for s in h2h_summaries:
            print(
                f"- {s['other']} vs {s['baseline']}: {s['other']} 승 {s['other_wins']} / "
                f"{s['baseline']} 승 {s['baseline_wins']} "
                f"(그중 근소한 차 {s['within_engine_tie_band']}쌍, "
                f"비교 {s['compared']}쌍, 평균 점수 "
                f"{s['other']} {s['avg_score_other']} vs {s['baseline']} {s['avg_score_baseline']})"
            )
    elif not args.no_head_to_head:
        print("\n## 정면 비교: 비교 가능한 쌍이 없었다(양쪽 다 성공한 시도 없음)")

    out_path = measure_root / f"pattern_value_{stamp}.json"
    out_path.write_text(
        json.dumps({"prompt_name": args.prompt, "prompt": PROMPT, "rubric": RUBRIC, "k": args.k, "conditions": conditions,
                    "generator": _GENERATOR_BACKEND, "evaluator": _EVALUATOR_BACKEND,
                    "requested_generator": args.generator, "requested_evaluator": args.evaluator,
                    "pace_seconds": pace,
                    "fan_out_candidates": FAN_OUT_CANDIDATES if "fan_out" in conditions else None,
                    "mixed_fan_out_backends": (
                        list(MIXED_FAN_OUT_BACKENDS) if "fan_out_mixed" in conditions else None
                    ),
                    "fallback_used": any(w.used_fallback for w in _FALLBACK_WRAPPERS),
                    "summaries": summaries,
                    "head_to_head_summaries": h2h_summaries,
                    "head_to_head_pairs": h2h_pairs,
                    "results": results}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n[ok] 전체 결과 저장: {out_path}")


if __name__ == "__main__":
    main()
