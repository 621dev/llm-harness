"""AWS VPC 부가 상품(Site-to-Site VPN) 가격 Fetcher — AWS Price List Bulk API 사용.

NAT Gateway/EBS와 달리 Site-to-Site VPN은 `AmazonEC2`가 아니라 `AmazonVPC` 서비스
카탈로그(productFamily="Cloud Connectivity")에 있어 별도 offer 파일
(`.../offers/v1.0/aws/AmazonVPC/current/{region}/index.json`)을 받아야 한다 — 그래서
`aws_price_fetcher.py`의 `AwsEc2PriceFetcher`에 얹지 않고 별도 클래스로 뺐다. 인증은
필요 없다(AWS Price List Bulk API는 공개 JSON).

2026-07-20 ap-northeast-2 실측으로 구조 확인(NCP 실제 청구서 분석 중 IPsec VPN Gateway가
견적에서 빠져있던 걸 발견해서 보강): `vpnType` 속성이 "VPN Standard (1.25 Gbps)"
(usagetype 접미사 `VPN-Usage-Hours:ipsec.1`)/"VPN Large (5 Gbps)"/"VPN Concentrator"로
나뉘는데, 온프레미스 연동용 표준 Site-to-Site VPN 연결 1개에 해당하는 "VPN Standard"를
대표값으로 쓴다($0.05/시간, ap-northeast-2).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import requests

from harness.schemas import FetchResult

from .base import Fetcher, FetcherError

_BULK_URL_TEMPLATE = (
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonVPC/current/{region}/index.json"
)
_CACHE_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_CACHE_DIR = Path(".cache/aws_vpc_price")
_VPN_STANDARD_TYPE = "VPN Standard (1.25 Gbps)"


class AwsVpcPriceFetcher(Fetcher):
    fetcher_id = "aws_vpc_price"

    def __init__(self, *, cache_dir: Optional[Path] = None, timeout: float = 60.0) -> None:
        self.cache_dir = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR
        self.timeout = timeout

    def fetch(self, *, region: str) -> FetchResult:
        """Fetcher ABC가 요구하는 진입점 — 이 클래스의 상품이 VPN 하나뿐이라
        `fetch_vpn_gateway_price()`에 그대로 위임한다."""
        return self.fetch_vpn_gateway_price(region=region)

    def fetch_vpn_gateway_price(self, *, region: str) -> FetchResult:
        """Site-to-Site VPN 표준 연결(VPN Standard, 1.25 Gbps) 1개의 시간당 요금."""
        catalog = self._load_catalog(region)
        sku = self._find_vpn_sku(catalog, vpn_type=_VPN_STANDARD_TYPE)
        if sku is None:
            raise FetcherError(f"VPN Gateway 가격 정보를 못 찾음: region={region!r}")
        price_usd_per_hour, description = self._extract_ondemand_price(catalog, sku)

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"Site-to-Site VPN({_VPN_STANDARD_TYPE}) ({region}) ${price_usd_per_hour}/시간",
            data={
                "region": region,
                "vpn_type": _VPN_STANDARD_TYPE,
                "sku": sku,
                "usd_per_hour": price_usd_per_hour,
                "description": description,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _find_vpn_sku(catalog: dict[str, Any], *, vpn_type: str) -> Optional[str]:
        for sku, product in catalog.get("products", {}).items():
            if product.get("productFamily") != "Cloud Connectivity":
                continue
            if product.get("attributes", {}).get("vpnType") == vpn_type:
                return sku
        return None

    @staticmethod
    def _extract_ondemand_price(catalog: dict[str, Any], sku: str) -> tuple[float, str]:
        sku_terms = catalog.get("terms", {}).get("OnDemand", {}).get(sku)
        if not sku_terms:
            raise FetcherError(f"OnDemand 조건을 못 찾음 (sku={sku!r})")
        term = next(iter(sku_terms.values()))
        dimension = next(iter(term["priceDimensions"].values()))
        price = float(dimension["pricePerUnit"]["USD"])
        return price, dimension.get("description", "")

    def _load_catalog(self, region: str) -> dict[str, Any]:
        cache_path = self.cache_dir / f"{region}.json"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _CACHE_TTL_SECONDS:
            return json.loads(cache_path.read_text(encoding="utf-8"))

        url = _BULK_URL_TEMPLATE.format(region=region)
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FetcherError(f"AWS VPC 가격 카탈로그 다운로드 실패 (region={region!r}): {exc}") from exc

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(resp.content)
        return json.loads(resp.content)
