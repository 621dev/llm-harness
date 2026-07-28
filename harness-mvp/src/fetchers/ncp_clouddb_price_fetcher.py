"""NCP Cloud DB for MySQL 가격 Fetcher — `ncp_price_fetcher.py`와 같은 Billing API
(getProductPriceList)를 `productCategoryCode="DATABASE"`로 호출한다.

2026-07-28 실제 계정으로 DATABASE 카테고리를 조회해 구조 확인(NCP 엔터프라이즈
견적서 템플릿 분석 중 발견): DATABASE 카테고리엔 MySQL/MongoDB/MSSQL/PostgreSQL/
Cache가 전부 섞여 있고 productCode 접두사로만 구분된다 — `VMGDB`=MongoDB(Config
Server/Mongod/Mongos 용어), `VMSSL`=MSSQL(Principal/Slave Server), `VPGSL`=
PostgreSQL(Primary&Secondary/Read Replica), `VRDS`=Cache(4vCPU 고정+51.2GB 단위
메모리 티어, Redis류), 나머지 `VDBAS`(Master/Recovery/Slave Server 용어 — 고전적
MySQL 복제 용어와 일치)가 소거법으로 MySQL로 확인됨(`productCode`에 "MYSQL" 문자열이
박힌 별도 상품은 백업/스냅샷/서버리스뿐이라 컴퓨트 자체엔 엔진명이 안 붙어 있음).

`priceList` 안에 `productRatingType.code`가 MASTER/RECOVERY/SLAVE로 나뉘는데,
단일 노드(비HA) 견적엔 MASTER 하나만 쓴다 — HA 구성이면 호출부가 SLAVE/RECOVERY
역할도 같은 단가로 추가해야 한다(3롤 전부 동일 시간당 단가로 확인됨, 2026-07-28).

백업(`fetch_backup_price()`)은 별도 상품(`SPBACKUPVMYSL001`, "MYSQL(VPC) Backup")
이고 정액 기본요금 없이 평균 보관 용량(GB-월) 기준 순수 종량제다(2026-07-28 실측
100원/GB-월) — `SPBACKUPMYSQL001`(구 버전, BareMetal 계열로 추정)도 동일 단가라
구분 없이 VPC용 상품 코드로 고정한다.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from harness.schemas import FetchResult

from .base import Fetcher, FetcherError
from .ncp_price_fetcher import NcpServerPriceFetcher

_HOURLY_PRICE_TYPE_CODE = "MTRAT"
_BYTES_PER_GIB = 1024**3
_MASTER_RATING_CODE = "MASTER"
_BACKUP_PRODUCT_CODE = "SPBACKUPVMYSL001"
_BACKUP_RATING_CODE = "DBBKU"


class NcpCloudDbPriceFetcher(Fetcher):
    fetcher_id = "ncp_clouddb_price"

    def __init__(self, *, access_key: Optional[str] = None, secret_key: Optional[str] = None, timeout: float = 30.0, cache_dir=None) -> None:
        self._client = NcpServerPriceFetcher(access_key=access_key, secret_key=secret_key, timeout=timeout, cache_dir=cache_dir)

    def fetch(self, *, vcpu: int, memory_gb: float, region_code: str = "KR", pay_currency_code: str = "KRW") -> FetchResult:
        """Cloud DB for MySQL 단일 노드(Master)의 시간당 컴퓨트 요금 + 기본 제공 디스크(GB)."""
        products = self._client.get_product_price_list(region_code=region_code, product_category_code="DATABASE", pay_currency_code=pay_currency_code)

        target_memory_bytes = round(memory_gb * _BYTES_PER_GIB)
        matched = None
        for product in products:
            code = product.get("productCode", "") or ""
            if not code.startswith("SVR.VDBAS"):
                continue
            if product.get("cpuCount") != vcpu or product.get("memorySize") != target_memory_bytes:
                continue
            for price in product.get("priceList", []):
                if (
                    price.get("priceType", {}).get("code") == _HOURLY_PRICE_TYPE_CODE
                    and price.get("productRatingType", {}).get("code") == _MASTER_RATING_CODE
                ):
                    matched = (product, price)
                    break
            if matched:
                break

        if matched is None:
            raise FetcherError(
                f"Cloud DB for MySQL 가격 정보를 못 찾음: vcpu={vcpu} memory_gb={memory_gb} region_code={region_code!r}"
            )
        product, price = matched
        base_disk_gb = (product.get("baseBlockStorageSize") or 0) / _BYTES_PER_GIB

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=(
                f"Cloud DB for MySQL vCPU {vcpu}개/메모리 {memory_gb}GB ({region_code}) "
                f"Master {price['price']}원/시간 ({product.get('productName')}), 기본 제공 디스크 {base_disk_gb:.0f}GB"
            ),
            data={
                "region_code": region_code,
                "vcpu": vcpu,
                "memory_gb": memory_gb,
                "product_code": product.get("productCode"),
                "product_name": product.get("productName"),
                "krw_per_hour": price["price"],
                "base_disk_gb": base_disk_gb,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    def fetch_backup_price(self, *, region_code: str = "KR", pay_currency_code: str = "KRW") -> FetchResult:
        """Cloud DB for MySQL 백업의 GB-월당 요금(정액 기본요금 없음, 순수 종량제)."""
        products = self._client.get_product_price_list(region_code=region_code, product_category_code="DATABASE", pay_currency_code=pay_currency_code)

        matched_price = None
        for product in products:
            if product.get("productCode") != _BACKUP_PRODUCT_CODE:
                continue
            for price in product.get("priceList", []):
                if (
                    price.get("priceType", {}).get("code") == _HOURLY_PRICE_TYPE_CODE
                    and price.get("productRatingType", {}).get("code") == _BACKUP_RATING_CODE
                ):
                    matched_price = price
                    break

        if matched_price is None:
            raise FetcherError(f"Cloud DB for MySQL 백업 가격 정보를 못 찾음: region_code={region_code!r}")

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"Cloud DB for MySQL 백업 ({region_code}) {matched_price['price']}원/GB-월(평균 보관 용량 기준)",
            data={"region_code": region_code, "krw_per_gb_month": matched_price["price"]},
            fetched_at=datetime.now(timezone.utc),
        )
