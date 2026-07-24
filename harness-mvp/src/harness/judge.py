"""Judge: 후보 비교 평가 (Fan-out/Judge 패턴 전용, ADR 0004).

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

from . import model_runner
from .schemas import Judging, JudgingScore, Candidate

_MERGE_THRESHOLD = 0.1  # 1·2등 점수 차이가 이 값(0~1 스케일) 미만이면 병합 전략 추천


class JudgeError(RuntimeError):
    """judge_provider 호출이 끝내 실패했거나 응답을 파싱할 수 없다."""


def evaluate(candidates: list[Candidate], rubric: list[str], judge_provider: Provider) -> Judging:
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

    judge_candidate = model_runner.generate_with_retry(judge_provider, prompt, temperature=0.0)
    if judge_candidate.status == "error":
        raise JudgeError(f"judge 호출 실패: {judge_candidate.content}")

    parsed = _parse_response(judge_candidate.content, labels)

    scores = [
        JudgingScore(
            candidate=candidate.model_id,
            score=parsed[label]["score"] / 100.0,
            strengths=[],
            weaknesses=parsed[label]["flaws"],
        )
        for label, candidate in labels.items()
    ]
    winner = max(scores, key=lambda s: s.score)

    return Judging(
        scores=scores,
        recommended_strategy=_recommend_strategy(scores),
        winner=winner.candidate,
        latency_ms=judge_candidate.latency_ms,
        cost_usd=judge_candidate.cost_usd,
    )


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
    if len(scores) == 1:
        return "single_candidate"

    top_two = sorted(scores, key=lambda s: s.score, reverse=True)[:2]
    if top_two[0].score - top_two[1].score < _MERGE_THRESHOLD:
        return "merge_top_candidates"
    return "adopt_winner"
