"""매니저-워커 위임 (ADR 0014, 2026-08-13). `hierarchical_delegation`의 재작성.

## 왜 다시 썼나

이 패턴의 **원래 의도는 컨텍스트 절약**이었다. `subagent_runner.py`(폐기 예정)의
docstring이 `gaebalai/claude-code-orchestrator`를 인용하며 "컨텍스트 격리 시뮬레이션"이라고
적어둔 그대로다: 큰 출력은 워커가 만들고, 매니저는 요약과 경로만 받는다.

**구현이 거기서 갈라졌다.** 격리가 "메인 Orchestrator에게 무엇을 보여주는가"에만 적용됐고,
정작 토큰이 흐르는 **단계→단계** 채널은 앞 단계 출력 **전문**을 실어 보냈다. 게다가
보호받던 "메인 Orchestrator"는 LLM이 아니라 **Python 코드**다 — 컨텍스트 창도 구독 한도도
없으니 요약해서 넘겨도 아끼는 게 없었다. 결과가 실측에 그대로 나왔다:

    조건                     합격률   입력토큰
    direct (기준선)          0.67        74
    chain (3역할)            0.43     5,806   <- 78배 쓰고 기준선보다 나쁘다
    departments (5역할)      0.83    23,660   <- 320배

**ADR 0009는 이 패턴을 강등했지만, 잰 것은 "역할을 쪼개면 품질이 오르나"였다**(답: 아니오).
원래 목적인 컨텍스트 절약은 한 번도 측정되지 않았다. 그래서 강등 근거를 이 재작성에
그대로 적용하면 안 된다 — ADR 0014에 그 구분을 남겼다.

## 무엇이 달라졌나

누적(chain)을 **팬아웃(fan-in)** 으로 바꿨다. 세 단계다:

    1. 분해   매니저(claude) 1회 호출 -> DelegationPlan(제목 + 머리글 + 조각 목록)
              입력·출력 모두 작다. 본문이 아직 없으므로 매니저는 본문을 볼 일이 없다.

    2. 실행   조각마다 워커(codex/gemini) 1회. **조각끼리 서로를 보지 않는다.**
              각 워커 입력 = 원본 요청 + 자기 지시. 전문은 파일로만.
              워커가 여럿이면 provider별로 병렬(같은 provider의 조각은 순차 —
              한 API의 rate limit에 동시 호출을 겹치지 않게).

    3. 조립   concat: 매니저 계획의 뼈대에 조각 파일을 끼운다. **LLM 호출 0회.**
              llm:    매니저가 조각을 받아 하나로 다시 쓴다. 조각 총량을 **한 번** 쓴다.

입력 증폭이 조각 수에 **선형**이다. 체인은 제곱에 가까웠다 — 그게 이 재작성의 전부다.

## concat 조립이 ADR 0011의 실패와 다른 이유

ADR 0011은 "상위 두 후보를 이어붙이는" 병합을 없앴다. 이유는 **완결된 문서 두 개**가
붙어서 "절차서 하나를 써줘"에 둘을 내놨기 때문이다. 여기는 다르다 — 조각은 처음부터
**겹치지 않는 섹션으로 설계된 것**이고, 뼈대도 매니저가 미리 정했다. 이어붙이는 게
맞는 유일한 경우다. 그래서 분해 프롬프트가 "서로 겹치지 않게" 를 명시적으로 요구한다.

## 한계 (숨기지 않는다)

- **매니저가 조각 본문을 검토하지 않는다**(concat 모드). 조각 사이의 중복·모순을 잡는
  주체가 없다. `llm` 모드가 그걸 하지만 조각 총량만큼 토큰을 쓴다 — 절약과 정합성의
  교환이고, 기본값은 절약(concat)이다.
- **아직 매니저가 도구를 쓰지 않는다.** 진짜 에이전트 매니저라면 조각을 골라 읽고 다시
  위임할 수 있어야 하는데, 그건 claude에 Bash/MCP를 열어야 해서 ADR 0007의 안전 경계를
  건드린다. 이 단계에서는 하네스가 순서를 돌리고 매니저는 분해·조립만 한다.
"""
from __future__ import annotations

