"""Judge: LLM 기반 평가 (fan_out_judge의 후보 비교 + iterative_refinement의 합격 판정).

harness-implementation-plan-ko.md Section 6, Section 7 Step 6과
`docs/adr/0004-judge-real-llm-upgrade.md`를 구현한다.

기존에는 rubric 문구가 candidate.content에 리터럴로 등장하는지 + 응답 길이로
점수를 매기는 규칙 기반 mock이었다. 실제 provider로 fan_out_judge를 돌려보니
실제 LLM은 산문으로 답해 rubric이 리터럴로 거의 안 걸리고, 사실상 응답
길이로만 승자가 갈리는 문제를 실측으로 확인했다(ADR 0004 배경). 이를
`judge_provider`로 실제 LLM 판단을 1회 호출하는 방식으로 승격한다.

bias 완화 장치 두 가지를 프롬프트에 반영한다(ADR 0004 결정 2번):
- reject-first: "문제 없음"을 기본값으로 주지 않고, 각 후보의 결함을 근거와
  함께 찾도록 강제한다.
- blind 익명화: 모델명 대신 무작위 순서의 A/B/C... 레이블을 쓰고, 길이로
  판단하지 말라고 명시적으로 지시한다.

judge 호출도 model_runner.generate_with_retry를 재사용해 다른 provider
호출과 동일하게 1회 재시도 계약을 따른다(Section 6). 호출이 끝내 실패하거나
응답을 파싱할 수 없으면 JudgeError를 던진다 — orchestrator가 이를 잡아
run을 error로 종료한다(errors.json에 stage="judge"로 기록).
"""
from __future__ import annotations

import json
import random
import re
import string

from providers.base import Provider

from typing import Optional

from . import model_runner
from .budget import BudgetTracker
from .schemas import Judging, JudgingScore, Candidate, RefinementVerdict

# 1·2등 점수 차이가 이 값(0~1 스케일) 미만이면 "판정자가 못 갈랐다"로 본다.
# 예전엔 이 조건에서 병합 전략을 추천했으나 ADR 0011로 폐기했다 — 이름은 이력 때문에
# 남겼고, 지금 쓰임은 `Judging.top_scores_near_tie` 표시뿐이다.
_MERGE_THRESHOLD = 0.1


class JudgeError(RuntimeError):
    """judge_provider 호출이 끝내 실패했거나 응답을 파싱할 수 없다."""


def evaluate(
    candidates: list[Candidate],
    rubric: list[str],
    judge_provider: Provider,
    *,
    budget: Optional[BudgetTracker] = None,
) -> Judging:
    """성공한 candidate만 judge_provider로 실제 평가한다.

    성공한 candidate가 하나도 없으면 예외를 던진다 — min_candidates 판단은
    orchestrator/recovery 책임이고, judge는 "평가할 게 있다"는 전제하에서만
    동작한다.
    """
    successful = [c for c in candidates if c.status == "success"]
    if not successful:
        raise ValueError("평가할 성공한 candidate가 없다 (min_candidates 판단은 orchestrator 책임)")

    labels = _assign_labels(successful)
    prompt = _build_prompt(rubric, labels)

    judge_candidate = model_runner.generate_with_retry(judge_provider, prompt, temperature=0.0, budget=budget)
    if judge_candidate.status == "error":
        raise JudgeError(f"judge 호출 실패: {judge_candidate.content}")

    parsed = _parse_response(judge_candidate.content, labels)

    scores = [
        JudgingScore(
            candidate=candidate.model_id,
            score=parsed[label]["score"] / 100.0,
            weaknesses=parsed[label]["flaws"],
        )
        for label, candidate in labels.items()
    ]
    winner = max(scores, key=lambda s: s.score)

    return Judging(
        scores=scores,
        recommended_strategy=_recommend_strategy(scores),
        top_scores_near_tie=_is_near_tie(scores),
        winner=winner.candidate,
        latency_ms=judge_candidate.latency_ms,
        cost_usd=judge_candidate.cost_usd,
        subscription_calls=judge_candidate.subscription_calls,
    )


def check_pass(
    content: str,
    rubric: list[str],
    judge_provider: Provider,
    *,
    request: Optional[str] = None,
    budget: Optional[BudgetTracker] = None,
) -> RefinementVerdict:
    """콘텐츠 하나가 rubric을 통과하는지 판정한다 (iterative_refinement 전용).

    evaluate()(N개 후보 비교)와 다른 질문이라 별도 함수다. reject-first 원칙은
    동일하게 적용한다(ADR 0004) — 결함부터 찾고, 그 결함이 rubric을 못 채울
    정도인지 판단하게 한다. feedback은 fail일 때 다음 라운드 generator 프롬프트에
    그대로 들어가므로 "무엇을 어떻게 고쳐야 하는지"를 요구한다.
    """
    prompt = _build_pass_prompt(content, rubric, request)

    judge_candidate = model_runner.generate_with_retry(judge_provider, prompt, temperature=0.0, budget=budget)
    if judge_candidate.status == "error":
        raise JudgeError(f"evaluator 호출 실패: {judge_candidate.content}")

    parsed = _parse_pass_response(judge_candidate.content)

    return RefinementVerdict(
        passed=parsed["passed"],
        feedback=parsed["feedback"],
        unmet_rubric_items=parsed["unmet_rubric_items"],
        latency_ms=judge_candidate.latency_ms,
        cost_usd=judge_candidate.cost_usd,
        subscription_calls=judge_candidate.subscription_calls,
    )


