"""NCP(네이버클라우드플랫폼) 서버 가격 Fetcher — Billing API(getProductPriceList) 사용.

`NCP_ACCESS_KEY`/`NCP_SECRET_KEY` 환경변수가 필요하다(콘솔 > 마이페이지 > 계정관리 >
인증키 관리에서 발급, 계정 생성 시 1개 자동 발급). AWS Bulk API와 달리 인증이 필수다.
`api_provider.py`의 관행과 동일하게 키는 헤더로만 전달하고 URL/예외 메시지에 노출되지
않게 한다.

**2026-07-13 실제 계정으로 검증 완료**(`scripts/verify_ncp_price_fetcher.py`). 공식
문서만 보고 작성했던 최초 버전 대비 실제로 다른 점 두 가지를 발견해서 고쳤다:

1. 서명 대상 URI는 `_BASE_URL`(`.../billing/v1`)이 아니라 **호스트 기준 전체 경로**여야
   한다 — `/billing/v1/product/getProductPriceList?...`처럼 `/billing/v1` 접두사를
   빠뜨리면 401 "Invalid authentication information"이 난다.
2. 응답 구조가 문서 추정과 달랐다: `getProductPriceListResponse.productPriceList`가
   `{"productPrice": [...]}`로 한 번 더 감싸여 있는 게 아니라 **그 자체가 이미 상품
   배열**이고, 각 상품 항목에 `cpuCount`/`memorySize`(바이트 단위) 스펙 필드와
   `priceList`(그 상품의 여러 가격 옵션 배열)가 들어있다.
3. `productCategoryCode="COMPUTE"`가 서버 컴퓨팅 상품의 실제 카테고리 코드다. 같은
   스펙(vCPU/메모리)에 여러 상품 라인(Standard/CPU/CPUSSD/HighCPU/HighCPU-SSD 등,
   디스크 타입·크기·세대별로도 나뉨)이 동시에 존재하고, 각 상품의 `priceList` 안에
   `priceType.code`가 `"MTRAT"`(Meter rate, 시간당 종량제 = 온디맨드)와 `"FXSUM"`
   (Monthly flat rate, 월정액)로 나뉜다 — 온디맨드 견적에는 `MTRAT`만 쓴다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional
from urllib.parse import urlencode

import requests

from harness.schemas import FetchResult

from .base import Fetcher, FetcherError

_HOST = "https://billingapi.apigw.ntruss.com"
_BASE_PATH = "/billing/v1"
_BASE_URL = _HOST + _BASE_PATH
_PRODUCT_PRICE_LIST_PATH = "/product/getProductPriceList"
_HOURLY_PRICE_TYPE_CODE = "MTRAT"  # Meter rate — 온디맨드/종량제 시간당 요금
_BYTES_PER_GIB = 1024**3

# aws_price_fetcher.py와 동일한 관행(2026-07-15) — 온디맨드 단가는 자주 안 바뀌고,
# NCP는 캐시가 전혀 없어서 컴퓨트/스토리지/Windows·RHEL 라이선스를 조회할 때마다
# 매번 실제 API를 호출하고 있었다. get_product_price_list()가 이 fetcher의 모든
# 조회(fetch/fetch_windows_license_price, ncp_storage_price_fetcher.py까지)가
# 거치는 단일 진입점이라 여기 한 곳에만 캐시를 두면 전부 적용된다.
_CACHE_TTL_SECONDS = 24 * 60 * 60
_DEFAULT_CACHE_DIR = Path(".cache/ncp_price")


class NcpServerPriceFetcher(Fetcher):
    fetcher_id = "ncp_server_price"

    def __init__(
        self,
        *,
        access_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        timeout: float = 30.0,
        cache_dir: Optional[Path] = None,
    ) -> None:
        self.access_key = access_key or os.environ.get("NCP_ACCESS_KEY")
        self.secret_key = secret_key or os.environ.get("NCP_SECRET_KEY")
        self.timeout = timeout
        self.cache_dir = cache_dir if cache_dir is not None else _DEFAULT_CACHE_DIR

    def fetch(
        self,
        *,
        vcpu: int,
        memory_gb: float,
        region_code: str = "KR",
        product_category_code: str = "COMPUTE",
        pay_currency_code: str = "KRW",
    ) -> FetchResult:
        """vCPU/메모리 스펙에 맞는 NCP 서버 상품의 온디맨드(시간당) 가격 후보를 조회한다.

        같은 스펙에 여러 상품 라인(디스크 타입/크기/세대별)이 존재할 수 있어, 매칭되는
        모든 후보를 시간당 요금 오름차순으로 `data["candidates"]`에 담아 반환한다 —
        어떤 상품 라인을 "그 스펙의 대표값"으로 볼지는 호출부(LLM 프롬프트 조립 등)가
        정하게 하고, 여기서는 임의로 하나만 골라 정보를 숨기지 않는다.
        """
        if not self.access_key or not self.secret_key:
            raise FetcherError("NCP_ACCESS_KEY/NCP_SECRET_KEY 환경변수가 설정돼 있지 않다")

        products = self.get_product_price_list(
            region_code=region_code,
            product_category_code=product_category_code,
            pay_currency_code=pay_currency_code,
        )

        target_memory_bytes = round(memory_gb * _BYTES_PER_GIB)
        candidates = self._extract_hourly_candidates(products, vcpu=vcpu, memory_bytes=target_memory_bytes)
        if not candidates:
            raise FetcherError(
                f"가격 정보를 못 찾음: vcpu={vcpu} memory_gb={memory_gb} "
                f"region_code={region_code!r} product_category_code={product_category_code!r}"
            )
        candidates.sort(key=lambda c: c["krw_per_hour"])
        cheapest = candidates[0]

        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=(
                f"vCPU {vcpu}개/메모리 {memory_gb}GB ({region_code}) NCP 온디맨드 "
                f"최저 {cheapest['krw_per_hour']}원/시간 ({cheapest['product_name']}), "
                f"후보 {len(candidates)}건"
            ),
            data={
                "region_code": region_code,
                "vcpu": vcpu,
                "memory_gb": memory_gb,
                "pay_currency_code": pay_currency_code,
                "candidates": candidates,
            },
            fetched_at=datetime.now(timezone.utc),
        )

    # NCP는 Windows Server 온디맨드 상품 자체가 없다(REST API 카탈로그에 2016/2019/2022만
    # 있고, 2026-07-14 기준 2025는 아직 없음) — 최신 버전을 기본 대체값으로 쓴다.
    _WINDOWS_LICENSE_FALLBACK_VERSIONS = ("2022", "2019", "2016")

    def fetch_windows_license_price(
        self, *, version: str = "2022", region_code: str = "KR", pay_currency_code: str = "KRW"
    ) -> FetchResult:
        """Windows Server OS 라이선스의 온디맨드(시간당) 가격을 조회한다.

        NCP는 Windows를 컴퓨트 상품과 분리된 별도 SW(소프트웨어) 상품으로 과금한다
        (2026-07-14 실제 계정으로 확인: `productItemKind.code="SW"`,
        `productCode` 예 `SW.VSVR.OS.WND64.WND.SVR2022EN.G003`, `cpuCount`/`memorySize`는
        0 — vCPU/메모리와 무관하게 서버 1대당 정액). `fetch()`가 돌려주는 컴퓨트 단가에
        이 값을 더해야 Windows 서버의 실제 시간당 비용이 된다.

        `version`(예: "2025")이 카탈로그에 없으면 `_WINDOWS_LICENSE_FALLBACK_VERSIONS`
        순서대로 대체 버전을 찾고, 실제로 쓴 버전을 `data["version"]`/`summary`에 명시한다
        (요청한 버전을 못 찾았다고 조용히 다른 값을 쓰지 않기 위해)."""
        if not self.access_key or not self.secret_key:
            raise FetcherError("NCP_ACCESS_KEY/NCP_SECRET_KEY 환경변수가 설정돼 있지 않다")

        products = self.get_product_price_list(
            region_code=region_code, product_category_code="COMPUTE", pay_currency_code=pay_currency_code
        )
        # VPC Server(가상서버)용만 남긴다 — Bare Metal 변형(productCode에 ".BM." 포함,
        # 예 SW.VSVR.BM.OS.WND64.WND.SVR2022EN)은 osInfomation이 VPC 버전과 완전히
        # 같은 텍스트라 dict 키 충돌이 나고, 코어 수별 가격 티어가 여러 개 섞여 있어
        # 첫 번째 MTRAT 항목을 그냥 집으면 부정확한 값이 나온다(2026-07-14 실제로
        # 재현: VPC $28/시간과 우연히 같은 값이 나와서 겉으로는 안 드러났지만, 로직
        # 자체는 임의의 코어-티어 가격을 집는 것이라 다른 버전/상황에서 틀릴 수 있음).
        windows_products = {
            p.get("osInfomation", ""): p
            for p in products
            if p.get("productItemKind", {}).get("code") == "SW"
            and p.get("osType", {}).get("code") == "WND"
            and ".BM." not in p.get("productCode", "")
        }

        versions_to_try = [version] + [v for v in self._WINDOWS_LICENSE_FALLBACK_VERSIONS if v != version]
        matched_product = None
        matched_version = None
        for candidate_version in versions_to_try:
            matched_product = next(
                (p for info, p in windows_products.items() if f"Windows Server {candidate_version}" in info), None
            )
            if matched_product is not None:
                matched_version = candidate_version
                break

        if matched_product is None:
            raise FetcherError(f"NCP 카탈로그에서 Windows Server 라이선스 상품을 못 찾음(요청 버전: {version})")

        hourly = next(
            (
                price
                for price in matched_product.get("priceList", [])
                if price.get("priceType", {}).get("code") == _HOURLY_PRICE_TYPE_CODE
            ),
            None,
        )
        if hourly is None:
            raise FetcherError(f"Windows Server {matched_version} 라이선스 상품에 시간당(MTRAT) 가격이 없음")

        note = "" if matched_version == version else f" (요청한 {version}이 카탈로그에 없어 {matched_version}으로 대체)"
        return FetchResult(
            source=self.fetcher_id,
            status="success",
            summary=f"NCP Windows Server {matched_version} 라이선스 {hourly['price']}원/시간{note}",
            data={
                "version": matched_version,
                "requested_version": version,
                "product_code": matched_product.get("productCode"),
                "krw_per_hour": hourly["price"],
            },
            fetched_at=datetime.now(timezone.utc),
        )

    @staticmethod
    def _extract_hourly_candidates(
        products: list[dict[str, Any]], *, vcpu: int, memory_bytes: int
    ) -> list[dict[str, Any]]:
        candidates: list[dict[str, Any]] = []
        for product in products:
            if product.get("cpuCount") != vcpu or product.get("memorySize") != memory_bytes:
                continue
            hourly = next(
                (
                    price
                    for price in product.get("priceList", [])
                    if price.get("priceType", {}).get("code") == _HOURLY_PRICE_TYPE_CODE
                ),
                None,
            )
            if hourly is None:
                continue
            price_value = hourly.get("price")
            # 0원/시간짜리 SKU가 카탈로그에 섞여 있다(2026-07-28 실제로 재현: vCPU 1개/
            # 메모리 1GB 조회 시 "SPSVRSTAND000056A" 가 0원으로 나와 candidates[0](최저가)이
            # 그걸 골라버림 — 실제 판매 상품이 아니라 사용 중단된/레거시 카탈로그 잔재로
            # 보인다. 실제 서버를 0원에 쓸 수 있을 리 없으니 0 이하 가격은 애초에 후보에서
            # 뺀다(aws_price_fetcher.py가 BYOL/capacitystatus로 anomaly를 거르는 것과 같은
            # 이유 — "그 스펙에 존재하는 모든 후보를 숨기지 않는다"는 원칙은 유효하되,
            # 구매 불가능한 0원 항목까지 "후보"로 셀 이유는 없다).
            if price_value is None or price_value <= 0:
                continue
            candidates.append(
                {
                    "product_code": product.get("productCode"),
                    "product_name": product.get("productName"),
                    "price_no": hourly.get("priceNo"),
                    "krw_per_hour": price_value,
                }
            )
        return candidates

    def get_product_price_list(
        self, *, region_code: str, product_category_code: str, pay_currency_code: str
    ) -> list[dict[str, Any]]:
        """`product_category_code`는 COMPUTE에 국한되지 않는 범용 호출이라(예: STORAGE)
        `ncp_storage_price_fetcher.py`가 서명/요청 로직을 새로 안 만들고 이 메서드를
        그대로 재사용한다(다른 fetcher 인스턴스를 생성해 호출).

        (region_code, product_category_code, pay_currency_code) 조합별로 24시간
        로컬 파일 캐시를 쓴다(aws_price_fetcher.py와 동일한 TTL) — 이 메서드가
        컴퓨트/스토리지/Windows·RHEL 라이선스 조회가 전부 거치는 단일 진입점이라
        여기서 캐시하면 그 전부에 적용된다."""
        cache_path = self.cache_dir / f"{region_code}_{product_category_code}_{pay_currency_code}.json"
        if cache_path.exists() and (time.time() - cache_path.stat().st_mtime) < _CACHE_TTL_SECONDS:
            return json.loads(cache_path.read_text(encoding="utf-8"))

        params = {
            "regionCode": region_code,
            "productCategoryCode": product_category_code,
            "payCurrencyCode": pay_currency_code,
            "pageSize": "1000",
            "responseFormatType": "json",
        }
        payload = self._request(_PRODUCT_PRICE_LIST_PATH, params)
        raw_products = payload.get("getProductPriceListResponse", {}).get("productPriceList", [])
        if raw_products is None:
            products: list[dict[str, Any]] = []
        elif isinstance(raw_products, list):
            products = raw_products
        else:
            products = [raw_products]

        self.cache_dir.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(products, ensure_ascii=False), encoding="utf-8")
        return products

    def _request(self, path: str, params: dict[str, str]) -> dict[str, Any]:
        timestamp = str(int(time.time() * 1000))
        # 서명 대상 URI는 호스트(https://billingapi.apigw.ntruss.com) 기준 전체 경로다 —
        # _BASE_URL이 이미 /billing/v1을 포함하므로 서명에도 그 접두사를 붙여야 한다
        # (프로토콜/도메인은 제외, 그 뒤 전체 경로+쿼리스트링은 포함). 실제 계정으로 이
        # 접두사를 빠뜨리면 401이 난다는 걸 확인했다(모듈 docstring 참고).
        signature = self._sign(method="GET", path=_BASE_PATH + path, params=params, timestamp=timestamp)
        headers = {
            "x-ncp-apigw-timestamp": timestamp,
            "x-ncp-iam-access-key": self.access_key,
            "x-ncp-apigw-signature-v2": signature,
        }
        try:
            resp = requests.get(_BASE_URL + path, params=params, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise FetcherError(f"NCP API 호출 실패: {exc}") from exc

        if resp.status_code != 200:
            raise FetcherError(f"NCP API가 상태코드 {resp.status_code}를 반환함: {resp.text[:300]}")
        try:
            return resp.json()
        except ValueError as exc:
            raise FetcherError(f"NCP API 응답이 JSON이 아님: {resp.text[:300]}") from exc

    def _sign(self, *, method: str, path: str, params: dict[str, str], timestamp: str) -> str:
        query = urlencode(params)
        message = f"{method} {path}?{query}\n{timestamp}\n{self.access_key}"
        digest = hmac.new(self.secret_key.encode("utf-8"), message.encode("utf-8"), hashlib.sha256).digest()
        return base64.b64encode(digest).decode("utf-8")
