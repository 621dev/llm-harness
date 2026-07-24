"""NCP getProductPriceList 실제 API 1회 수동 검증 (ADR 0004의 fault-injection 스크립트와
같은 관행 — 의도적으로 `pytest tests/` 밖에 둔다. 실제 NCP 계정/키가 필요하다).

`harness-mvp/.env` 파일(NCP_ACCESS_KEY=.../NCP_SECRET_KEY=... 등 모든 키를 한
파일에 모아둠)을 직접 읽어 프로세스 환경변수로만 설정한다 — 키 값 자체는 어떤
print()에도 등장하지 않는다.

2026-07-13: 이 스크립트로 실제 계정 호출 → 서명 URI 접두사 버그(401) 발견/수정,
productCategoryCode="COMPUTE" 확인, MTRAT(시간당)/FXSUM(월정액) 구분 확인 →
ncp_price_fetcher.py의 fetch()를 실제 스펙으로 완성. 지금은 그 fetch()를 그대로
호출해서 최종 확인만 한다.
"""
from __future__ import annotations

import sys
from pathlib import Path

_HARNESS_MVP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_HARNESS_MVP_ROOT / "src"))

_ENV_FILE = _HARNESS_MVP_ROOT / ".env"


def _load_env_file(path: Path) -> None:
    import os

    if not path.exists():
        print(f"[fatal] {path} 이 없습니다. NCP_ACCESS_KEY/NCP_SECRET_KEY 두 줄로 만들어주세요.")
        sys.exit(1)

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ[key.strip()] = value.strip()


def main() -> None:
    _load_env_file(_ENV_FILE)

    from fetchers.base import FetcherError
    from fetchers.ncp_price_fetcher import NcpServerPriceFetcher

    fetcher = NcpServerPriceFetcher()
    if not fetcher.access_key or not fetcher.secret_key:
        print("[fatal] 환경변수 로드는 됐는데 access_key/secret_key가 비어있습니다 — .env 형식을 확인하세요.")
        sys.exit(1)
    print(f"[ok] 키 로드됨 (access_key 길이={len(fetcher.access_key)}, secret_key 길이={len(fetcher.secret_key)})")

    try:
        result = fetcher.fetch(vcpu=8, memory_gb=16)
    except FetcherError as exc:
        print(f"[error] fetch 실패: {exc}")
        sys.exit(1)

    print(f"[ok] status={result.status}")
    print(f"[ok] summary={result.summary}")
    print(f"[ok] candidates 개수={len(result.data['candidates'])}")
    for c in result.data["candidates"][:5]:
        print(f"    {c['krw_per_hour']}원/시간  {c['product_name']}  ({c['product_code']})")


if __name__ == "__main__":
    main()
