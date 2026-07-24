"""Synthesizer: 최종 답변 합성 (Fan-out/Judge 패턴 전용, Step 6).

harness-implementation-plan-ko.md Section 7 Step 6을 구현한다.

judging.recommended_strategy에 따라 winner 후보를 그대로 채택하거나("adopt_winner"),
상위 두 후보의 내용을 이어붙이는 간단한 병합 템플릿을 쓴다("merge_top_candidates").
진짜 LLM 합성이 아니라 규칙 기반 mock 합성이다.
"""
from __future__ import annotations

from .schemas import Candidate, Judging


def synthesize(candidates: list[Candidate], judging: Judging) -> str:
    by_model = {c.model_id: c for c in candidates}
    winner = by_model[judging.winner]

    if judging.recommended_strategy == "merge_top_candidates":
        top_two = sorted(judging.scores, key=lambda s: s.score, reverse=True)[:2]
        parts = [by_model[s.candidate].content for s in top_two if s.candidate in by_model]
        if len(parts) > 1:
            return "\n\n---\n\n".join(parts)

    return winner.content
