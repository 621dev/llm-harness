"""Provider 구현체 전체가 공통 계약을 지키는지 검증 (2026-07-29, ECC 재분석에서 도입).

**왜 필요한가**: 우리 자동 테스트는 규칙상 전부 mock이다(실제 API/CLI 미호출). 그래서
"mock 경로는 통과하는데 실제 구현체가 계약을 어긴다"는 유형이 통째로 사각지대다 —
ECC `ai-regression-testing`이 "AI가 만드는 회귀 1위는 sandbox 경로와 production 경로의
불일치"로 지적하는 것과 같은 구조다. 실제로 이 유형에 이미 데였다:

- `QuotaFallbackProvider`가 wrapper 자신의 `auth_mode`를 보고해서, 구독 2차가 답했는데도
  `subscription_calls` 집계에서 빠졌다(2026-07-28). 계약 검증 테스트가 없어서 측정을
  돌리다 우연히 발견했다.

구현체가 8개까지 늘어난 지금(`ApiProvider`/`GeminiApiProvider`/`CliSubscriptionProvider`/
`ClaudeCliProvider`/`ClaudeAgentProvider`/`CodexCliProvider`/`QuotaFallbackProvider`/
`MockProvider`) 개별 테스트로 흩어놓으면 새 구현체가 조용히 빠진다. 그래서 여기서
**리플렉션으로 구현체를 찾아** 아래를 고정한다:

- 새 구현체가 이 파일의 등록표에 빠지면 **테스트가 실패한다**(핵심 — 드리프트 방지)
- `auth_mode`가 유효한 값이다
- **실패는 `ProviderError`로 던진다** — 2026-07-29부터 `model_runner`의 재시도 분류가
  이걸 전제로 하므로(다른 타입이면 분류 없이 재시도된다) 계약이 하중을 받게 됐다

여기는 **모든 구현체에 공통인 것만** 본다. 구현체 하나에 특수한 계약(예: wrapper가
자기 config가 아니라 답한 쪽의 `auth_mode`를 반영하는지)은 그 구현체 전용 파일에
있다 — `test_quota_fallback_provider.py`.
"""
from __future__ import annotations

import contextlib
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.schemas import ProviderConfig  # noqa: E402
from providers.api_provider import ApiProvider, GeminiApiProvider  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402
from providers.cli_subscription_provider import (  # noqa: E402
    ClaudeAgentProvider,
    ClaudeCliProvider,
    CliSubscriptionProvider,
    CodexCliProvider,
)
from providers.fallback_provider import QuotaFallbackProvider  # noqa: E402
from providers.mock import MockProvider  # noqa: E402

_VALID_AUTH_MODES = {"api_key", "cli_subscription"}


@contextlib.contextmanager
def no_api_keys():
    """API 키 환경변수를 지운 상태를 만든다.

    **이게 없으면 규칙을 어긴다.** `ApiProvider`의 실패 경로는 "키가 없으면 즉시
    ProviderError"인데, 키가 **설정된** 머신에서 돌리면 그 분기를 지나쳐 `requests.post`로
    **실제 API를 호출**한다. 이 프로젝트에서 자동 테스트는 실제 API/CLI를 절대 호출하지
    않으므로 여기서 명시적으로 비운다 — 개발 머신에 키가 있는지 없는지에 테스트 성격이
    달라지면 안 된다.
    """
    api_key_vars = [
        cls.api_key_env_var
        for cls in (ApiProvider, *_all_subclasses(ApiProvider))
        if cls.api_key_env_var
    ]
    with mock.patch.dict(os.environ, {var: "" for var in api_key_vars}):
        yield


def _all_subclasses(cls: type) -> list[type]:
    direct = cls.__subclasses__()
    return direct + [nested for sub in direct for nested in _all_subclasses(sub)]


def api_config(provider_id: str = "some-api") -> ProviderConfig:
    return ProviderConfig(provider_id=provider_id, model_id=provider_id, auth_mode="api_key")


def sub_config(provider_id: str = "some-cli") -> ProviderConfig:
    return ProviderConfig(
        provider_id=provider_id, model_id=provider_id, auth_mode="cli_subscription"
    )


def failing_cli(cls: type[CliSubscriptionProvider]) -> CliSubscriptionProvider:
    """실제 CLI를 부르지 않고 실패 경로만 타게 만든다.

    `executable`을 PATH에 없는 이름으로 바꾸면 `_resolve_executable()`이
    `shutil.which()` 단계에서 `ProviderError`를 던진다 — subprocess는 시작조차
    하지 않으므로 "자동 테스트는 실제 CLI 미호출" 규칙을 지킨다.
    """
    provider = cls(sub_config(cls.__name__))
    provider.executable = "definitely-not-a-real-binary-xyz"
    return provider