import json
import re
from concurrent import futures
from pathlib import Path
from typing import Optional

from providers.base import Provider

from . import run_store
from .budget import BudgetTracker
from .model_runner import generate_with_retry
from .schemas import Candidate, DelegationPlan, WorkerPart

# 조각 수 상한. 조각 하나가 워커 호출 하나라 비용에 직결된다(`MAX_REFINEMENT_ROUNDS`와
# 같은 성격). 매니저가 이보다 많이 제안하면 잘라낸다 — 상한을 매니저 판단에 맡기지 않는다.
MAX_PARTS = 6

# 조립 방식. "concat"이 기본인 이유는 이 패턴이 아끼려는 게 매니저 토큰이기 때문이다.
ASSEMBLE_MODE = "concat"

_PARTS_DIRNAME = "artifacts/parts"


class DelegationError(RuntimeError):
    """매니저 호출이 실패했거나 분해 계획을 파싱할 수 없다."""


def _decompose_prompt(request: str, max_parts: int) -> str:
    """분해 지시문. **본문을 쓰라고 하지 않는다** — 그게 워커 몫이고, 여기서 쓰면 아낀 게 없다."""
    return (
        "당신은 문서 작성을 지휘하는 관리자다. 아래 요청을 **겹치지 않는 섹션**으로 쪼개고,\n"
        "각 섹션을 담당할 작성자에게 줄 지시를 만들어라.\n\n"
        "## 지켜야 할 것\n"
        f"- 섹션은 최대 {max_parts}개. 적을수록 좋다 — 쪼갤 이유가 없으면 쪼개지 마라.\n"
        "- **섹션끼리 내용이 겹치면 안 된다.** 각 작성자는 다른 섹션을 보지 못하므로,\n"
        "  겹치게 지시하면 최종 문서에 같은 내용이 두 번 들어간다.\n"
        "- **당신은 본문을 쓰지 않는다.** 섹션 제목과 작성 지시만 만들어라.\n"
        "- 각 지시는 그것만 읽고도 작업이 되도록 자기완결적이어야 한다(작성자는 이 대화를\n"
        "  보지 못한다). 필요한 맥락은 지시 안에 담아라.\n"
        "- `intro`는 문서 도입부 본문이다(2~4문장). 아직 섹션 본문이 없으므로 개별 내용을\n"
        "  단정하지 말고, 이 문서가 무엇을 다루는지만 써라.\n"
        "- 주어지지 않은 수치를 임의로 만들어 채우지 마라.\n\n"
        "## 원본 요청\n"
        f"{request}\n\n"
        "## 출력 형식 (JSON만. 앞뒤에 다른 텍스트를 붙이지 마라)\n"
        '{"document_title": "...", "intro": "...", '
        '"parts": [{"title": "...", "instruction": "..."}]}\n'
    )


def _worker_prompt(request: str, part: WorkerPart) -> str:
    """워커 지시문. 원본 요청 + 자기 지시만 준다 — **다른 조각은 주지 않는다.**"""
    return (
        f"당신은 문서의 '{part.title}' 섹션을 작성한다. 이 섹션만 쓰면 되고, 다른 섹션은\n"
        "다른 작성자가 맡는다 — 여기서 문서 전체를 끝내려고 하지 마라.\n\n"
        "## 지켜야 할 것\n"
        "- 이 섹션의 **본문만** 써라. 문서 제목이나 다른 섹션의 제목을 쓰지 마라.\n"
        f"- 섹션 제목(`## {part.title}`)은 조립할 때 붙으므로 직접 쓰지 마라.\n"
        "- 파일을 만들거나 승인을 요청하지 마라. 작업 보고문 대신 **완성된 본문 자체**를 응답으로 줘라.\n"
        "- 주어지지 않은 수치를 임의로 만들어 채우지 마라. 모르면 판단 기준을 설명해라.\n\n"
        "## 당신이 맡은 지시\n"
        f"{part.instruction}\n\n"
        "## 원본 요청 (전체 맥락 — 당신 섹션의 범위를 넘지 마라)\n"
        f"{request}\n"
    )


