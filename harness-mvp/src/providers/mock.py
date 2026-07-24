"""결정적 mock Provider (Step 2).

harness-implementation-plan-ko.md Section 7 Step 2: "결정적 응답 3종 생성 (모델별 다른
강점 시뮬레이션)". 실제 LLM을 호출하지 않고, 같은 prompt에는 항상 같은 응답을 내서
model_runner/orchestrator 파이프라인을 재현 가능하게 검증하기 위한 용도다.

fail_times로 처음 N번 호출을 실패시킬 수 있어, model_runner의 재시도/복구 로직을
실패 주입으로 테스트할 수 있다.
"""
from __future__ import annotations

import json
import re

from harness.schemas import Candidate, ProviderConfig

from .base import Provider, ProviderError

# 모델별로 다른 강점을 시뮬레이션한다 (revfactory/harness의 Fan-out 비교 취지에 맞춰,
# 후보들이 서로 다른 관점을 대표하도록 함).
PROFILES: dict[str, str] = {
    "concise": "핵심만 짧게 정리한 답변",
    "detailed": "배경과 근거를 함께 제시하는 상세한 답변",
    "creative": "기존과 다른 관점을 제안하는 답변",
}

# judge.py가 보내는 프롬프트의 "### 후보 <레이블>\n<내용>" 블록을 찾는다
# (judge.py의 _build_prompt와 짝을 이루는 테스트 전용 파싱 — ADR 0004 참고).
_JUDGE_CANDIDATE_BLOCK_RE = re.compile(r"### 후보 (\S+)\n(.*?)\n\n(?=### 후보 |## |\Z)", re.DOTALL)


class MockProvider(Provider):
    """결정적 mock 응답을 내는 provider. 실제 API/CLI 호출 없이 파이프라인 검증용.

    profile: PROFILES 중 하나, 또는 "judge"(judge.py의 실제 LLM 호출 지점을
    대신하는 결정적 판정 대역 — 아래 `_judge_response` 참고). 모르는 값이면
    "concise"로 취급한다.
    fail_times: generate() 처음 N번 호출에서 ProviderError를 던진다 (재시도 테스트용).
    """

    _VALID_PROFILES = frozenset(PROFILES) | {"judge"}

    def __init__(self, config: ProviderConfig, *, profile: str = "concise", fail_times: int = 0) -> None:
        super().__init__(config)
        self.profile = profile if profile in self._VALID_PROFILES else "concise"
        self.fail_times = fail_times
        self.call_count = 0

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise ProviderError(
                f"{self.provider_id} mock 실패 주입 ({self.call_count}/{self.fail_times})"
            )

        if self.profile == "judge":
            return self._judge_response(prompt)

        style = PROFILES[self.profile]
        content = f"[{self.model_id} / {self.profile}] {style}\n\n요청: {prompt}"
        # cost_usd는 auth_mode="api_key"일 때만 채운다 (schemas.py Candidate 문서 참고).
        cost_usd = round(0.0002 * len(content), 6) if self.config.auth_mode == "api_key" else None

        return Candidate(
            model_id=self.model_id,
            content=content,
            tokens=len(content.split()),
            latency_ms=10,
            cost_usd=cost_usd,
            status="success",
        )

    def _judge_response(self, prompt: str) -> Candidate:
        """judge.py가 기대하는 JSON 형식으로 결정적 판정을 돌려준다.

        judge.py의 호출/파싱 로직(레이블 매핑, JudgeError 처리 등)을 실제 LLM
        없이 검증하기 위한 대역이다 — 길이 기반 점수는 이 mock 자체의 결정성
        확보용일 뿐, 편향 회피는 이 mock의 책임이 아니다(실제 판단 품질은
        수동 e2e 검증으로 확인, ADR 0004).
        """
        blocks = _JUDGE_CANDIDATE_BLOCK_RE.findall(prompt)
        result = {
            label: {
                "score": min(len(text.strip()), 100),
                "flaws": [] if len(text.strip()) > 20 else ["내용이 부실함"],
            }
            for label, text in blocks
        }
        content = json.dumps(result, ensure_ascii=False)
        return Candidate(
            model_id=self.model_id,
            content=content,
            tokens=len(content.split()),
            latency_ms=5,
            cost_usd=None,
            status="success",
        )
