"""Fetcher 인터페이스: 외부 데이터를 "읽기 전용"으로 조회하는 컴포넌트.

providers/base.py의 Provider와 역할이 다르다 — Provider는 LLM을 호출해 새 콘텐츠를
"생성"하고, Fetcher는 외부 시스템(클라우드 가격 API 등)에서 이미 존재하는 데이터를
"조회"만 한다. 아무것도 바꾸지 않는다(액션 실행이 아니다) — cloud-ops 도메인 범위를
"텍스트 출력만"으로 정했을 때와 같은 안전 경계다(총괄 레이어 설계 논의 참고).

실패 처리는 Provider와 동일하게 예외로 한다 — 호출부가 재시도/폴백 여부를 판단한다.
"""
from __future__ import annotations

from abc import ABC, abstractmethod

from harness.schemas import FetchResult


class FetcherError(RuntimeError):
    """Fetcher.fetch() 호출 실패를 나타낸다."""


class Fetcher(ABC):
    """모든 fetcher 구현체(aws_price_fetcher, ncp_price_fetcher 등)가 따르는 공통 인터페이스."""

    fetcher_id: str

    @abstractmethod
    def fetch(self, **params: object) -> FetchResult:
        """외부 데이터를 조회해서 FetchResult로 반환한다. 실패 시 FetcherError를 던진다."""
        raise NotImplementedError
