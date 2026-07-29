"""Gemini API 키 상태 확인 (실제 호출 — `pytest tests/` 밖에 두는 `verify_*.py` 관례).

`verify_judge_fault_injection.py` / `verify_ncp_price_fetcher.py`와 같은 성격이다:
자동 테스트는 실제 API를 절대 호출하지 않으므로, 실제 응답으로만 알 수 있는 것은
사람이 이 스크립트를 돌려 확인한다.

**무엇을 확인하나** (2026-07-29, 키를 종량제로 업그레이드한 뒤 만듦)

1. **키가 살아있고 한도가 안 걸리는가.** 무료 티어는 일 20회 한도가 있었다(실측).
   그 한도가 아직이면 첫 호출부터 429가 난다.
2. **`promptTokenCount`(입력 토큰)가 실제로 오는가.** 입력 비용 계산을 이 필드에
   의존하게 바꿨는데(2026-07-29) 그건 mock으로만 검증했다 — 필드명이 틀렸으면
   입력 비용이 조용히 0원으로 잡힌다.
3. **연속 호출 여유(속도 한도).** 측정 스크립트의 기본 간격 25초는 무료 티어 기준이다.
   종량제면 더 낮춰도 되는데, 얼마나 낮출 수 있는지는 실측해야 안다.

**비용**: 아주 짧은 프롬프트 몇 번이라 무시할 수준이다(출력 몇 토큰).
**키 값은 어떤 출력에도 찍지 않는다** — 변수 이름과 설정 여부만 보여준다.
"""
from __future__ import annotations

import io
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from harness.schemas import ProviderConfig  # noqa: E402
from providers.api_provider import GeminiApiProvider  # noqa: E402
from providers.base import ProviderError  # noqa: E402

MODEL_ID = "gemini-2.5-flash"
# 연속 호출 프로브 횟수. 무료 티어의 짧은 윈도우 한도(실측: limit 20, "retry in ~20s")에
# 걸리는지 보려는 것이라 크게 잡을 필요가 없다.
BURST = 5
PROMPT = "1+1은? 숫자만."


def make_provider() -> GeminiApiProvider:
    return GeminiApiProvider(
        ProviderConfig(provider_id="verify", model_id=MODEL_ID, auth_mode="api_key")
    )


def load_env_file() -> list[str]:
    """`.env`를 프로세스 환경으로 읽어온다(값은 출력하지 않는다). 변수 이름만 반환."""
    path = Path(".env")
    if not path.is_file():
        return []
    loaded = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")
        loaded.append(key.strip())
    return loaded


def main() -> int:
    loaded = load_env_file()
    print(f"[env] .env에서 주입한 변수: {loaded or '(없음)'}")
    if not os.environ.get("GEMINI_API_KEY"):
        print("[fatal] GEMINI_API_KEY가 없다 — harness-mvp/.env 또는 환경변수를 확인할 것")
        return 1
    print(f"[env] GEMINI_API_KEY 설정됨 (값은 출력하지 않음), model={MODEL_ID}")
    print()

    provider = make_provider()

    print("## 1. 호출 가능 여부 + 입력 토큰 필드 확인")
    try:
        candidate = provider.generate(PROMPT, temperature=0.0)
    except ProviderError as exc:
        quota = getattr(exc, "is_quota_error", False)
        print(f"[실패] {exc}")
        if quota:
            print(
                "  → 한도(429)다. 업그레이드가 아직 반영되지 않았거나 다른 한도에 걸렸다.\n"
                "     콘솔에서 결제/티어 상태를 확인할 것."
            )
        else:
            print("  → 한도 문제가 아니다(인증/형식 등). 메시지를 그대로 보고 판단할 것.")
        return 1

    print(f"  응답: {candidate.content.strip()[:40]!r}")
    print(f"  출력 토큰(candidatesTokenCount): {candidate.tokens}")
    print(f"  입력 토큰(promptTokenCount)    : {candidate.input_tokens}")
    print(f"  추정 비용: ${candidate.cost_usd}")
    if candidate.input_tokens is None:
        print(
            "  ⚠️ 입력 토큰이 None이다 — `promptTokenCount` 필드가 응답에 없다는 뜻이다.\n"
            "     `GeminiApiProvider._parse_input_tokens()`가 보는 경로를 실제 응답에 맞춰\n"
            "     고쳐야 한다. 안 고치면 입력 비용이 0원으로 잡혀 budget_usd 상한이 헐거워진다."
        )
    else:
        print("  ✅ 입력 비용 계산의 근거 필드가 실제로 온다(2026-07-29 수정분 검증 완료).")
    print()

    print(f"## 2. 연속 호출 여유 — 간격 없이 {BURST}회")
    print("   (무료 티어면 짧은 윈도우 한도에 걸려 중간에 429가 난다)")
    started = time.monotonic()
    ok = 0
    for index in range(1, BURST + 1):
        try:
            provider.generate(PROMPT, temperature=0.0)
            ok += 1
            print(f"   {index}/{BURST} 성공")
        except ProviderError as exc:
            print(f"   {index}/{BURST} 실패: {exc}")
            if getattr(exc, "is_quota_error", False):
                print(
                    f"   → {index}번째에서 한도에 걸렸다. 측정 스크립트의 --pace-seconds를\n"
                    f"      기본값(25초) 근처로 유지할 것."
                )
            break
    elapsed = time.monotonic() - started
    print(f"   결과: {ok}/{BURST} 성공, {elapsed:.1f}초 소요")
    print()

    print("## 판정")
    total_calls = 1 + ok
    if ok == BURST:
        print(
            f"  연속 {BURST}회가 간격 없이 전부 통과했다 — 무료 티어의 짧은 윈도우 한도보다\n"
            f"  여유가 있다는 뜻이다. 측정 시 `--pace-seconds`를 낮춰도 될 가능성이 높다\n"
            f"  (5~10초부터 시도하고 429가 나면 올릴 것)."
        )
    else:
        print(
            f"  연속 호출이 {ok}회에서 막혔다 — 속도 한도가 여전히 좁다.\n"
            f"  측정은 기본 간격(25초)을 유지할 것."
        )
    print(f"  이 확인에 쓴 호출: {total_calls}회 (짧은 프롬프트라 비용은 무시할 수준)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
