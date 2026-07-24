"""NCP 블록스토리지/NAS 가격 Fetcher — `ncp_price_fetcher.py`와 같은 Billing API
(getProductPriceList)를 `productCategoryCode="STORAGE"`로 호출한다.

서명(HMAC-SHA256)/HTTP 요청 로직을 새로 만들지 않고 `NcpServerPriceFetcher` 인스턴스를
내부에 두고 그 `get_product_price_list()`를 그대로 재사용한다(같은 API, 같은 인증
방식이라 중복 구현할 이유가 없음).

2026-07-14 실제 계정으로 STORAGE 카테고리를 조회해 구조 확인:
- 블록스토리지(추가형, 네트워크 SSD, productCode="SPBSTBSTAD000006")는
  `productRatingType.code="BST"`, `unit.code="STRG_1G_HH"`(GB-시간 종량)로 0.16원/GB-시간.
- NAS(productCode="SPNAS00000000001")는 `productRatingType.code="NSSZ"`(NAS Volume Size),
  같은 `STRG_1G_HH` 단위로 0.1원/GB-시간.
둘 다 컴퓨트 서버와 달리 "GB-시간" 단위라 월 비용 환산 시 호출부가 730시간을 곱해야 한다.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Optional

from harness.schemas import FetchResult

from .base import Fetcher, FetcherError
from .ncp_price_fetcher import NcpServerPriceFetcher

_HOURLY_PRICE_TYPE_CODE = "MTRAT"  # Meter rate — 온디맨드/종량제
_GB_HOUR_UNIT_CODE = "STRG_1G_HH"  # Usage capacity (GB-hour)

# storage_kind -> (productCode, productRatingType.code) — 둘 다 2026-07-14 실제 조회로 확인.
_STORAGE_KIND_TO_PRODUCT = {
    "block_ssd": ("SPBSTBSTAD000006", "BST"),  # Additional Block Storage [NET] (SSD)
    "nas": ("SPNAS00000000001", "NSSZ"),  # Ncloud NAS, NAS Volume Size 과금 항목
}


class NcpStoragePriceFetcher(Fetcher):
    fetcher_id = "ncp_storage_price"

    def __init__(
        self,
        *,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        timeout: float = 30.0,
        cache_dir=None,
    ) -> None:
        self.access_key = access_key or os.environ.get("NCP_ACCESS_KEY")
        self.secret_key = secret_key or os.environ.get("NCP_SECRET_KEY")
        # cache_dir을 그대로 내부 클라이언트에 넘긴다 — get_product_price_list()의
        # 24시간 캐시(ncp_price_fetcher.py 참고)가 여기도 그대로 적용된다.
        self._client = NcpServerPriceFetcher(
            access_key=self.access_key, secret_key=self.secret_key, timeout=timeout, cache_dir=cache_dir
        )

    def fetch(
        self,
        *,
        storage_kind: str,
        region_code: str = "KR",
        pay_currency_code: str = "KRW",
    ) -> FetchResult:
        """storage_kind: "block_ssd"(네트워크 블록스토리지, SSD, 추가형) 또는 "nas"."""
        if not self.access_key or not self.secret_key:
            raise FetcherError("NCP_ACCESS_KEY/NCP_SECRET_KEY 환경변수가 설정돼 있지 않다")
        if storage_kind not in _STORAGE_KIND_TO_PRODUCT:
            raise FetcherError(f"알 수 없는 storage_kind: {storage_kind!r} (block_ssd/nas만 지원)")

        product_code, rating_type_code = _STORAGE_KIND_TO_PRODUCT[storage_kind]
        products = self._client.get_product_price_list(
            region_code=region_code, product_category_code="STORAGE", pay_currency_code=pay_currency_code
        )
        krw_per_gb_hour = self._extract_gb_hour_price(
            products, product_code=product_code, rating_type_code=rating_type_code
        )
        if krw_per_gb_hour is None:
            raise FetcherError(
                f"가격 정보를 못 찾음: storage_kind={storage_kind!r} product_code={product_code!r}"
            )

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"NCP {storage_kind} ({region_code}) {krw_per_gb_hour}원/GB-시간",
            data={
                "storage_kind": storage_kind,
                "region_code": region_code,
                "product_code": product_code,
                "krw_per_gb_hour": krw_per_gb_hour,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _extract_gb_hour_price(
        products: list[dict[str, Any]], *, product_code: str, rating_type_code: str
    ) -> Optional[float]:
        for product in products:
            if product.get("productCode") != product_code:
                continue
            for price in product.get("priceList", []):
                if (
                    price.get("priceType", {}).get("code") == _HOURLY_PRICE_TYPE_CODE
                    and price.get("productRatingType", {}).get("code") == rating_type_code
                    and price.get("unit", {}).get("code") == _GB_HOUR_UNIT_CODE
                ):
                    return price.get("price")
        return None
