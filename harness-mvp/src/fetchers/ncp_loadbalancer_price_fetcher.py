"""NCP 로드밸런서/NAT Gateway 데이터 처리 요금 Fetcher — `ncp_price_fetcher.py`와 같은
Billing API를 `productCategoryCode="NETWORKING"`으로 호출한다.

2026-07-28 실제 계정으로 NETWORKING 카테고리 조회해 확인(NCP 엔터프라이즈 견적서
템플릿 분석 중 발견): 로드밸런서는 4종(`LB.VLB.APP.001`=어플리케이션,
`LB.VLB.NET.001`=네트워크, `LB.VLB.NP.001`=네트워크 프록시, `LB.VLB.INLINE.001`=
인라인)이고, 각 상품의 `priceList` 안에 `productRatingType.code`가 티어별로
PF_SM(Small)/PF_MM(Medium)/PF_LG(Large)/PF_EL(Extra-large)/PF_DS(dynamic-sizing,
인라인·네트워크 로드밸런서 일부만 지원)로 나뉜다. 어플리케이션 로드밸런서만
인바운드 데이터 처리 요금(`DTIN`, GB당)이 별도로 있다 — 나머지 3종은 없음.

NAT Gateway는 이미 `estimate_config.json`에 정적 참고값(56원/시간)이 있지만,
데이터 처리량 요금(`NATNW`, GB당)은 아직 어디에도 없어서 여기서 같이 조회한다
(NAT Gateway 자체는 `SPNATGW000000001` 상품, `NATMN`=기본요금 56원/시간으로
`estimate_config.json`의 값과 실측 일치 확인, `NATNW`는 2026-07-28 기준 0원/GB —
계정/시점에 따라 달라질 수 있어 매번 실측한다).
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from harness.schemas import FetchResult

from .base import Fetcher, FetcherError
from .ncp_price_fetcher import NcpServerPriceFetcher

_HOURLY_PRICE_TYPE_CODE = "MTRAT"

_LB_TYPE_TO_PRODUCT_CODE = {
    "APP": "LB.VLB.APP.001",
    "NET": "LB.VLB.NET.001",
    "NP": "LB.VLB.NP.001",
    "INLINE": "LB.VLB.INLINE.001",
}
_TIER_TO_RATING_CODE = {
    "Small": "PF_SM", "Medium": "PF_MM", "Large": "PF_LG", "Extra-large": "PF_EL", "dynamic-sizing": "PF_DS",
}
_LB_INBOUND_RATING_CODE = "DTIN"
_NAT_GATEWAY_PRODUCT_CODE = "SPNATGW000000001"
_NAT_MAINTENANCE_RATING_CODE = "NATMN"
_NAT_DATA_RATING_CODE = "NATNW"


class NcpLoadBalancerPriceFetcher(Fetcher):
    fetcher_id = "ncp_loadbalancer_price"

    def __init__(self, *, access_key: Optional[str] = None, secret_key: Optional[str] = None, timeout: float = 30.0, cache_dir=None) -> None:
        self._client = NcpServerPriceFetcher(access_key=access_key, secret_key=secret_key, timeout=timeout, cache_dir=cache_dir)

    def fetch(self, *, lb_type: str = "APP", tier: str = "Small", region_code: str = "KR", pay_currency_code: str = "KRW") -> FetchResult:
        """`lb_type`: APP(어플리케이션)/NET(네트워크)/NP(네트워크 프록시)/INLINE(인라인).
        `tier`: Small/Medium/Large/Extra-large(전부 지원하는 타입은 APP/NP뿐, NET은
        Small/Medium/Large만, INLINE은 dynamic-sizing만 — 안 맞으면 FetcherError)."""
        if lb_type not in _LB_TYPE_TO_PRODUCT_CODE:
            raise FetcherError(f"알 수 없는 lb_type: {lb_type!r} (지원: {sorted(_LB_TYPE_TO_PRODUCT_CODE)})")
        if tier not in _TIER_TO_RATING_CODE:
            raise FetcherError(f"알 수 없는 tier: {tier!r} (지원: {sorted(_TIER_TO_RATING_CODE)})")

        product_code = _LB_TYPE_TO_PRODUCT_CODE[lb_type]
        rating_code = _TIER_TO_RATING_CODE[tier]
        products = self._client.get_product_price_list(region_code=region_code, product_category_code="NETWORKING", pay_currency_code=pay_currency_code)

        product = next((p for p in products if p.get("productCode") == product_code), None)
        if product is None:
            raise FetcherError(f"로드밸런서 상품을 못 찾음: product_code={product_code!r}")

        hourly = next(
            (
                p for p in product.get("priceList", [])
                if p.get("priceType", {}).get("code") == _HOURLY_PRICE_TYPE_CODE and p.get("productRatingType", {}).get("code") == rating_code
            ),
            None,
        )
        if hourly is None:
            raise FetcherError(f"{lb_type} 로드밸런서는 {tier} 티어를 지원 안 함(product_code={product_code!r})")

        inbound = None
        if lb_type == "APP":
            inbound = next(
                (
                    p for p in product.get("priceList", [])
                    if p.get("priceType", {}).get("code") == _HOURLY_PRICE_TYPE_CODE and p.get("productRatingType", {}).get("code") == _LB_INBOUND_RATING_CODE
                ),
                None,
            )

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=(
                f"{product.get('productName')} {tier} ({region_code}) {hourly['price']}원/시간"
                + (f" + 인바운드 데이터 {inbound['price']}원/GB" if inbound else "")
            ),
            data={
                "region_code": region_code, "lb_type": lb_type, "tier": tier,
                "product_code": product_code, "krw_per_hour": hourly["price"],
                "inbound_krw_per_gb": inbound["price"] if inbound else None,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    def fetch_nat_gateway_price(self, *, region_code: str = "KR", pay_currency_code: str = "KRW") -> FetchResult:
        """NAT Gateway 기본요금(시간당) + 데이터 처리량 요금(GB당)."""
        products = self._client.get_product_price_list(region_code=region_code, product_category_code="NETWORKING", pay_currency_code=pay_currency_code)
        product = next((p for p in products if p.get("productCode") == _NAT_GATEWAY_PRODUCT_CODE), None)
        if product is None:
            raise FetcherError("NAT Gateway 상품을 못 찾음")

        maintenance = next((p for p in product.get("priceList", []) if p.get("productRatingType", {}).get("code") == _NAT_MAINTENANCE_RATING_CODE), None)
        data = next((p for p in product.get("priceList", []) if p.get("productRatingType", {}).get("code") == _NAT_DATA_RATING_CODE), None)
        if maintenance is None or data is None:
            raise FetcherError("NAT Gateway 기본요금/데이터 처리량 요금 항목을 못 찾음")

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"NAT Gateway ({region_code}) {maintenance['price']}원/시간 + 데이터 처리 {data['price']}원/GB",
            data={"region_code": region_code, "krw_per_hour": maintenance["price"], "data_krw_per_gb": data["price"]},
            fetched_at=datetime.now(timezone.utc),
        )
