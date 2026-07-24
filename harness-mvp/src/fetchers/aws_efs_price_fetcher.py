"""AWS EFS(관리형 NAS) Standard 스토리지 가격 Fetcher — AWS Price List Bulk API 사용.

`aws_price_fetcher.py`의 EC2 Fetcher와 같은 패턴(공개 Bulk API, 인증 불필요, 리전별
전체 카탈로그를 받아 로컬 필터링)이지만 서비스가 다르므로(`AmazonEFS`) 별도 offer
파일을 받는다. `_load_catalog()`의 캐싱 로직도 같은 이유로 이 파일 안에 따로 둔다
(EC2 Fetcher의 `_load_catalog`는 `AmazonEC2` URL에 고정돼 있어 재사용 불가).

2026-07-14 실제로 `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEFS/
current/ap-northeast-2/index.json`을 받아 구조 확인: Standard(General Purpose,
Multi-AZ) 스토리지가 `usagetype`이 `...TimedStorage-ByteHrs`로 끝나고(One Zone은
`-Z-`가 중간에 낀 `...TimedStorage-Z-ByteHrs`라 구분됨) `pricePerUnit.USD`가
GB-월 단가다(ap-northeast-2 기준 $0.33/GB-월).
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
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEFS/current/{region}/index.json"
)
_CACHE_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_CACHE_DIR = Path(".cache/aws_efs_price")


class AwsEfsPriceFetcher(Fetcher):
    fetcher_id = "aws_efs_price"

    def __init__(self, *, cache_dir: Optional[Path] = None, timeout: float = 60.0) -> None:
        self.cache_dir = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR
        self.timeout = timeout

    def fetch(self, *, region: str) -> FetchResult:
        """EFS Standard(General Purpose, Multi-AZ) 스토리지의 GB-월 단가를 조회한다."""
        catalog = self._load_catalog(region)
        sku = self._find_standard_storage_sku(catalog)
        if sku is None:
            raise FetcherError(f"EFS 가격 정보를 못 찾음: region={region!r}")
        price_usd_per_gb_month, description = self._extract_ondemand_price(catalog, sku)

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"EFS Standard ({region}) ${price_usd_per_gb_month}/GB-월",
            data={
                "region": region,
                "sku": sku,
                "usd_per_gb_month": price_usd_per_gb_month,
                "description": description,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    def _load_catalog(self, region: str) -> dict[str, Any]:
        cache_path = self.cache_dir / f"{region}.json"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _CACHE_TTL_SECONDS:
            return json.loads(cache_path.read_text(encoding="utf-8"))

        url = _BULK_URL_TEMPLATE.format(region=region)
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FetcherError(f"AWS EFS 가격 카탈로그 다운로드 실패 (region={region!r}): {exc}") from exc

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(resp.content)
        return json.loads(resp.content)

    @staticmethod
    def _find_standard_storage_sku(catalog: dict[str, Any]) -> Optional[str]:
        for sku, product in catalog.get("products", {}).items():
            if product.get("productFamily") != "Storage":
                continue
            attrs = product.get("attributes", {})
            usagetype = attrs.get("usagetype", "")
            if attrs.get("storageClass") == "General Purpose" and usagetype.endswith("TimedStorage-ByteHrs"):
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
