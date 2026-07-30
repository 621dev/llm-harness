"""Synthesizer: 최종 답변 합성 (Fan-out/Judge 패턴 전용, Step 6).

harness-implementation-plan-ko.md Section 7 Step 6을 구현한다.

**지금은 승자 후보를 그대로 채택하는 것뿐이다**(ADR 0011). 예전엔 1·2등 점수가
근소하면 상위 두 후보를 `---`로 이어붙이는 "병합" 전략이 있었는데, 그게 산출물을
망쳤다 — final.md가 **완결된 문서 두 개**가 되어서, "진단 절차서를 작성해줘"(단수)라는
프롬프트에 절차서 둘을 내놓고 불합격했다(7차 측정, 12개 run 중 9개가 병합됐다).

근소하다는 건 **어느 쪽을 써도 비슷하다**는 뜻이고 둘을 붙이라는 뜻이 아니었다.
근소 여부 자체는 `Judging.top_scores_near_tie`로 계속 남는다.

이 모듈이 한 줄로 줄어든 게 어색해 보일 수 있지만 그대로 둔다 — orchestrator가
"후보들에서 최종 답변을 만든다"는 단계를 이름으로 갖고 있는 게 낫고, 실제 LLM 합성이
필요해지면(ADR 0011의 기각된 대안) 들어올 자리가 여기다.
"""
from __future__ import annotations

from .schemas import Candidate, Judging


def synthesize(candidates: list[Candidate], judging: Judging) -> str:
    return {c.model_id: c for c in candidates}[judging.winner].content