def _build_pass_prompt(content: str, rubric: list[str], request: Optional[str] = None) -> str:
    """rubric 충족 여부만 묻는 판정 프롬프트를 만든다.

    **2026-07-29 개편** — 2차 측정에서 불합격 2건이 **둘 다 판정자 문제**로 관측됐고,
    유형이 둘로 갈렸다(1차 때 "정황"이던 것이 재현됐다):

    1. **rubric 밖 요건을 발명한다.** rubric이 `[출처 신뢰성, 핵심 정보 커버리지]`
       뿐인데 "시각 자료가 없다"를 커버리지 필수 요건으로 만들어 불합격시켰다.
       심지어 같은 응답에서 "매우 훌륭하게 작성되었습니다"라고 칭찬했다 — 결함을
       자유롭게 찾은 뒤 rubric에 사후 연결하는 패턴이다.
       → `unmet_items`로 **어느 rubric 항목이 왜 미충족인지 명시하게** 강제한다.
         자유 서술만으로는 이 연결을 건너뛸 수 있다.
    2. **요청된 산출물을 결함으로 오판한다.** 프롬프트가 "검토해줘"라고 요청해서
       답변에 검토 섹션이 있었는데, 판정자가 그걸 "심사자인 내 역할 침범"으로 읽고
       삭제를 지시했다. **판정자가 원본 요청을 못 보기 때문**에 생긴 오판이다.
       → `request`를 함께 준다.

    reject-first(ADR 0004)는 유지한다 — 다만 "결함을 자유롭게 찾아라"에서 **"rubric
    항목별로 충족 여부를 근거와 함께 따져라"**로 좁힌다. 무조건 지시를 내리는 쪽으로
    변질되던 게 문제였고, 판정 대상 없는 개선 아이디어는 판정 근거가 아니다.
    """
    rubric_lines = "\n".join(f"- {item}" for item in rubric) or "- (rubric 없음, 일반적인 품질 기준으로 판단)"
    request_block = (
        f"## 답변이 응답해야 하는 원본 요청\n{request}\n\n"
        "요청이 명시적으로 요구한 내용이 답변에 들어있는 것은 **결함이 아니다** "
        "(예: 요청이 '검토해줘'였다면 답변의 검토 섹션은 요구된 산출물이다).\n\n"
        if request
        else ""
    )
    return (
        "당신은 답변이 주어진 rubric을 충족하는지만 판정하는 심사자다.\n"
        "답변을 다시 쓰거나 형식을 지시하는 사람이 아니다 — rubric 충족 여부 외의 "
        "개선 아이디어는 판정 근거가 아니다.\n\n"
        "## 평가 기준 (rubric) — 판정은 이 항목들로만 한다\n"
        f"{rubric_lines}\n\n"
        f"{request_block}"
        "## 지시사항\n"
        "- rubric **항목마다** 충족/미충족을 근거와 함께 따져라."
        ' "문제 없음"을 기본값으로 삼지 마라.\n'
        "- 미충족 항목이 하나도 없으면 통과다. 사소한 결함은 미충족이 아니다.\n"
        "- **rubric 항목에 해당하지 않는 지적은 쓰지 마라** — 더 좋게 만들 아이디어가"
        " 있어도 그건 판정 대상이 아니다.\n"
        "- `unmet_items`에는 위 rubric 항목의 문구를 그대로 넣어라. 거기 없는 항목을"
        " 새로 만들지 마라.\n"
        "- 통과가 아니라면 feedback에 무엇을 어떻게 고쳐야 하는지 구체적으로 써라"
        " (이 피드백만 보고 답변을 다시 쓸 수 있어야 한다).\n"
        "- 답변 길이로 판단하지 마라 — 길다고 더 나은 답은 아니다.\n"
        "- 아래 JSON 형식으로만 답하라. 다른 텍스트/설명을 앞뒤에 붙이지 마라.\n\n"
        "## 답변\n"
        f"{content}\n\n"
        "## 출력 형식 (JSON)\n"
        '{"unmet_items": ["<미충족 rubric 항목 문구>", ...], '
        '"passed": <true/false>, '
        '"feedback": "<미충족 사유와 수정 지시. 통과면 빈 문자열>"}\n'
    )