# 실제로 호출부(cli.py)가 등록해서 쓰는 구현체 — (만드는 법, 실패하게 만드는 법).
# 전체 계약을 적용한다.
_CONCRETE: dict[type[Provider], tuple[object, object]] = {
    GeminiApiProvider: (
        lambda: GeminiApiProvider(api_config("gemini")),
        # 환경변수 미설정 → 인증 실패 경로(실제 API 호출 없음)
        lambda: GeminiApiProvider(api_config("gemini")),
    ),
    ClaudeCliProvider: (
        lambda: ClaudeCliProvider(sub_config("claude")),
        lambda: failing_cli(ClaudeCliProvider),
    ),
    ClaudeAgentProvider: (
        lambda: ClaudeAgentProvider(sub_config("claude-agent")),
        lambda: failing_cli(ClaudeAgentProvider),
    ),
    CodexCliProvider: (
        lambda: CodexCliProvider(sub_config("codex")),
        lambda: failing_cli(CodexCliProvider),
    ),
    MockProvider: (
        lambda: MockProvider(api_config("mock")),
        lambda: MockProvider(api_config("mock"), fail_times=99),
    ),
    QuotaFallbackProvider: (
        lambda: QuotaFallbackProvider(
            MockProvider(api_config("primary")),
            MockProvider(sub_config("fallback")),
            api_config("wrapper"),
        ),
        # 1차가 quota가 아닌 이유로 실패하면 그대로 전파돼야 한다
        lambda: QuotaFallbackProvider(
            MockProvider(api_config("primary"), fail_times=99),
            MockProvider(sub_config("fallback")),
            api_config("wrapper"),
        ),
    ),
}

# 서브클래스가 채워야 하는 중간 기반 클래스. 직접 등록해서 쓰이지 않으므로 실패
# 계약은 적용하지 않는다 — 이 테스트를 처음 돌렸을 때 `CliSubscriptionProvider`가
# `_invoke`의 `NotImplementedError`로 걸렸고, 그게 버그가 아니라 "이 클래스는 완성된
# provider가 아니다"라는 뜻이었다. 대신 정체성/auth_mode 계약은 상속되므로 같이 본다.
#
# 알려진 구멍: 둘 다 인스턴스화 자체는 막지 않으므로(추상 메서드 대신 런타임
# `NotImplementedError`), 실수로 cli.py에 직접 등록하면 분류 안 되는 예외가 뜬다.
# 지금은 등록 지점이 한 곳뿐이라 감수하고, 구현체가 더 늘면 `@abstractmethod`로
# 승격할 것.
_ABSTRACT_BASES: dict[type[Provider], object] = {
    ApiProvider: lambda: ApiProvider(api_config("bare-api")),
    CliSubscriptionProvider: lambda: CliSubscriptionProvider(sub_config("bare-cli")),
}

_ALL = {**{cls: make for cls, (make, _) in _CONCRETE.items()}, **_ABSTRACT_BASES}


def concrete_provider_classes() -> set[type[Provider]]:
    """`Provider`의 모든 하위 클래스를 재귀로 모은다(중간 기반 클래스 포함)."""
    found: set[type[Provider]] = set()

    def walk(cls: type[Provider]) -> None:
        for sub in cls.__subclasses__():
            found.add(sub)
            walk(sub)

    walk(Provider)
    # 테스트 파일들이 정의한 fake/stub은 계약 검증 대상이 아니다 — src/providers 소속만 본다.
    return {cls for cls in found if cls.__module__.startswith("providers.")}


class RegistryCoversEveryImplementationTest(unittest.TestCase):
    """이 테스트가 이 파일의 존재 이유다 — 새 구현체가 조용히 빠지는 것을 막는다."""

    def test_no_implementation_is_missing_from_registry(self) -> None:
        missing = concrete_provider_classes() - set(_ALL)
        self.assertEqual(
            missing,
            set(),
            f"Provider 구현체가 새로 생겼는데 계약 테스트 등록표에 없다: "
            f"{sorted(cls.__name__ for cls in missing)}. "
            f"tests/test_provider_contract.py의 _CONCRETE(또는 기반 클래스면 "
            f"_ABSTRACT_BASES)에 추가할 것.",
        )

    def test_registry_has_no_stale_entries(self) -> None:
        """구현체가 삭제됐는데 등록표만 남는 반대 방향도 막는다."""
        stale = set(_ALL) - concrete_provider_classes()
        self.assertEqual(stale, set(), f"{sorted(cls.__name__ for cls in stale)}")


class ProviderContractTest(unittest.TestCase):
    def test_auth_mode_is_a_known_value(self) -> None:
        for cls, make in _ALL.items():
            with self.subTest(provider=cls.__name__):
                self.assertIn(make().auth_mode, _VALID_AUTH_MODES)

    def test_identity_fields_are_non_empty(self) -> None:
        """provider_id/model_id는 run 기록과 지표 집계의 키라 빈 값이면 안 된다."""
        for cls, make in _ALL.items():
            with self.subTest(provider=cls.__name__):
                provider = make()
                self.assertTrue(provider.provider_id)
                self.assertTrue(provider.model_id)

    def test_failure_raises_provider_error(self) -> None:
        """model_runner의 재시도 분류가 이 계약에 하중을 걸고 있다(2026-07-29).

        `ProviderError`가 아닌 예외로 실패하면 `_is_retryable()`이 판단 근거를 못 찾아
        분류 없이 재시도한다 — 한도 초과에 재시도를 얹지 않으려고 만든 장치가 무력화된다.
        """
        for cls, (_, make_failing) in _CONCRETE.items():
            with self.subTest(provider=cls.__name__), no_api_keys():
                with self.assertRaises(ProviderError):
                    make_failing().generate("아무 프롬프트")

    def test_generate_is_actually_implemented(self) -> None:
        """추상 메서드를 상속만 받고 구현을 안 한 구현체가 없어야 한다."""
        for cls in _ALL:
            with self.subTest(provider=cls.__name__):
                self.assertIsNot(cls.generate, Provider.generate)


if __name__ == "__main__":
    unittest.main()