def decompose(
    request: str,
    manager: Provider,
    *,
    max_parts: int = MAX_PARTS,
    budget: Optional[BudgetTracker] = None,
) -> tuple[DelegationPlan, Candidate]:
    """매니저를 1회 불러 분해 계획을 만든다. 입력·출력 모두 작다.

    **Candidate를 함께 돌려주는 이유**: 이 호출도 지연·비용·구독 한도를 쓴다. 처음엔
    계획만 돌려줬는데 그러자 **분해 호출이 metrics에서 통째로 사라졌다**(구독 호출 3회를
    2회로 보고). `test_subscription_call_metrics`가 잡아냈다 — 매니저 호출이 "작다"는
    것과 "안 센다"는 건 다르다(Section 9 Cost Blindness).
    """
    candidate = generate_with_retry(
        manager, _decompose_prompt(request, max_parts), temperature=0.0, budget=budget
    )
    if candidate.status == "error":
        raise DelegationError(f"매니저(분해) 호출 실패: {candidate.content}")

    plan = _parse_plan(candidate.content, max_parts=max_parts)
    return plan, candidate


def _parse_plan(content: str, *, max_parts: int) -> DelegationPlan:
    """매니저 응답에서 JSON을 뽑는다 (`judge._parse_response`와 같은 방식)."""
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise DelegationError(f"분해 계획에서 JSON을 찾지 못함: {content[:200]!r}")
    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise DelegationError(f"분해 계획 JSON 파싱 실패: {content[:200]!r}") from exc

    raw_parts = parsed.get("parts")
    if not isinstance(raw_parts, list) or not raw_parts:
        raise DelegationError(f"분해 계획에 parts가 없거나 비었다: {content[:200]!r}")

    parts: list[WorkerPart] = []
    for entry in raw_parts[:max_parts]:
        if not isinstance(entry, dict):
            continue
        title = str(entry.get("title", "")).strip()
        instruction = str(entry.get("instruction", "")).strip()
        if not title or not instruction:
            # 제목이나 지시가 빠진 조각은 워커에게 보낼 수 없다 — 조용히 버리지 않고
            # 남은 게 없으면 아래에서 실패로 만든다.
            continue
        parts.append(WorkerPart(title=title, instruction=instruction, status="pending"))

    if not parts:
        raise DelegationError(f"쓸 수 있는 조각이 하나도 없다: {content[:200]!r}")

    return DelegationPlan(
        document_title=str(parsed.get("document_title", "")).strip() or "(제목 없음)",
        intro=str(parsed.get("intro", "")).strip(),
        parts=parts,
    )


def _slug(index: int, title: str) -> str:
    """조각 파일 이름. 순서를 보존해야 조립이 재현된다."""
    cleaned = re.sub(r"[^0-9A-Za-z가-힣]+", "-", title).strip("-")[:40] or "part"
    return f"{index:02d}-{cleaned}"


