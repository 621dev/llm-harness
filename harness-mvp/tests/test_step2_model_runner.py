"""Step 2 테스트: mock provider + model_runner (stdlib unittest, 외부 패키지 불필요).

harness-implementation-plan-ko.md Section 7 Step 2 체크리스트를 검증한다.
- 3개 mock provider가 서로 다른 프로필로 결정적 응답을 내는가
- model_runner.run_all이 provider마다 artifacts/candidates/<model_id>.md를 저장하는가
- 실패 주입: 1회 실패 후 재시도로 복구되는가, 재시도까지 다 실패하면 error 후보로
  기록되고 나머지 provider는 계속 진행되는가 (Section 6 복구 전략)
- 동일 prompt로 재실행 시 동일 결과가 나오는가 (재현성)

실행: python -m unittest tests/test_step2_model_runner.py -v (harness-mvp 디렉토리에서)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import model_runner, run_store  # noqa: E402
from harness.schemas import ProviderConfig  # noqa: E402
from providers.base import Provider, ProviderError  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def make_providers(*, fail_times: dict[str, int] | None = None) -> list[MockProvider]:
    fail_times = fail_times or {}
    specs = [
        ("model-a", "concise"),
        ("model-b", "detailed"),
        ("model-c", "creative"),
    ]
    return [
        MockProvider(
            ProviderConfig(provider_id=provider_id, model_id=provider_id),
            profile=profile,
            fail_times=fail_times.get(provider_id, 0),
        )
        for provider_id, profile in specs
    ]


class ModelRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-model-runner-test", root=self.tmp_dir)

    def test_run_all_generates_one_candidate_per_provider_with_distinct_profiles(self) -> None:
        providers = make_providers()

        candidates = model_runner.run_all("설계안을 검토해줘", providers, self.run_dir)

        self.assertEqual(len(candidates), 3)
        self.assertEqual([c.status for c in candidates], ["success", "success", "success"])
        self.assertTrue(all("설계안을 검토해줘" in c.content for c in candidates))
        # 세 후보의 내용이 서로 달라야 한다 (모델별 다른 강점 시뮬레이션)
        contents = {c.content for c in candidates}
        self.assertEqual(len(contents), 3)

        for candidate in candidates:
            candidate_path = self.run_dir / "artifacts" / "candidates" / f"{candidate.model_id}.md"
            self.assertTrue(candidate_path.exists())
            self.assertIn(candidate.content, candidate_path.read_text(encoding="utf-8"))

    def test_provider_recovers_after_one_retry(self) -> None:
        providers = make_providers(fail_times={"model-a": 1})  # 1회 실패 후 재시도로 성공

        candidates = model_runner.run_all("리서치해줘", providers, self.run_dir)

        candidate_a = next(c for c in candidates if c.model_id == "model-a")
        self.assertEqual(candidate_a.status, "success")
        self.assertEqual(providers[0].call_count, 2)  # 최초 호출 + 재시도 1회

    def test_provider_exhausts_retry_records_error_and_others_continue(self) -> None:
        # model-b는 재시도(1회)까지 다 실패하는 상황을 주입한다 (MAX_RETRIES=1 이므로 총 2번 호출).
        providers = make_providers(fail_times={"model-b": 2})

        candidates = model_runner.run_all("리서치해줘", providers, self.run_dir)

        statuses = {c.model_id: c.status for c in candidates}
        self.assertEqual(statuses["model-a"], "success")
        self.assertEqual(statuses["model-b"], "error")
        self.assertEqual(statuses["model-c"], "success")  # 한 provider의 실패가 나머지를 막지 않음
        self.assertEqual(providers[1].call_count, 2)

        error_candidate = next(c for c in candidates if c.model_id == "model-b")
        error_path = self.run_dir / "artifacts" / "candidates" / "model-b.md"
        self.assertIn("error", error_path.read_text(encoding="utf-8"))
        self.assertIsNone(error_candidate.tokens)
        self.assertIsNone(error_candidate.cost_usd)

    def test_rerun_same_prompt_is_reproducible(self) -> None:
        providers_first = make_providers()
        first = model_runner.run_all("동일 프롬프트", providers_first, self.run_dir)

        providers_second = make_providers()
        second = model_runner.run_all("동일 프롬프트", providers_second, self.run_dir)

        self.assertEqual([c.content for c in first], [c.content for c in second])

    def test_cost_usd_only_filled_for_api_key_auth_mode(self) -> None:
        cli_provider = MockProvider(
            ProviderConfig(provider_id="model-a", model_id="model-a", auth_mode="cli_subscription"),
            profile="concise",
        )
        api_provider = MockProvider(
            ProviderConfig(provider_id="model-b", model_id="model-b", auth_mode="api_key"),
            profile="concise",
        )

        candidates = model_runner.run_all("비용 필드 확인", [cli_provider, api_provider], self.run_dir)

        self.assertIsNone(candidates[0].cost_usd)
        self.assertIsNotNone(candidates[1].cost_usd)


class RetryClassificationTest(unittest.TestCase):
    """재시도가 무의미한 실패는 재시도하지 않는다 (2026-07-29, ECC 재분석에서 나온 결함 수정).

    고치기 전: `except Exception`으로 전부 잡아 무조건 1회 재시도했다. 한도 초과(429)에
    재시도를 얹으면 **이미 소진된 한도에 호출을 한 번 더 던지고**, 인증 실패에 얹으면
    지연만 2배가 된다. Gemini 무료 티어 일 20회로 측정이 막힌 게 실제 경험이라 가장
    아픈 자리에 낭비를 더하고 있었다.

    판정은 발생 지점이 표시한 구조적 신호(`ProviderError.is_retryable`)만 본다 —
    에러 메시지 문구 매칭은 이 프로젝트에서 이미 세 번 데인 방식이다.
    """

    def make_provider(self, *, auth_mode: str = "cli_subscription", **error_flags) -> object:
        """지정한 플래그로 매번 실패하는 provider. 호출 횟수를 센다."""

        class _AlwaysFailing(Provider):
            def __init__(inner) -> None:  # noqa: N805 - 바깥 self와 구분하려고 inner
                super().__init__(
                    ProviderConfig(provider_id="p", model_id="p", auth_mode=auth_mode)
                )
                inner.calls = 0

            def generate(inner, prompt: str, *, temperature: float = 0.7):  # noqa: N805
                inner.calls += 1
                raise ProviderError("실패 주입", **error_flags)

        return _AlwaysFailing()

    def test_quota_error_is_not_retried(self) -> None:
        """핵심 회귀 방지: 소진된 한도에 두 번째 호출을 던지지 않는다."""
        provider = self.make_provider(is_quota_error=True)

        candidate = model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(provider.calls, 1)
        self.assertEqual(candidate.status, "error")
        # 실제로 소모한 만큼만 기록돼야 한다 — 예전엔 2회였다
        self.assertEqual(candidate.subscription_calls, 1)

    def test_auth_error_is_not_retried(self) -> None:
        """키가 틀린 건 두 번 해도 틀리다."""
        provider = self.make_provider(is_auth_error=True)

        model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(provider.calls, 1)

    def test_unflagged_failure_is_still_retried(self) -> None:
        """일시적 실패로 볼 근거가 있는 것은 기존대로 재시도한다(동작 변경 아님)."""
        provider = self.make_provider()

        model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(provider.calls, model_runner.MAX_RETRIES + 1)

    def test_non_provider_error_is_still_retried(self) -> None:
        """ProviderError가 아닌 예외는 판단 근거가 없어 재시도한다 — 상한이 낭비를 막는다."""

        class _Broken(Provider):
            def __init__(inner) -> None:  # noqa: N805
                super().__init__(ProviderConfig(provider_id="p", model_id="p"))
                inner.calls = 0

            def generate(inner, prompt: str, *, temperature: float = 0.7):  # noqa: N805
                inner.calls += 1
                raise RuntimeError("계약을 어긴 예외")

        provider = _Broken()

        candidate = model_runner.generate_with_retry(provider, "질문")

        self.assertEqual(provider.calls, model_runner.MAX_RETRIES + 1)
        self.assertEqual(candidate.status, "error")

    def test_all_attempt_errors_are_kept(self) -> None:
        """시도별 원인을 전부 남긴다 — 예전엔 덮어써서 1차 실패 원인이 사라졌다."""

        class _DifferentEachTime(Provider):
            def __init__(inner) -> None:  # noqa: N805
                super().__init__(ProviderConfig(provider_id="p", model_id="p"))
                inner.calls = 0

            def generate(inner, prompt: str, *, temperature: float = 0.7):  # noqa: N805
                inner.calls += 1
                raise ProviderError(f"{inner.calls}번째 원인")

        candidate = model_runner.generate_with_retry(_DifferentEachTime(), "질문")

        self.assertIn("1번째 원인", candidate.content)
        self.assertIn("2번째 원인", candidate.content)

    def test_single_attempt_message_has_no_attempt_prefix(self) -> None:
        """재시도가 없었으면 '1차:' 같은 접두어로 소음을 만들지 않는다."""
        provider = self.make_provider(is_quota_error=True)

        candidate = model_runner.generate_with_retry(provider, "질문")

        self.assertNotIn("1차:", candidate.content)


if __name__ == "__main__":
    unittest.main()
