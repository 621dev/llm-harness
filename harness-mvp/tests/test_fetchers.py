"""fetchers/ 테스트 (stdlib unittest).

총괄 레이어 설계 논의에서 도출된 새 컴포넌트 종류(Fetcher: 외부 데이터 읽기 전용)를
검증한다. 실제 AWS/NCP API를 호출하면 진짜 다운로드/과금이 발생하므로, 여기서는
`requests.get`을 모킹해서 파싱/서명/에러 처리 로직만 검증한다. NCP는 아직 실제 계정
검증 전이라(모듈 docstring 참고) 여기 테스트도 문서 스키마 기준 최선 추정이다.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from fetchers.aws_efs_price_fetcher import AwsEfsPriceFetcher  # noqa: E402
from fetchers.aws_price_fetcher import AwsEc2PriceFetcher  # noqa: E402
from fetchers.base import FetcherError  # noqa: E402
from fetchers.ncp_price_fetcher import NcpServerPriceFetcher  # noqa: E402
from fetchers.ncp_storage_price_fetcher import NcpStoragePriceFetcher  # noqa: E402


def make_response(status_code: int, *, json_data: dict | None = None, content: bytes | None = None, text: str = "") -> MagicMock:
    response = MagicMock(spec=requests.Response)
    response.status_code = status_code
    response.content = content if content is not None else json.dumps(json_data or {}).encode("utf-8")
    response.text = text or str(json_data)
    response.json.return_value = json_data
    response.raise_for_status.side_effect = (
        None if status_code < 400 else requests.HTTPError(f"{status_code}")
    )
    return response


def _sample_catalog() -> dict:
    """실제 AWS EC2 offer 파일의 최소 구조(2026-07-12에 실제 응답으로 확인한 형태).

    2026-07-14: EBS Storage(gp3) 항목도 실제 응답 구조 그대로 추가함(같은 AmazonEC2
    카탈로그 안에 productFamily="Storage"로 인스턴스와 나란히 들어있음)."""
    return {
        "products": {
            "SKU123": {
                "sku": "SKU123",
                "productFamily": "Compute Instance",
                "attributes": {
                    "instanceType": "c5.2xlarge",
                    "operatingSystem": "Linux",
                    "tenancy": "Shared",
                    "preInstalledSw": "NA",
                    "capacitystatus": "Used",
                    "licenseModel": "No License required",
                    "regionCode": "ap-northeast-2",
                },
            },
            "SKU_WINDOWS": {
                "sku": "SKU_WINDOWS",
                "productFamily": "Compute Instance",
                "attributes": {
                    "instanceType": "c5.2xlarge",
                    "operatingSystem": "Windows",
                    "tenancy": "Shared",
                    "preInstalledSw": "NA",
                    "capacitystatus": "Used",
                    "licenseModel": "No License required",
                    "regionCode": "ap-northeast-2",
                },
            },
            "SKU_WINDOWS_BYOL": {
                "sku": "SKU_WINDOWS_BYOL",
                "productFamily": "Compute Instance",
                "attributes": {
                    "instanceType": "c5.2xlarge",
                    "operatingSystem": "Windows",
                    "tenancy": "Shared",
                    "preInstalledSw": "NA",
                    "capacitystatus": "Used",
                    "licenseModel": "Bring your own license",
                    "regionCode": "ap-northeast-2",
                },
            },
            "SKU_EBS_GP3": {
                "sku": "SKU_EBS_GP3",
                "productFamily": "Storage",
                "attributes": {
                    "volumeType": "General Purpose",
                    "volumeApiName": "gp3",
                    "regionCode": "ap-northeast-2",
                },
            },
        },
        "terms": {
            "OnDemand": {
                "SKU123": {
                    "SKU123.TERMCODE": {
                        "priceDimensions": {
                            "SKU123.TERMCODE.RATECODE": {
                                "pricePerUnit": {"USD": "0.3840000000"},
                                "description": "$0.384 per On Demand Linux c5.2xlarge Instance Hour",
                            }
                        }
                    }
                },
                "SKU_EBS_GP3": {
                    "SKU_EBS_GP3.TERMCODE": {
                        "priceDimensions": {
                            "SKU_EBS_GP3.TERMCODE.RATECODE": {
                                "pricePerUnit": {"USD": "0.0912000000"},
                                "description": "$0.0912 per GB-month of General Purpose (gp3) provisioned storage",
                            }
                        }
                    }
                },
                "SKU_WINDOWS": {
                    "SKU_WINDOWS.TERMCODE": {
                        "priceDimensions": {
                            "SKU_WINDOWS.TERMCODE.RATECODE": {
                                "pricePerUnit": {"USD": "0.6800000000"},
                                "description": "$0.68 per On Demand Windows c5.2xlarge Instance Hour",
                            }
                        }
                    }
                },
                "SKU_WINDOWS_BYOL": {
                    "SKU_WINDOWS_BYOL.TERMCODE": {
                        "priceDimensions": {
                            "SKU_WINDOWS_BYOL.TERMCODE.RATECODE": {
                                "pricePerUnit": {"USD": "0.3840000000"},
                                "description": "$0.384 per On Demand Windows BYOL c5.2xlarge Instance Hour",
                            }
                        }
                    }
                },
            }
        },
    }


def _sample_efs_catalog() -> dict:
    """실제 AWS EFS offer 파일의 최소 구조(2026-07-14 실제 응답으로 확인).

    One Zone 스토리지(usagetype에 "-Z-"가 낀 변형)도 함께 넣어서 필터링이 Standard만
    골라내는지 검증한다."""
    return {
        "products": {
            "SKU_EFS_STANDARD": {
                "sku": "SKU_EFS_STANDARD",
                "productFamily": "Storage",
                "attributes": {"storageClass": "General Purpose", "usagetype": "APN2-TimedStorage-ByteHrs"},
            },
            "SKU_EFS_ONE_ZONE": {
                "sku": "SKU_EFS_ONE_ZONE",
                "productFamily": "Storage",
                "attributes": {"storageClass": "One Zone-General Purpose", "usagetype": "APN2-TimedStorage-Z-ByteHrs"},
            },
        },
        "terms": {
            "OnDemand": {
                "SKU_EFS_STANDARD": {
                    "SKU_EFS_STANDARD.TERMCODE": {
                        "priceDimensions": {
                            "SKU_EFS_STANDARD.TERMCODE.RATECODE": {
                                "pricePerUnit": {"USD": "0.3300000000"},
                                "description": "$0.33 per GB-Mo for Standard storage",
                            }
                        }
                    }
                },
                "SKU_EFS_ONE_ZONE": {
                    "SKU_EFS_ONE_ZONE.TERMCODE": {
                        "priceDimensions": {
                            "SKU_EFS_ONE_ZONE.TERMCODE.RATECODE": {
                                "pricePerUnit": {"USD": "0.1760000000"},
                                "description": "$0.176 per GB-Mo for One Zone Storage",
                            }
                        }
                    }
                },
            }
        },
    }


class AwsEc2PriceFetcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="aws-price-cache-"))
        self.addCleanup(self._cleanup)
        self.fetcher = AwsEc2PriceFetcher(cache_dir=self.tmp_dir)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("fetchers.aws_price_fetcher.requests.get")
    def test_fetch_finds_linux_shared_price(self, mock_get) -> None:
        mock_get.return_value = make_response(200, json_data=_sample_catalog())

        result = self.fetcher.fetch(region="ap-northeast-2", instance_type="c5.2xlarge")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["usd_per_hour"], 0.384)
        self.assertEqual(result.data["sku"], "SKU123")
        mock_get.assert_called_once()

    @patch("fetchers.aws_price_fetcher.requests.get")
    def test_windows_prefers_license_included_over_byol(self, mock_get) -> None:
        """회귀 테스트: 2026-07-14 실제 m5.xlarge Windows 조회에서 재현 — 같은
        instanceType/operatingSystem에 BYOL(가져온 라이선스, 컴퓨트만 과금이라
        더 저렴)과 License Included(AWS가 라이선스 포함 제공, 표준 요금) SKU가
        동시에 존재한다. licenseModel 필터 없이는 dict 순회 순서에 따라 BYOL이
        먼저 걸려 실제보다 훨씬 싼 값을 반환할 위험이 있었다(실측: BYOL $0.236 vs
        표준 $0.42, 거의 2배 차이). License Included 쪽이 선택되는지 고정해둔다."""
        mock_get.return_value = make_response(200, json_data=_sample_catalog())

        result = self.fetcher.fetch(region="ap-northeast-2", instance_type="c5.2xlarge", operating_system="Windows")

        self.assertEqual(result.data["usd_per_hour"], 0.68)  # License Included, BYOL(0.384)이 아님
        self.assertEqual(result.data["sku"], "SKU_WINDOWS")

    @patch("fetchers.aws_price_fetcher.requests.get")
    def test_second_call_uses_cache_not_network(self, mock_get) -> None:
        mock_get.return_value = make_response(200, json_data=_sample_catalog())

        self.fetcher.fetch(region="ap-northeast-2", instance_type="c5.2xlarge")
        self.fetcher.fetch(region="ap-northeast-2", instance_type="c5.2xlarge")

        mock_get.assert_called_once()  # 두 번째 호출은 캐시를 써서 네트워크 호출이 없어야 함

    @patch("fetchers.aws_price_fetcher.requests.get")
    def test_unknown_instance_type_raises_fetcher_error(self, mock_get) -> None:
        mock_get.return_value = make_response(200, json_data=_sample_catalog())

        with self.assertRaises(FetcherError):
            self.fetcher.fetch(region="ap-northeast-2", instance_type="made-up.xlarge")

    @patch("fetchers.aws_price_fetcher.requests.get")
    def test_network_failure_raises_fetcher_error(self, mock_get) -> None:
        mock_get.side_effect = requests.ConnectionError("boom")

        with self.assertRaises(FetcherError):
            self.fetcher.fetch(region="ap-northeast-2", instance_type="c5.2xlarge")

    @patch("fetchers.aws_price_fetcher.requests.get")
    def test_fetch_ebs_price_finds_gp3_price_per_gb_month(self, mock_get) -> None:
        mock_get.return_value = make_response(200, json_data=_sample_catalog())

        result = self.fetcher.fetch_ebs_price(region="ap-northeast-2", volume_type="gp3")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["usd_per_gb_month"], 0.0912)

    @patch("fetchers.aws_price_fetcher.requests.get")
    def test_fetch_ebs_price_unknown_volume_type_raises(self, mock_get) -> None:
        mock_get.return_value = make_response(200, json_data=_sample_catalog())

        with self.assertRaises(FetcherError):
            self.fetcher.fetch_ebs_price(region="ap-northeast-2", volume_type="made-up")


class AwsEfsPriceFetcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="aws-efs-price-cache-"))
        self.addCleanup(self._cleanup)
        self.fetcher = AwsEfsPriceFetcher(cache_dir=self.tmp_dir)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    @patch("fetchers.aws_efs_price_fetcher.requests.get")
    def test_fetch_finds_standard_general_purpose_price(self, mock_get) -> None:
        mock_get.return_value = make_response(200, json_data=_sample_efs_catalog())

        result = self.fetcher.fetch(region="ap-northeast-2")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["usd_per_gb_month"], 0.33)  # One Zone($0.176)이 아니라 Standard가 선택돼야 함

    @patch("fetchers.aws_efs_price_fetcher.requests.get")
    def test_network_failure_raises_fetcher_error(self, mock_get) -> None:
        mock_get.side_effect = requests.ConnectionError("boom")

        with self.assertRaises(FetcherError):
            self.fetcher.fetch(region="ap-northeast-2")


class NcpServerPriceFetcherTest(unittest.TestCase):
    def setUp(self) -> None:
        # get_product_price_list()가 24시간 로컬 캐시를 쓰게 되면서(2026-07-15),
        # 테스트마다 격리된 임시 캐시 디렉터리를 안 쓰면 같은 (region, category,
        # currency) 키를 쓰는 테스트끼리 캐시가 섞여 이전 테스트의 mock 데이터를
        # 잘못 재사용할 위험이 있다(aws_price_fetcher.py 테스트와 동일한 이유).
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ncp-price-cache-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_missing_keys_raises_without_network_call(self) -> None:
        fetcher = NcpServerPriceFetcher(access_key=None, secret_key=None)

        with self.assertRaises(FetcherError):
            fetcher.fetch(vcpu=8, memory_gb=16)

    def test_signature_matches_documented_hmac_sha256_algorithm(self) -> None:
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)

        signature = fetcher._sign(
            method="GET", path="/product/getProductPriceList", params={"a": "1", "b": "2"}, timestamp="1000"
        )

        message = "GET /product/getProductPriceList?a=1&b=2\n1000\ntest-access"
        expected = base64.b64encode(
            hmac.new(b"test-secret", message.encode("utf-8"), hashlib.sha256).digest()
        ).decode("utf-8")
        self.assertEqual(signature, expected)

    def test_request_signs_full_host_relative_path_not_just_base_url_suffix(self) -> None:
        """회귀 테스트: _BASE_URL(.../billing/v1) 뒤의 path만 서명하면 실제 계정 호출 시
        401 "Invalid authentication information"이 났다(2026-07-13 실제로 확인) — 서명
        대상은 /billing/v1까지 포함한 호스트 기준 전체 경로여야 한다."""
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        captured: dict[str, str] = {}

        def fake_sign(*, method, path, params, timestamp):
            captured["path"] = path
            return "sig"

        fetcher._sign = fake_sign  # type: ignore[method-assign]
        with patch("fetchers.ncp_price_fetcher.requests.get") as mock_get:
            mock_get.return_value = make_response(200, json_data={"getProductPriceListResponse": {}})
            fetcher._request("/product/getProductPriceList", {"a": "1"})

        self.assertEqual(captured["path"], "/billing/v1/product/getProductPriceList")

    def _sample_products(self) -> list[dict]:
        """실제 getProductPriceList 응답(2026-07-13, COMPUTE 카테고리)의 최소 재현.

        productPriceList는 그 자체가 상품 배열이고(문서만 보고 추정했던 "productPrice로
        한 번 더 감싸여 있다"는 가정은 틀렸음이 실제 호출로 확인됨), 각 상품에
        cpuCount/memorySize(바이트)와 priceList(MTRAT=시간당/FXSUM=월정액 혼재)가 있다.
        """
        return [
            {
                "cpuCount": 8,
                "memorySize": 17179869184,  # 16 GiB
                "productCode": "SPSVRSTAND000006",
                "productName": "vCPU 8EA, Memory 16GB, Disk 50GB",
                "priceList": [
                    {"priceNo": "9", "priceType": {"code": "MTRAT"}, "price": 317},
                    {"priceNo": "10", "priceType": {"code": "FXSUM"}, "price": 228000},
                ],
            },
            {
                "cpuCount": 8,
                "memorySize": 17179869184,
                "productCode": "SPSVRSSD00000007",
                "productName": "vCPU 8EA, Memory 16GB, [SSD]Disk 50GB",
                "priceList": [
                    {"priceNo": "330", "priceType": {"code": "MTRAT"}, "price": 325},
                    {"priceNo": "331", "priceType": {"code": "FXSUM"}, "price": 234000},
                ],
            },
            {
                "cpuCount": 4,  # vCPU 다름 — 매칭 안 돼야 함
                "memorySize": 17179869184,
                "productCode": "SPSVRSTAND000005",
                "productName": "vCPU 4EA, Memory 16GB",
                "priceList": [{"priceNo": "8", "priceType": {"code": "MTRAT"}, "price": 200}],
            },
        ]

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_fetch_returns_hourly_candidates_sorted_by_price(self, mock_get) -> None:
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_products()}},
        )

        result = fetcher.fetch(vcpu=8, memory_gb=16)

        self.assertEqual(result.status, "success")
        candidates = result.data["candidates"]
        self.assertEqual(len(candidates), 2)  # cpuCount=4인 상품은 제외돼야 함
        self.assertEqual(candidates[0]["krw_per_hour"], 317)  # 오름차순 정렬 — 가장 싼 것 먼저
        self.assertEqual(candidates[1]["krw_per_hour"], 325)
        headers = mock_get.call_args.kwargs["headers"]
        self.assertIn("x-ncp-apigw-signature-v2", headers)
        self.assertEqual(headers["x-ncp-iam-access-key"], "test-access")

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_second_call_with_same_params_uses_cache_not_network(self, mock_get) -> None:
        """2026-07-15 추가: NCP는 캐시가 전혀 없어서 컴퓨트/스토리지/라이선스를
        조회할 때마다 매번 실제 API를 불렀다. aws_price_fetcher.py와 같은 24시간
        로컬 캐시를 get_product_price_list()에 추가 — 같은 (region, category,
        currency) 조합은 두 번째 호출부터 네트워크를 안 타야 한다."""
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_products()}},
        )

        fetcher.fetch(vcpu=8, memory_gb=16)
        fetcher.fetch(vcpu=8, memory_gb=16)

        mock_get.assert_called_once()

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_different_category_uses_separate_cache_entry(self, mock_get) -> None:
        """캐시 키에 product_category_code가 들어가므로 COMPUTE 조회 캐시가 STORAGE
        조회에 잘못 재사용되면 안 된다(다른 상품군인데 같은 캐시를 공유하면 엉뚱한
        데이터를 돌려주게 됨)."""
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_products()}},
        )

        fetcher.get_product_price_list(region_code="KR", product_category_code="COMPUTE", pay_currency_code="KRW")
        fetcher.get_product_price_list(region_code="KR", product_category_code="STORAGE", pay_currency_code="KRW")

        self.assertEqual(mock_get.call_count, 2)  # 카테고리가 다르니 캐시 미스로 둘 다 네트워크를 타야 함

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_fetch_excludes_monthly_flat_rate_from_candidates(self, mock_get) -> None:
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_products()}},
        )

        result = fetcher.fetch(vcpu=8, memory_gb=16)

        prices = [c["krw_per_hour"] for c in result.data["candidates"]]
        self.assertNotIn(228000, prices)  # FXSUM(월정액)은 후보에서 빠져야 함
        self.assertNotIn(234000, prices)

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_non_200_status_raises_fetcher_error(self, mock_get) -> None:
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(403, json_data={}, text="Forbidden")

        with self.assertRaises(FetcherError):
            fetcher.fetch(vcpu=8, memory_gb=16)

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_no_matching_spec_raises_fetcher_error(self, mock_get) -> None:
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_products()}},
        )

        with self.assertRaises(FetcherError):
            fetcher.fetch(vcpu=64, memory_gb=256)  # 샘플 데이터에 없는 스펙

    def _sample_windows_products(self) -> list[dict]:
        """실제 COMPUTE 카테고리 응답의 Windows SW 라이선스 상품 최소 재현(2026-07-14
        실제 계정으로 확인). cpuCount/memorySize는 0 — vCPU/메모리와 무관한 서버 1대당
        정액이라 fetch()의 컴퓨트 매칭 로직과는 별개다."""
        return [
            {
                "cpuCount": 0,
                "memorySize": 0,
                "productCode": "SW.VSVR.OS.WND64.WND.SVR2019EN.G003",
                "productItemKind": {"code": "SW"},
                "osType": {"code": "WND"},
                "osInfomation": "Windows Server 2019 (64-bit) English Edition",
                "priceList": [{"priceType": {"code": "MTRAT"}, "price": 24}],
            },
            {
                "cpuCount": 0,
                "memorySize": 0,
                "productCode": "SW.VSVR.OS.WND64.WND.SVR2022EN.G003",
                "productItemKind": {"code": "SW"},
                "osType": {"code": "WND"},
                "osInfomation": "Windows Server 2022 (64-bit) English Edition",
                "priceList": [{"priceType": {"code": "MTRAT"}, "price": 28}],
            },
        ]

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_fetch_windows_license_price_exact_version_match(self, mock_get) -> None:
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_windows_products()}},
        )

        result = fetcher.fetch_windows_license_price(version="2019")

        self.assertEqual(result.data["version"], "2019")
        self.assertEqual(result.data["krw_per_hour"], 24)

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_fetch_windows_license_price_falls_back_when_requested_version_missing(self, mock_get) -> None:
        """회귀 대상 시나리오: NCP 카탈로그에 Windows Server 2025가 아직 없다(2026-07-14
        기준 2016/2019/2022까지만) — 요청 버전이 없으면 최신 대체 버전(2022)으로
        폴백하고, 대체했다는 사실을 summary/data에 명시해야 한다(조용히 다른 값을
        쓰면 안 됨)."""
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_windows_products()}},
        )

        result = fetcher.fetch_windows_license_price(version="2025")

        self.assertEqual(result.data["requested_version"], "2025")
        self.assertEqual(result.data["version"], "2022")  # 최신 대체 버전
        self.assertEqual(result.data["krw_per_hour"], 28)
        self.assertIn("2022", result.summary)
        self.assertIn("대체", result.summary)

    def test_fetch_windows_license_price_missing_keys_raises_without_network_call(self) -> None:
        fetcher = NcpServerPriceFetcher(access_key=None, secret_key=None)

        with self.assertRaises(FetcherError):
            fetcher.fetch_windows_license_price()

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_fetch_windows_license_price_excludes_bare_metal_variant(self, mock_get) -> None:
        """회귀 테스트: 2026-07-14 실제 조회에서 재현 — Bare Metal 변형
        (productCode에 ".BM." 포함)이 VPC Server 변형과 osInfomation이 완전히
        같은 텍스트라 dict 키가 충돌하고, Bare Metal은 코어 수별 여러 가격 티어가
        섞여 있어 첫 MTRAT 항목을 집으면 부정확한 값이 나올 수 있었다. VPC Server
        productCode(".BM." 없음)만 선택되는지 고정해둔다."""
        products = self._sample_windows_products()
        products.append(
            {
                "cpuCount": 0,
                "memorySize": 0,
                "productCode": "SW.VSVR.BM.OS.WND64.WND.SVR2022EN",  # Bare Metal, VPC와 같은 osInfomation
                "productItemKind": {"code": "SW"},
                "osType": {"code": "WND"},
                "osInfomation": "Windows Server 2022 (64-bit) English Edition",
                "priceList": [{"priceType": {"code": "MTRAT"}, "price": 336}],  # Bare Metal 대형 코어 티어(오답)
            }
        )
        fetcher = NcpServerPriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200, json_data={"getProductPriceListResponse": {"productPriceList": products}}
        )

        result = fetcher.fetch_windows_license_price(version="2022")

        self.assertEqual(result.data["product_code"], "SW.VSVR.OS.WND64.WND.SVR2022EN.G003")
        self.assertEqual(result.data["krw_per_hour"], 28)  # Bare Metal의 336이 아님


class NcpStoragePriceFetcherTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="ncp-storage-price-cache-"))
        self.addCleanup(self._cleanup)

    def _cleanup(self) -> None:
        import shutil

        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def _sample_storage_products(self) -> list[dict]:
        """실제 STORAGE 카테고리 응답(2026-07-14, 실제 계정으로 확인)의 최소 재현.

        같은 productCode 안에 무관한 priceList 항목(IOPS 과금 등, price=0)도 섞어 둬서
        `productRatingType.code`/`unit.code`로 정확히 골라내는지 검증한다."""
        return [
            {
                "productCode": "SPBSTBSTAD000006",
                "productName": "Additional Block Storage [NET] (SSD)",
                "priceList": [
                    {
                        "priceType": {"code": "MTRAT"},
                        "productRatingType": {"code": "BST"},
                        "unit": {"code": "STRG_1G_HH"},
                        "price": 0.16,
                    },
                    {
                        "priceType": {"code": "MTRAT"},
                        "productRatingType": {"code": "BST_IOPS_CHARGE"},
                        "unit": {"code": "USAGE_HH"},
                        "price": 0,
                    },
                ],
            },
            {
                "productCode": "SPNAS00000000001",
                "productName": "Ncloud NAS",
                "priceList": [
                    {
                        "priceType": {"code": "MTRAT"},
                        "productRatingType": {"code": "NSSMS"},
                        "unit": {"code": "REQ_CNT"},
                        "price": 0,
                    },
                    {
                        "priceType": {"code": "MTRAT"},
                        "productRatingType": {"code": "NSSZ"},
                        "unit": {"code": "STRG_1G_HH"},
                        "price": 0.1,
                    },
                ],
            },
        ]

    def test_missing_keys_raises_without_network_call(self) -> None:
        fetcher = NcpStoragePriceFetcher(access_key=None, secret_key=None)

        with self.assertRaises(FetcherError):
            fetcher.fetch(storage_kind="block_ssd")

    def test_unknown_storage_kind_raises(self) -> None:
        fetcher = NcpStoragePriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)

        with self.assertRaises(FetcherError):
            fetcher.fetch(storage_kind="made-up")

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_fetch_block_ssd_returns_gb_hour_price(self, mock_get) -> None:
        fetcher = NcpStoragePriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_storage_products()}},
        )

        result = fetcher.fetch(storage_kind="block_ssd")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["krw_per_gb_hour"], 0.16)

    @patch("fetchers.ncp_price_fetcher.requests.get")
    def test_fetch_nas_returns_gb_hour_price_not_sms_zero(self, mock_get) -> None:
        fetcher = NcpStoragePriceFetcher(access_key="test-access", secret_key="test-secret", cache_dir=self.tmp_dir)
        mock_get.return_value = make_response(
            200,
            json_data={"getProductPriceListResponse": {"productPriceList": self._sample_storage_products()}},
        )

        result = fetcher.fetch(storage_kind="nas")

        self.assertEqual(result.status, "success")
        self.assertEqual(result.data["krw_per_gb_hour"], 0.1)  # NSSMS(0)가 아니라 NSSZ(0.1)가 선택돼야 함


if __name__ == "__main__":
    unittest.main()
