"""QuotaFallbackProvider 테스트 (2026-07-27).

server-engineering-learning 도메인에서 Gemini 무료 티어 일일 한도(20회)를
반복 소진하며, 사람이 매번 config.json을 codex로 고쳤다 되돌리는 수작업을
반복한 것을 자동화하려고 도입했다. 검증하는 것:
- quota 오류(is_quota_error=True)일 때만 2차 provider로 전환하는가
- quota가 아닌 실패(진짜 버그일 수 있는 것)는 그대로 전파하는가(폴백 대상을
  넓히면 "실패를 조용히 감추지 않는다"는 원칙이 무의미해짐)
- 전환 여부를 `used_fallback`으로 관측할 수 있는가
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.schemas import Candidate, ProviderConfig  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402
from providers.fallback_provider import QuotaFallbackProvider  # noqa: E402


class _StubProvider(Provider):
    """호출 결과나 예외를 그대로 지정할 수 있는 최소 provider."""

    def __init__(self, provider_id: str, *, result=None, error: ProviderError | None = None):
        super().__init__(ProviderConfig(provider_id=provider_id, model_id=provider_id))
        self.result = result
        self.error = error
        self.call_count = 0

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        self.call_count += 1
        if self.error is not None:
            raise self.error
        return self.result


def make_candidate(model_id: str) -> Candidate:
    return Candidate(model_id=model_id, content=f"{model_id} 응답", status="success")


class QuotaFallbackProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.config = ProviderConfig(provider_id="research-mock", model_id="gemini-2.5-flash")

    def test_quota_error_switches_to_fallback(self) -> None:
        primary = _StubProvider("gemini", error=ProviderError("429 quota exceeded", is_quota_error=True))
        fallback = _StubProvider("codex", result=make_candidate("codex-cli"))
        provider = QuotaFallbackProvider(primary=primary, fallback=fallback, config=self.config)

        candidate = provider.generate("리서치해줘")

        self.assertEqual(candidate.model_id, "codex-cli")
        self.assertTrue(provider.used_fallback)
        self.assertEqual(primary.call_count, 1)
        self.assertEqual(fallback.call_count, 1)

    def test_non_quota_error_propagates_without_fallback(self) -> None:
        """진짜 버그일 수 있는 실패(응답 형식 오류 등)는 폴백하지 않고 그대로 전파한다."""
        primary = _StubProvider("gemini", error=ProviderError("응답 형식이 예상과 다름"))
        fallback = _StubProvider("codex", result=make_candidate("codex-cli"))
        provider = QuotaFallbackProvider(primary=primary, fallback=fallback, config=self.config)

        with self.assertRaises(ProviderError):
            provider.generate("리서치해줘")

        self.assertFalse(provider.used_fallback)
        self.assertEqual(fallback.call_count, 0)  # 폴백 자체가 호출 안 됨

    def test_auth_mode_follows_the_provider_that_actually_answered(self) -> None:
        """회귀 테스트(2026-07-28 실측): wrapper가 자기 config의 auth_mode를 그대로
        보고하면, **종량제 1차가 한도로 실패해 구독 2차가 답했는데도 auth_mode가
        "api_key"로 남아** model_runner의 `subscription_calls` 집계에서 그 호출이
        빠진다. 구독 한도를 실제로 소모했는데 지표에 안 보이면 Section 9
        "Cost Blindness 방지"가 깨진다."""
        primary = _StubProvider("gemini", error=ProviderError("429", is_quota_error=True))
        primary.config.auth_mode = "api_key"
        fallback = _StubProvider("claude", result=make_candidate("claude-cli"))
        fallback.config.auth_mode = "cli_subscription"
        # 호출부(cli._wrap_with_quota_fallback)는 1차 기준 config를 넘긴다
        provider = QuotaFallbackProvider(
            primary=primary,
            fallback=fallback,
            config=ProviderConfig(provider_id="research-mock", model_id="gemini-2.5-flash"),
        )

        provider.generate("리서치해줘")

        self.assertEqual(provider.auth_mode, "cli_subscription")  # 답한 쪽을 따라간다

    def test_auth_mode_stays_on_primary_when_no_fallback(self) -> None:
        primary = _StubProvider("claude", result=make_candidate("claude-cli"))
        primary.config.auth_mode = "cli_subscription"
        fallback = _StubProvider("codex", result=make_candidate("codex-cli"))
        provider = QuotaFallbackProvider(primary=primary, fallback=fallback, config=self.config)

        provider.generate("리서치해줘")

        self.assertEqual(provider.auth_mode, "cli_subscription")

    def test_success_does_not_touch_fallback(self) -> None:
        primary = _StubProvider("gemini", result=make_candidate("gemini-2.5-flash"))
        fallback = _StubProvider("codex", result=make_candidate("codex-cli"))
        provider = QuotaFallbackProvider(primary=primary, fallback=fallback, config=self.config)

        candidate = provider.generate("리서치해줘")

        self.assertEqual(candidate.model_id, "gemini-2.5-flash")
        self.assertFalse(provider.used_fallback)
        self.assertEqual(fallback.call_count, 0)


if __name__ == "__main__":
    unittest.main()
