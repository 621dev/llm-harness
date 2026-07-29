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
            raise ProviderError(
                f"{self.api_key_env_var} 환경변수가 설정돼 있지 않다 (API 키 필요)",
                is_auth_error=True,  # 환경변수는 재시도해도 안 생긴다
            )

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
            # 401/403 = 인증 실패. 둘 다 재시도가 무의미해서 model_runner가
            # is_retryable로 걸러낸다(2026-07-29).
            raise ProviderError(
                f"{self.provider_id} API 오류 (status={response.status_code}): "
                f"{self._extract_error_message(response)}",
                is_quota_error=response.status_code == 429,
                is_auth_error=response.status_code in (401, 403),
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
        input_tokens = self._parse_input_tokens(payload)

        return Candidate(
            model_id=self.model_id,
            content=content,
            tokens=tokens,
            input_tokens=input_tokens,
            latency_ms=latency_ms,
            # api_key 모드는 cost_usd를 채운다. 입력 토큰도 함께 넘긴다(2026-07-29) —
            # 그전까지 출력만 세서 체인처럼 입력이 큰 패턴의 비용이 과소 집계됐다.
            cost_usd=self._estimate_cost(tokens, input_tokens),
            status="success",
        )

    def _build_request(self, api_key: str, prompt: str, temperature: float) -> tuple[str, dict, dict]:
        """(url, headers, json_body)를 반환한다. 서브클래스가 구현."""
        raise NotImplementedError

    def _parse_response(self, data: dict) -> tuple[str, Optional[int]]:
        """(content, 출력 토큰)를 반환한다. 서브클래스가 구현."""
        raise NotImplementedError

    def _parse_input_tokens(self, data: dict) -> Optional[int]:
        """입력(프롬프트) 토큰 수. 응답에 없으면 None — 서브클래스가 구현."""
        return None

    def _estimate_cost(self, tokens: Optional[int], input_tokens: Optional[int] = None) -> Optional[float]:
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
    # gemini-2.5-flash 기준 대략적인 토큰 단가(2026-07 시점). 정확한 청구 금액이 아니라
    # 러프한 추정치임을 명확히 하려고 필드명도 estimated_cost_usd 계열로 쓴다.
    #
    # **입력 단가를 2026-07-29에 추가했다.** 그전까지 `candidatesTokenCount`(출력)만
    # 세서, 체인처럼 **입력이 큰 패턴의 비용이 통째로 과소 집계**됐다 — 3단계 체인은
    # 스텝마다 이전 결과를 전부 받아 입력이 direct_call의 90배가 넘는데(실측) 그게
    # cost_usd에 0원으로 반영됐다. 종량제 키에서는 입력도 실제 청구 대상이고,
    # `budget_usd` 상한이 이 값을 근거로 동작하므로 빠지면 상한이 헐거워진다.
    #
    # ⚠️ 단가는 콘솔에서 확인해 맞출 것 — 모델/티어에 따라 다르고, 여기 값은
    # 코드가 원래 갖고 있던 출력 단가($2.50/1M)와 Flash 계열의 통상적인 입력:출력
    # 비율을 근거로 둔 추정이다. 정확한 금액 관리가 필요하면 이 두 상수를 먼저 맞춘다.
    _COST_PER_OUTPUT_TOKEN_USD = 2.50 / 1_000_000
    _COST_PER_INPUT_TOKEN_USD = 0.30 / 1_000_000

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

    def _parse_input_tokens(self, data: dict) -> Optional[int]:
        return data.get("usageMetadata", {}).get("promptTokenCount")

    def _estimate_cost(self, tokens: Optional[int], input_tokens: Optional[int] = None) -> Optional[float]:
        """출력 + 입력 토큰으로 추정한다.

        둘 다 없으면 None(= 비용 미상). 한쪽만 있으면 있는 쪽만 계산한다 — 응답 형식이
        바뀌어 한 필드가 사라져도 비용이 통째로 None이 되는 것보다 낫다(그러면
        `budget_usd` 상한이 아무것도 못 막는다).
        """
        if tokens is None and input_tokens is None:
            return None
        cost = (tokens or 0) * self._COST_PER_OUTPUT_TOKEN_USD
        cost += (input_tokens or 0) * self._COST_PER_INPUT_TOKEN_USD
        return round(cost, 8)
