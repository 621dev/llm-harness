"""Provider 인터페이스 정의 (Step 2).

harness-implementation-plan-ko.md Section 2, Section 4(Action/Observation Contract)의
`run_model(model_id, prompt, temperature) -> Observation` 계약을 provider 레벨로 좁힌
버전이다. mock/api_key/cli_subscription 구현체가 전부 이 인터페이스를 따른다.

실패 처리는 예외로 한다: generate()가 실패하면 예외를 던진다. 재시도/최종 error 기록
(Section 6 복구 전략)은 이 클래스가 아니라 호출부인 model_runner가 담당한다 — provider는
"어떻게 답을 만드는가"만 알고, "실패하면 어떻게 복구하는가"는 모른다 (관심사 분리).
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from harness.schemas import Candidate, ProviderConfig


class ProviderError(RuntimeError):
    """Provider.generate() 호출 실패를 나타낸다 (model_runner가 잡아서 재시도 여부를 판단).

    mock.py, api_provider.py, cli_subscription_provider.py가 전부 이걸 공유한다 —
    model_runner는 provider 구현체가 뭔지 몰라도 이 예외 하나만 알면 재시도 로직을
    똑같이 적용할 수 있다.

    `is_quota_error`(2026-07-27, `QuotaFallbackProvider` 도입과 함께 추가): 호출
    한도/rate-limit(예: Gemini 무료 티어 HTTP 429)로 실패했음을 표시한다.
    `QuotaFallbackProvider`가 이 플래그가 선 예외만 골라 대체 provider로 넘어가고,
    그 외(응답 형식 오류, 인증 실패 등 진짜 버그일 수 있는 실패)는 그대로
    전파한다 — 문자열 매칭(예: 에러 메시지에 "429"가 있는지 검사)은 로케일/문구
    변경에 취약하다는 걸 `worktree-sync` 상태 판정 버그에서 이미 겪어서, 발생
    지점(api_provider.py)에서 상태 코드를 보고 직접 표시하는 방식을 택했다."""

    def __init__(self, message: str, *, is_quota_error: bool = False) -> None:
        super().__init__(message)
        self.is_quota_error = is_quota_error


class Provider(ABC):
    """모든 provider 구현체(mock, api_key, cli_subscription)가 따르는 공통 인터페이스."""

    def __init__(self, config: ProviderConfig) -> None:
        self.config = config

    @property
    def provider_id(self) -> str:
        return self.config.provider_id

    @property
    def model_id(self) -> str:
        return self.config.model_id

    @property
    def auth_mode(self) -> str:
        """api_key(종량제) / cli_subscription(구독 한도 소모).

        구독 호출은 cost_usd가 None이라 비용 지표에 안 잡히므로, 대신 호출 횟수를
        세려면 호출부(model_runner)가 이 값을 봐야 한다(Section 9 Cost Blindness 방지).
        """
        return self.config.auth_mode

    @abstractmethod
    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        """prompt에 대한 후보 응답을 생성해서 Candidate로 반환한다.

        실패 시 예외를 던진다 (status="error" Candidate를 직접 만들지 않는다 — 그건
        재시도 여부를 판단하는 model_runner의 책임이다).
        """
        raise NotImplementedError
