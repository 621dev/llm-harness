"""Quota Fallback Provider (2026-07-27).

하네스는 지금까지 "provider가 실패하면 mock 등으로 몰래 대체하지 않는다"는
원칙을 지켜왔다(README.md: "하나라도 없으면 그 provider만 재시도 후
status='error'로 기록 — 나머지 provider로 계속 진행, 별도 mock fallback
없음"). 실패를 조용히 감추면 진짜 문제를 놓칠 수 있어서다.

`QuotaFallbackProvider`는 그 원칙의 유일한 의도적 예외다. 대상은 **호출 한도
소진(quota/rate-limit)** 하나로 좁힌다 — 이건 코드나 프롬프트의 문제가 아니라
외부 서비스의 운영 제약이라, 사람이 매번 config.json을 고쳤다 되돌리는 수작업을
반복할 이유가 없다(2026-07-27 server-engineering-learning 도메인에서 Gemini
무료 티어 20회/일 한도를 반복 소진하며 실제로 겪음). 그 외 실패(응답 형식
오류, 인증 실패 등 진짜 버그일 수 있는 것)는 그대로 전파한다 — 대체 대상을
넓히면 이 원칙 자체가 무의미해진다.

`used_fallback`은 실제로 전환이 일어났는지 관측용으로 남긴다 — Section 9
"Cost Blindness 방지"와 같은 철학: 무슨 일이 있었는지 절대 숨기지 않는다.
"""
from __future__ import annotations

from harness.schemas import Candidate, ProviderConfig

from .base import Provider, ProviderError


class QuotaFallbackProvider(Provider):
    """1차 provider가 quota 오류(`ProviderError.is_quota_error`)로 실패하면
    2차 provider로 즉시 전환한다. 재시도는 하지 않고 곧바로 넘어간다 — 한도
    소진은 몇 초 재시도한다고 풀리는 문제가 아니므로(2026-07-27 실측: 30초/90초
    대기 후 재시도해도 계속 같은 429), 1차의 재시도 예산을 낭비하지 않는다.
    """

    def __init__(self, primary: Provider, fallback: Provider, config: ProviderConfig) -> None:
        super().__init__(config)
        self.primary = primary
        self.fallback = fallback
        self.used_fallback = False
        self._last_used = primary

    @property
    def auth_mode(self) -> str:
        """**실제로 답한 쪽**의 인증 모드를 돌려준다.

        wrapper 자신의 config(호출부가 만든 것)를 그대로 쓰면, 종량제 1차가 한도로
        실패해 **구독 2차가 답했는데도 auth_mode가 "api_key"로 남아** model_runner의
        `subscription_calls` 집계에서 그 호출이 빠진다(2026-07-28 실측). 구독 한도를
        실제로 소모했는데 지표에 안 보이면 Section 9 "Cost Blindness 방지"가
        깨지므로, 답한 쪽을 따라간다.
        """
        return self._last_used.auth_mode

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        try:
            candidate = self.primary.generate(prompt, temperature=temperature)
            self._last_used = self.primary
            return candidate
        except ProviderError as exc:
            if not exc.is_quota_error:
                raise
            self.used_fallback = True
            self._last_used = self.fallback
            return self.fallback.generate(prompt, temperature=temperature)
