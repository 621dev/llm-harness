"""AWS EC2 온디맨드 가격 Fetcher — AWS Price List Bulk API 사용.

인증이 필요 없다(공개 JSON 파일, `pricing.us-east-1.amazonaws.com`에서 리전 무관하게
서빙됨). 리전별 EC2 전체 카탈로그를 통째로 받아(수백 MB 수준) 로컬에서 원하는
instanceType/OS/tenancy 조합을 필터링한다 — SigV4 인증이 필요한 Query API보다 쉽지만,
매번 전체 카탈로그를 새로 받으면 느리므로 짧은 로컬 캐시를 둔다.

실제로 `https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/
ap-northeast-2/index.json`을 받아 구조를 확인하고 작성함(2026-07-12) — `products[sku]
.attributes`에서 스펙으로 SKU를 찾고, `terms.OnDemand[sku]`(내부에 "{sku}.{offerTermCode}"
키 하나) → `priceDimensions`(내부에 "{sku}.{offerTermCode}.{rateCode}" 키 하나) →
`pricePerUnit.USD`로 가격을 얻는다.

데이터 전송(아웃바운드) 비용은 다루지 않는다 — AWSDataTransfer가 별도 offer 파일이라
범위를 인스턴스 온디맨드 요금으로 좁혔다(1차 검증 범위, task 프롬프트의 "가정한 단가는
명시" 제약으로 나머지는 LLM이 채운다).

`fetch_nat_gateway_price()`(2026-07-20 추가, NCP 실제 청구서 분석 중 NAT Gateway가
견적에서 빠져있던 걸 발견해서 보강): NAT Gateway도 같은 AmazonEC2 카탈로그
(productFamily="NAT Gateway")에 있다. GB당 데이터 처리 요금은 제외 — 견적 시점엔
처리량을 모르므로 시간당(상시 가동) 단가만 다룬다. 2026-07-20 ap-northeast-2 실측:
$0.059/시간.
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
    "https://pricing.us-east-1.amazonaws.com/offers/v1.0/aws/AmazonEC2/current/{region}/index.json"
)
_CACHE_TTL_SECONDS = 24 * 60 * 60  # 24시간 — 온디맨드 단가는 자주 안 바뀐다
_DEFAULT_CACHE_DIR = Path(".cache/aws_price")


class AwsEc2PriceFetcher(Fetcher):
    fetcher_id = "aws_ec2_price"

    def __init__(self, *, cache_dir: Optional[Path] = None, timeout: float = 60.0) -> None:
        self.cache_dir = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR
        self.timeout = timeout

    def fetch(
        self,
        *,
        region: str,
        instance_type: str,
        operating_system: str = "Linux",
        tenancy: str = "Shared",
    ) -> FetchResult:
        catalog = self._load_catalog(region)
        sku = self._find_sku(catalog, instance_type=instance_type, operating_system=operating_system, tenancy=tenancy)
        if sku is None:
            raise FetcherError(
                f"가격 정보를 못 찾음: region={region!r} instance_type={instance_type!r} "
                f"operating_system={operating_system!r} tenancy={tenancy!r}"
            )
        price_usd_per_hour, description = self._extract_ondemand_price(catalog, sku)

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"{instance_type} ({operating_system}, {region}) 온디맨드 ${price_usd_per_hour}/시간",
            data={
                "region": region,
                "instance_type": instance_type,
                "operating_system": operating_system,
                "tenancy": tenancy,
                "sku": sku,
                "usd_per_hour": price_usd_per_hour,
                "description": description,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    def fetch_ebs_price(self, *, region: str, volume_type: str = "gp3") -> FetchResult:
        """EBS 볼륨(디스크)의 GB-월 단가를 조회한다. `volume_type`은 AWS의 volumeApiName
        (gp3/gp2/standard/st1/io1/io2/sc1). 같은 AmazonEC2 카탈로그(Storage productFamily)에
        들어있어 `_load_catalog()`를 그대로 재사용한다(2026-07-14 실제 조회로 확인:
        gp3 ap-northeast-2 = $0.0912/GB-월)."""
        catalog = self._load_catalog(region)
        sku = self._find_ebs_sku(catalog, volume_type=volume_type)
        if sku is None:
            raise FetcherError(f"EBS 가격 정보를 못 찾음: region={region!r} volume_type={volume_type!r}")
        price_usd_per_gb_month, description = self._extract_ondemand_price(catalog, sku)

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"EBS {volume_type} ({region}) ${price_usd_per_gb_month}/GB-월",
            data={
                "region": region,
                "volume_type": volume_type,
                "sku": sku,
                "usd_per_gb_month": price_usd_per_gb_month,
                "description": description,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    def fetch_nat_gateway_price(self, *, region: str) -> FetchResult:
        """NAT Gateway 시간당 요금(GB당 데이터 처리 요금은 제외 — 견적 시점엔 처리량을
        모르므로 컴퓨트류 항목과 같은 "상시 가동" 단가만 다룬다)을 조회한다. 같은
        AmazonEC2 카탈로그(productFamily="NAT Gateway")에 들어있어 `_load_catalog()`를
        재사용한다. "Regional"(신형)과 구형 NAT Gateway 시간당 단가는 2026-07-20
        ap-northeast-2 실측으로 동일($0.059/시간)함을 확인, Regional 쪽을 대표값으로 쓴다."""
        catalog = self._load_catalog(region)
        sku = self._find_nat_gateway_sku(catalog)
        if sku is None:
            raise FetcherError(f"NAT Gateway 가격 정보를 못 찾음: region={region!r}")
        price_usd_per_hour, description = self._extract_ondemand_price(catalog, sku)

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"NAT Gateway ({region}) ${price_usd_per_hour}/시간",
            data={
                "region": region,
                "sku": sku,
                "usd_per_hour": price_usd_per_hour,
                "description": description,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _find_nat_gateway_sku(catalog: dict[str, Any]) -> Optional[str]:
        for sku, product in catalog.get("products", {}).items():
            if product.get("productFamily") != "NAT Gateway":
                continue
            usagetype = product.get("attributes", {}).get("usagetype", "")
            if usagetype.endswith("RegionalNatGateway-Hours"):
                return sku
        return None

    @staticmethod
    def _find_ebs_sku(catalog: dict[str, Any], *, volume_type: str) -> Optional[str]:
        for sku, product in catalog.get("products", {}).items():
            if product.get("productFamily") != "Storage":
                continue
            if product.get("attributes", {}).get("volumeApiName") == volume_type:
                return sku
        return None

    def _load_catalog(self, region: str) -> dict[str, Any]:
        cache_path = self.cache_dir / f"{region}.json"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _CACHE_TTL_SECONDS:
            return json.loads(cache_path.read_text(encoding="utf-8"))

        url = _BULK_URL_TEMPLATE.format(region=region)
        try:
            resp = requests.get(url, timeout=self.timeout)
            resp.raise_for_status()
        except requests.RequestException as exc:
            raise FetcherError(f"AWS 가격 카탈로그 다운로드 실패 (region={region!r}): {exc}") from exc

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(resp.content)
        return json.loads(resp.content)

    @staticmethod
    def _find_sku(
        catalog: dict[str, Any], *, instance_type: str, operating_system: str, tenancy: str
    ) -> Optional[str]:
        for sku, product in catalog.get("products", {}).items():
            attrs = product.get("attributes", {})
            if (
                attrs.get("instanceType") == instance_type
                and attrs.get("operatingSystem") == operating_system
                and attrs.get("tenancy") == tenancy
                and attrs.get("preInstalledSw") == "NA"
                and attrs.get("capacitystatus") == "Used"
                # licenseModel: Linux는 값이 하나뿐이지만(항상 "No License required"),
                # Windows는 BYOL(가져온 라이선스, 컴퓨트만 과금 — 더 저렴)과 License
                # Included(AWS가 라이선스 포함 제공 — 표준 요금)가 같은 instanceType/
                # operatingSystem으로 동시에 존재한다. 이 필터 없이는 dict 순회 순서에
                # 따라 BYOL이 먼저 걸려 실제보다 훨씬 싼 값을 실측치인 것처럼 반환할
                # 위험이 있다(2026-07-14 실제로 m5.xlarge Windows에서 재현 — BYOL
                # $0.236/시간 vs 표준 $0.42/시간, 거의 2배 차이). 사용자가 자체
                # 라이선스를 갖고 있다는 명시가 없는 한 표준(라이선스 포함) 요금이
                # 맞는 기본값이다.
                and attrs.get("licenseModel") == "No License required"
            ):
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