def run_workers(
    request: str,
    parts: list[WorkerPart],
    workers: list[Provider],
    run_dir: Path,
    *,
    budget: Optional[BudgetTracker] = None,
) -> list[WorkerPart]:
    """조각을 워커에게 돌린다. **전문은 파일로만 남고 반환값에는 크기와 경로만 담는다.**

    provider별로 조각을 나눠 **provider 사이는 병렬, 같은 provider 안은 순차**로 돈다 —
    한 API의 rate limit에 동시 호출을 겹치지 않게 하려는 것이다(`model_runner`의 후보
    병렬화에서 서로 다른 provider라 안전했던 것과 같은 논리를 여기서는 명시적으로 만든다).

    예산 상한이 있으면 병렬을 쓰지 않는다 — 상한의 유일한 수단이 "다음 호출을 시작하지
    않는 것"이라 병렬로 던지면 막을 대상이 이미 날아간 뒤가 된다(`model_runner._worker_count`).
    """
    if not workers:
        raise DelegationError("워커 provider가 없다")

    buckets: list[list[int]] = [[] for _ in workers]
    for index in range(len(parts)):
        buckets[index % len(workers)].append(index)

    serial = budget is not None and not budget.unlimited

    def run_bucket(worker_index: int) -> None:
        provider = workers[worker_index]
        for part_index in buckets[worker_index]:
            part = parts[part_index]
            if budget is not None and budget.exhausted:
                part.status = "error"
                part.provider_id = provider.model_id
                continue
            candidate = generate_with_retry(
                provider, _worker_prompt(request, part), budget=budget
            )
            part.provider_id = candidate.model_id
            part.status = "error" if candidate.status == "error" else "success"
            part.latency_ms = candidate.latency_ms
            part.cost_usd = candidate.cost_usd
            part.subscription_calls = candidate.subscription_calls
            part.input_tokens = candidate.input_tokens
            if part.status == "error":
                continue
            body = candidate.content.strip()
            part.chars = len(body)
            name = f"{_PARTS_DIRNAME}/{_slug(part_index + 1, part.title)}.md"
            run_store.write_markdown(run_dir, name, body)
            part.output_ref = name

    if serial or len(workers) == 1:
        for worker_index in range(len(workers)):
            run_bucket(worker_index)
    else:
        with futures.ThreadPoolExecutor(
            max_workers=len(workers), thread_name_prefix="worker"
        ) as pool:
            for future in futures.as_completed(
                [pool.submit(run_bucket, i) for i in range(len(workers))]
            ):
                future.result()  # 워커 실패는 error 조각으로 흡수되므로 여기 예외는 하네스 결함이다

    return parts


def assemble(
    request: str,
    plan: DelegationPlan,
    run_dir: Path,
    *,
    mode: str = ASSEMBLE_MODE,
    manager: Optional[Provider] = None,
    budget: Optional[BudgetTracker] = None,
) -> tuple[str, Optional[object]]:
    """조각을 최종 문서로 합친다. 두 번째 반환값은 `llm` 모드에서 쓴 매니저 Candidate.

    `concat`은 **LLM 호출 0회**다 — 매니저 계획의 뼈대에 조각 파일을 끼운다.
    `llm`은 조각 본문을 매니저에게 한 번 보내 다시 쓰게 한다(조각 총량 × 1회).
    """
    usable = [p for p in plan.parts if p.status == "success" and p.output_ref]
    if not usable:
        raise DelegationError("성공한 조각이 없어 조립할 수 없다")

    sections: list[tuple[str, str]] = []
    for part in usable:
        body = run_store.read_markdown(run_dir, part.output_ref).strip()
        sections.append((part.title, body))

    skeleton = [f"# {plan.document_title}"]
    if plan.intro:
        skeleton.append(plan.intro)
    for title, body in sections:
        skeleton.append(f"## {title}")
        skeleton.append(body)
    concatenated = "\n\n".join(skeleton) + "\n"

    if mode != "llm":
        return concatenated, None

    if manager is None:
        raise DelegationError('assemble mode="llm"에는 manager provider가 필요하다')
    prompt = (
        "당신은 아래 초안을 하나의 문서로 다듬는 편집자다. 여러 작성자가 섹션을 따로 썼기\n"
        "때문에 중복·용어 불일치·연결이 끊긴 부분이 있을 수 있다.\n\n"
        "## 지켜야 할 것\n"
        "- **새 정보나 판단을 추가하지 마라.** 중복 제거, 용어 통일, 연결 문장 정리까지가 범위다.\n"
        "- 섹션을 통째로 삭제하지 마라.\n"
        "- 작업 보고문 대신 **완성된 문서 본문 자체**를 응답으로 줘라.\n\n"
        "## 원본 요청\n"
        f"{request}\n\n"
        "## 초안\n"
        f"{concatenated}\n"
    )
    candidate = generate_with_retry(manager, prompt, temperature=0.0, budget=budget)
    if candidate.status == "error":
        # **조립 실패로 산출물을 버리지 않는다** — 이어붙인 초안이 이미 유효한 문서다
        # (예산 상한/체인 중단을 partial로 승격하는 것과 같은 관례).
        return concatenated, candidate
    return candidate.content.strip(), candidate
