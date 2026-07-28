"""API Key Provider (Phase 3).

harness-implementation-plan-ko.md Section 2, Section 10을 구현한다. API 키로 REST를
직접 호출해서 종량제(pay-as-you-go) 방식으로 답을 받는다 — 구독 CLI 로그인
(cli_subscription_provider.py)과 달리 실제 토큰당 과금이 발생하므로, api_key 모드는
`Candidate.cost_usd`를 채운다(schemas.py 규칙 그대로).

지금은 `GeminiApiProvider`만 있다. Gemini는 개인 Google 계정으로 Gemini Code Assist
CLI 구독 로그인이 막혀 있어서(`cli_subscription_provider.py` 모듈 docstring 참고)
api_key 모드로만 지원하기로 했다. openai/anthropic도 API 키가 있다면 같은
`ApiProvider` 베이스를 상속해서 추가하면 된다 — 지금은 필요할 때 확장하는 걸로
미룬다(Agent Soup/과설계 방지, Section 3 "확장 지점은 미리, 실제 구현은 필요할 때").

보안 참고: API 키는 URL 쿼리스트링이 아니라 HTTP 헤더(`x-goog-api-key`)로 보낸다 —
쿼리스트링에 넣으면 `requests`의 연결 예외 메시지에 URL이 그대로 포함되면서 키가
로그/errors.json에 새어나갈 수 있다(실제로 이 위험을 인지하고 헤더 방식으로
바꿨다). 에러 메시지에도 키나 헤더 원문을 절대 넣지 않는다.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests

from harness.schemas import Candidate

from .base import Provider, ProviderError

DEFAULT_TIMEOUT_SEC = 60.0


class ApiProvider(Provider):
    """API 키 기반 REST 호출 공통 로직.

    서비스마다 요청/응답 형식이 달라 서브클래스가 `_build_request`/`_parse_response`를
    구현한다. 재시도는 여기서 하지 않는다(Provider 계약: 실패는 예외로, 재시도는
    model_runner 책임).
    """

    api_key_env_var: str = ""  # 서브클래스가 지정 (예: "GEMINI_API_KEY")

    def __init__(self, config, *, timeout_sec: float = DEFAULT_TIMEOUT_SEC) -> None:
        super().__init__(config)
        self.timeout_sec = timeout_sec

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        api_key = os.environ.get(self.api_key_env_var)
        if not api_key:
            raise ProviderError(f"{self.api_key_env_var} 환경변수가 설정돼 있지 않다 (API 키 필요)")

        url, headers, body = self._build_request(api_key, prompt, temperature)

        start = time.monotonic()
        try:
            response = requests.post(url, headers=headers, json=body, timeout=self.timeout_sec)
        except requests.RequestException as exc:
            # str(exc)에 URL이 섞여 나올 수 있어(쿼리스트링 방식이었다면 키 노출 위험),
            # 예외 타입 이름만 남기고 원문은 버린다.
            raise ProviderError(f"{self.provider_id} API 호출 실패: {type(exc).__name__}") from exc
        latency_ms = int((time.monotonic() - start) * 1000)

        if response.status_code != 200:
            # 429 = rate limit/quota exceeded. QuotaFallbackProvider가 이 플래그로
            # "대체 provider로 넘어가도 되는 실패"와 "진짜 버그" 실패를 구분한다.
            raise ProviderError(
                f"{self.provider_id} API 오류 (status={response.status_code}): "
                f"{self._extract_error_message(response)}",
                is_quota_error=response.status_code == 429,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            # 상태 코드는 200인데 몸통이 JSON이 아닌 경우(예: 프록시가 끼어든 경우) —
            # 원문을 그대로 노출하지 않고 앞부분만 잘라서 보여준다.
            raise ProviderError(f"{self.provider_id} API 응답이 JSON이 아님: {response.text[:200]!r}") from exc

        content, tokens = self._parse_response(payload)
        if not content:
            raise ProviderError(f"{self.provider_id} API 응답에 내용이 없음")

        return Candidate(
            model_id=self.model_id,
            content=content,
            tokens=tokens,
            latency_ms=latency_ms,
            cost_usd=self._estimate_cost(tokens),  # api_key 모드는 cost_usd를 채운다
            status="success",
        )

    def _build_request(self, api_key: str, prompt: str, temperature: float) -> tuple[str, dict, dict]:
        """(url, headers, json_body)를 반환한다. 서브클래스가 구현."""
        raise NotImplementedError

    def _parse_response(self, data: dict) -> tuple[str, Optional[int]]:
        """(content, tokens)를 반환한다. 서브클래스가 구현."""
        raise NotImplementedError

    def _estimate_cost(self, tokens: Optional[int]) -> Optional[float]:
        """대략적인 비용 추정치. 정확한 청구 금액은 각 서비스 콘솔에서 확인해야 한다."""
        return None

    def _extract_error_message(self, response: requests.Response) -> str:
        try:
            return str(response.json().get("error", {}).get("message", response.text[:300]))
        except ValueError:
            return response.text[:300]


class GeminiApiProvider(ApiProvider):
    """Gemini REST API(`generateContent`)를 API 키로 직접 호출한다."""

    api_key_env_var = "GEMINI_API_KEY"
    # gemini-2.5-flash 기준 대략적인 출력 토큰 단가(2026-07 시점, $2.50/1M output).
    # candidatesTokenCount(출력 토큰)만 알 수 있어 출력 단가로만 추정한다 — 정확한
    # 청구 금액이 아니라 러프한 추정치임을 명확히 하기 위해 필드명도 estimated_cost_usd
    # 계열로 취급한다(RunMetrics와 동일한 관례).
    _COST_PER_OUTPUT_TOKEN_USD = 2.50 / 1_000_000

    def _build_request(self, api_key: str, prompt: str, temperature: float) -> tuple[str, dict, dict]:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.model_id}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {"temperature": temperature},
        }
        return url, headers, body

    def _parse_response(self, data: dict) -> tuple[str, Optional[int]]:
        try:
            parts = data["candidates"][0]["content"]["parts"]
            content = "".join(part.get("text", "") for part in parts)
        except (KeyError, IndexError) as exc:
            raise ProviderError("Gemini 응답 형식이 예상과 다름 (candidates/content/parts 없음)") from exc

        tokens = data.get("usageMetadata", {}).get("candidatesTokenCount")
        return content, tokens

    def _estimate_cost(self, tokens: Optional[int]) -> Optional[float]:
        if tokens is None:
            return None
        return round(tokens * self._COST_PER_OUTPUT_TOKEN_USD, 8)
