"""Phase 3 테스트: providers/api_provider.py (stdlib unittest).

harness-implementation-plan-ko.md Section 10을 검증한다. 실제 API를 호출하면 진짜
과금이 발생하므로, 여기서는 `requests.post`를 모킹해서 파싱/에러 처리 로직만
검증한다. 실제 Gemini API 연동 자체는 이 기능을 만들면서 수동으로 직접 호출해
확인했다(진행상황 문서 참고).
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness.schemas import ProviderConfig  # noqa: E402
from providers.api_provider import GeminiApiProvider  # noqa: E402
from providers.base import ProviderError  # noqa: E402


def make_config() -> ProviderConfig:
    return ProviderConfig(provider_id="gemini-api", model_id="gemini-2.5-flash", auth_mode="api_key")


def make_response(status_code: int, json_data: dict, *, text: str = "") -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.json.return_value = json_data
    response.text = text or str(json_data)
    return response


class GeminiApiProviderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.provider = GeminiApiProvider(make_config())
        env_patcher = patch.dict("os.environ", {"GEMINI_API_KEY": "fake-key-for-tests"})
        env_patcher.start()
        self.addCleanup(env_patcher.stop)

    @patch("providers.api_provider.requests.post")
    def test_success_parses_content_and_tokens(self, mock_post) -> None:
        mock_post.return_value = make_response(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "2"}]}}],
                "usageMetadata": {"candidatesTokenCount": 1},
            },
        )

        candidate = self.provider.generate("1+1은?")

        self.assertEqual(candidate.status, "success")
        self.assertEqual(candidate.content, "2")
        self.assertEqual(candidate.tokens, 1)
        self.assertIsNotNone(candidate.cost_usd)  # api_key 모드는 cost_usd를 채움
        self.assertGreaterEqual(candidate.latency_ms, 0)

    @patch("providers.api_provider.requests.post")
    def test_concatenates_multiple_parts(self, mock_post) -> None:
        mock_post.return_value = make_response(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "안녕"}, {"text": "하세요"}]}}],
                "usageMetadata": {"candidatesTokenCount": 4},
            },
        )

        candidate = self.provider.generate("인사해줘")

        self.assertEqual(candidate.content, "안녕하세요")

    @patch("providers.api_provider.requests.post")
    def test_input_tokens_are_counted_into_cost(self, mock_post) -> None:
        """입력 토큰이 비용에 반영되는지 (2026-07-29 수정한 누락).

        그전까지 `candidatesTokenCount`(출력)만 세서, **체인처럼 입력이 큰 패턴의 비용이
        통째로 과소 집계**됐다 — 3단계 체인은 스텝마다 이전 결과를 전부 받아 입력이
        direct_call의 90배가 넘는데(실측) cost_usd에는 0원으로 반영됐다.
        종량제 키에서는 입력도 실제 청구 대상이고, `budget_usd` 상한이 이 값을 근거로
        동작하므로 빠지면 상한이 헐거워진다.
        """
        mock_post.return_value = make_response(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "답"}]}}],
                "usageMetadata": {"candidatesTokenCount": 100, "promptTokenCount": 10_000},
            },
        )

        candidate = self.provider.generate("아주 긴 프롬프트")

        self.assertEqual(candidate.input_tokens, 10_000)
        # 출력 100토큰만 세면 무시할 금액인데, 입력 10,000토큰이 들어가면 훨씬 커진다
        output_only = 100 * GeminiApiProvider._COST_PER_OUTPUT_TOKEN_USD
        self.assertGreater(candidate.cost_usd, output_only)

    @patch("providers.api_provider.requests.post")
    def test_cost_survives_a_missing_token_field(self, mock_post) -> None:
        """한 필드가 사라져도 비용이 통째로 None이 되면 budget 상한이 아무것도 못 막는다."""
        mock_post.return_value = make_response(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "답"}]}}],
                "usageMetadata": {"promptTokenCount": 500},  # 출력 토큰 없음
            },
        )

        candidate = self.provider.generate("질문")

        self.assertIsNone(candidate.tokens)
        self.assertIsNotNone(candidate.cost_usd)  # 있는 쪽만으로 계산한다

    @patch("providers.api_provider.requests.post")
    def test_cost_is_none_only_when_both_are_missing(self, mock_post) -> None:
        mock_post.return_value = make_response(
            200, {"candidates": [{"content": {"parts": [{"text": "답"}]}}], "usageMetadata": {}}
        )

        self.assertIsNone(self.provider.generate("질문").cost_usd)

    def test_missing_api_key_raises(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaises(ProviderError):
                self.provider.generate("1+1은?")

    @patch("providers.api_provider.requests.post")
    def test_non_200_status_raises_with_error_message(self, mock_post) -> None:
        mock_post.return_value = make_response(
            400, {"error": {"code": 400, "message": "API key not valid."}}
        )

        with self.assertRaises(ProviderError) as ctx:
            self.provider.generate("1+1은?")
        self.assertIn("API key not valid", str(ctx.exception))
        self.assertFalse(ctx.exception.is_quota_error)  # 400은 quota 문제가 아님

    @patch("providers.api_provider.requests.post")
    def test_429_status_marks_quota_error(self, mock_post) -> None:
        # 회귀 방지(2026-07-27 QuotaFallbackProvider 도입): 429는 호출 한도 소진이라
        # is_quota_error가 서야 QuotaFallbackProvider가 대체 provider로 넘어간다.
        mock_post.return_value = make_response(
            429, {"error": {"code": 429, "message": "You exceeded your current quota"}}
        )

        with self.assertRaises(ProviderError) as ctx:
            self.provider.generate("1+1은?")
        self.assertTrue(ctx.exception.is_quota_error)

    @patch("providers.api_provider.requests.post")
    def test_non_json_200_response_raises(self, mock_post) -> None:
        # 상태 코드는 200이지만 몸통이 JSON이 아닌 경우 (예: 프록시 개입) — 회귀 방지 테스트.
        response = MagicMock(spec=requests.Response)
        response.status_code = 200
        response.json.side_effect = ValueError("not json")
        response.text = "<html>proxy error</html>"
        mock_post.return_value = response

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.api_provider.requests.post")
    def test_malformed_response_raises(self, mock_post) -> None:
        mock_post.return_value = make_response(200, {"unexpected": "shape"})

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.api_provider.requests.post")
    def test_empty_content_raises(self, mock_post) -> None:
        mock_post.return_value = make_response(
            200, {"candidates": [{"content": {"parts": []}}], "usageMetadata": {}}
        )

        with self.assertRaises(ProviderError):
            self.provider.generate("1+1은?")

    @patch("providers.api_provider.requests.post")
    def test_network_error_raises_without_leaking_url(self, mock_post) -> None:
        mock_post.side_effect = requests.exceptions.ConnectionError(
            "Failed to resolve 'generativelanguage.googleapis.com' (key=fake-key-for-tests)"
        )

        with self.assertRaises(ProviderError) as ctx:
            self.provider.generate("1+1은?")
        # 원본 예외 메시지(키가 섞여 나올 수 있는)를 그대로 노출하지 않는지 확인
        self.assertNotIn("fake-key-for-tests", str(ctx.exception))

    @patch("providers.api_provider.requests.post")
    def test_api_key_sent_as_header_not_query_string(self, mock_post) -> None:
        mock_post.return_value = make_response(
            200,
            {
                "candidates": [{"content": {"parts": [{"text": "2"}]}}],
                "usageMetadata": {"candidatesTokenCount": 1},
            },
        )

        self.provider.generate("1+1은?")

        called_url = mock_post.call_args.args[0]
        called_headers = mock_post.call_args.kwargs["headers"]
        self.assertNotIn("fake-key-for-tests", called_url)
        self.assertEqual(called_headers.get("x-goog-api-key"), "fake-key-for-tests")


if __name__ == "__main__":
    unittest.main()