def _parse_pass_response(content: str) -> dict:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise JudgeError(f"evaluator 응답에서 JSON을 찾지 못함: {content[:200]!r}")

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise JudgeError(f"evaluator 응답 JSON 파싱 실패: {content[:200]!r}") from exc

    if not isinstance(parsed, dict) or not isinstance(parsed.get("passed"), bool):
        raise JudgeError(f"evaluator 응답에 'passed'(bool)가 없음: {content[:200]!r}")

    feedback = parsed.get("feedback", "")
    if not isinstance(feedback, str):
        feedback = str(feedback)

    raw_items = parsed.get("unmet_items", [])
    unmet = [str(item) for item in raw_items if str(item).strip()] if isinstance(raw_items, list) else []

    # `passed=false`인데 미충족 항목을 하나도 못 대면, 그게 바로 고치려던 실패
    # 유형이다(rubric에 연결되지 않은 불합격). **판정을 뒤집지는 않는다** — 품질
    # 판단 주체는 evaluator라는 게 ADR 0004의 결정이고, 여기서 pass로 바꾸면
    # 하네스가 판정자를 덮어쓰는 셈이다. 대신 눈에 보이게 표시해서 측정 결과와
    # refinement 피드백에서 드러나게 한다.
    if not parsed["passed"] and not unmet:
        feedback = f"[rubric 항목 미지정 불합격 — 판정 신뢰도 확인 필요] {feedback}"

    return {"passed": parsed["passed"], "feedback": feedback, "unmet_rubric_items": unmet}


def _assign_labels(successful: list[Candidate]) -> dict[str, Candidate]:
    """모델명을 숨기고 무작위 순서로 A/B/C... 레이블을 부여한다 (blind 익명화,
    verbosity/position/identity bias 완화 목적, ADR 0004 결정 2번)."""
    shuffled = list(successful)
    random.shuffle(shuffled)
    return {string.ascii_uppercase[i]: candidate for i, candidate in enumerate(shuffled)}


def _build_prompt(rubric: list[str], labels: dict[str, Candidate]) -> str:
    rubric_lines = "\n".join(f"- {item}" for item in rubric) or "- (rubric 없음, 일반적인 품질 기준으로 판단)"
    candidate_blocks = "\n\n".join(f"### 후보 {label}\n{candidate.content}" for label, candidate in labels.items())
    label_keys = ", ".join(f'"{label}"' for label in labels)
    json_shape = ", ".join(f'"{label}": {{"score": <0-100 정수>, "flaws": ["..."]}}' for label in labels)

    return (
        "당신은 여러 LLM이 만든 답변 후보를 비교 평가하는 심사자다.\n\n"
        "## 평가 기준 (rubric)\n"
        f"{rubric_lines}\n\n"
        "## 지시사항\n"
        '- 각 후보의 결함/약점을 근거와 함께 찾아라. "문제 없음"을 기본값으로 삼지 마라.\n'
        "- 답변 길이로 판단하지 마라 — 길다고 더 나은 답은 아니다.\n"
        "- 아래 JSON 형식으로만 답하라. 다른 텍스트/설명을 앞뒤에 붙이지 마라.\n\n"
        "## 후보\n"
        f"{candidate_blocks}\n\n"
        f"## 출력 형식 (JSON, 키는 반드시 {label_keys})\n"
        f"{{{json_shape}}}\n"
    )


def _parse_response(content: str, labels: dict[str, Candidate]) -> dict[str, dict]:
    match = re.search(r"\{.*\}", content, re.DOTALL)
    if not match:
        raise JudgeError(f"judge 응답에서 JSON을 찾지 못함: {content[:200]!r}")

    try:
        parsed = json.loads(match.group(0))
    except json.JSONDecodeError as exc:
        raise JudgeError(f"judge 응답 JSON 파싱 실패: {content[:200]!r}") from exc

    missing = [label for label in labels if label not in parsed]
    if missing:
        raise JudgeError(f"judge 응답에 레이블 누락: {missing} (응답: {content[:200]!r})")

    for label in labels:
        entry = parsed[label]
        if not isinstance(entry, dict) or "score" not in entry:
            raise JudgeError(f"judge 응답의 '{label}' 항목 형식이 잘못됨: {entry!r}")
        entry.setdefault("flaws", [])

    return parsed


def _recommend_strategy(scores: list[JudgingScore]) -> str:
    """최종 답변을 어떻게 만들지 (ADR 0011로 선택지가 둘로 줄었다).

    예전엔 1·2등이 근소하면 `"merge_top_candidates"`를 돌려주고 synthesizer가 상위 두
    후보를 이어붙였다. **그 병합이 산출물을 망쳤다** — final.md가 완결된 문서 두 개가
    되어서, "절차서 하나를 작성해줘"라는 프롬프트에 두 개를 내놓고 불합격했다(7차 측정).
    근소하다는 건 **어느 쪽을 써도 비슷하다**는 뜻이고, 둘을 붙이라는 뜻이 아니었다.

    근소 여부 자체는 `Judging.top_scores_near_tie`로 계속 남는다(`_is_near_tie`).
    """
    return "single_candidate" if len(scores) == 1 else "adopt_winner"


def _is_near_tie(scores: list[JudgingScore]) -> bool:
    """1·2등 점수가 `_MERGE_THRESHOLD` 미만으로 붙어 있나 (후보가 하나면 False)."""
    if len(scores) < 2:
        return False
    top_two = sorted(scores, key=lambda s: s.score, reverse=True)[:2]
    return top_two[0].score - top_two[1].score < _MERGE_THRESHOLD
