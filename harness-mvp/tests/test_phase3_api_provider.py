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
