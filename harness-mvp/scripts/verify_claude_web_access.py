"""claude 단발 호출(`generate` 경로)에 웹 검색이 열려 있는지 + 그 토큰 비용 실측.

`pytest tests/` 밖에 두는 `verify_*.py` 관례(자동 테스트는 실제 CLI 미호출).
`verify_agent_boundary.py`처럼 **CLI를 직접** 부른다 — provider가 `usage` 딕셔너리를
버리고 출력 토큰만 남기기 때문에, 입력 토큰까지 보려면 원본 JSON이 필요하다.

## 왜 확인하나 (2026-07-29)

3차 측정에서 **9건 중 6건이 "출처 미명시"로 불합격**했다. rubric에 `출처 신뢰성`이
있는데 gemini API는 검색 도구를 안 보내므로 **달성 불가능한 항목**일 가능성이 있었다.
그런데 확인해보니 경로마다 상태가 다르다:

- `agentic_task`: `--disallowedTools`에 `WebFetch,WebSearch` — **명시적 차단**(ADR 0007)
- Gemini API: 요청에 `tools`를 안 보냄 — **불가**
- **`ClaudeCliProvider._invoke`: 아무 도구 제한이 없다** — cwd만 빈 임시 디렉터리로
  격리했고(CLAUDE.md/git 유출 방지, 2026-07-14) 그건 파일 얘기지 네트워크 얘기가 아니다

즉 **같은 rubric인데 백엔드에 따라 달성 가능성이 다를 수 있다** — 백엔드 비교를
오염시키는 문제다. ADR 0007에서 "문서를 믿었더니 경계가 뚫려 있었다"를 겪었으므로
추측하지 않고 실측한다.

## 왜 소모량까지 재나

검색이 **되는지**와 **써도 되는지**는 다른 질문이다. 검색 결과는 컨텍스트로 주입되므로
입력 토큰이 크게 늘어난다. 구독 백엔드는 `cost_usd`가 None이라 금액으로는 안 보이지만
5시간/주간 롤링 한도는 토큰을 소모한다 — 그래서 검색 프롬프트와 대조 프롬프트의
토큰을 나란히 재서 "쓸 만한가"를 판단할 재료를 만든다.

**비용**: 구독 호출 2회. 짧은 프롬프트지만 검색이 실제로 돌면 입력 토큰이 커질 수 있다.
"""
from __future__ import annotations

import io
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from providers.cli_subscription_provider import DEFAULT_TIMEOUT_SEC  # noqa: E402

# `ClaudeCliProvider._invoke`와 **똑같은 인자**로 부른다. 여기서 다르게 부르면
# "우리 코드가 실제로 하는 호출"을 검증하는 게 아니게 된다.
_ARGS = ["--print", "--output-format", "json", "--input-format", "text"]

# 검색 없이는 답할 수 없는 것을 요구한다 — 모델 내부 지식으로 답할 수 있는 질문이면
# 검색이 됐는지 안 됐는지 구분이 안 된다.
SEARCH_PROMPT = (
    "웹에서 검색해서, 지금 접근 가능한 실제 URL 2개를 출처로 제시하며 답하라. "
    "질문: Linux 방화벽 설정 도구 firewalld의 공식 문서는 어디에 있는가? "
    "검색 도구를 쓸 수 없으면 '검색 불가'라고만 답하라."
)
# 대조군: 같은 주제·비슷한 길이인데 검색을 요구하지 않는다. 토큰 차이가 검색 때문인지
# 프롬프트 길이 때문인지 구분하기 위한 것.
CONTROL_PROMPT = (
    "Linux 방화벽 설정 도구 firewalld의 역할을 초급 엔지니어에게 설명하라. "
    "출처나 URL은 제시하지 말고 개념만 3문장으로 답하라."
)

_URL_PATTERN = re.compile(r"https?://[^\s\)\]]+")


def call_claude(prompt: str) -> dict:
    executable = shutil.which("claude")
    if executable is None:
        raise SystemExit("[fatal] claude CLI를 PATH에서 찾을 수 없다 — `claude auth login` 확인")
    with TemporaryDirectory() as tmp_dir:
        result = subprocess.run(
            [executable, *_ARGS],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=DEFAULT_TIMEOUT_SEC,
            encoding="utf-8",
            cwd=tmp_dir,  # _invoke와 동일: CLAUDE.md/git 격리
        )
    if result.returncode != 0:
        raise SystemExit(f"[fatal] claude CLI 종료 코드 {result.returncode}: {result.stderr.strip()[:300]}")
    return json.loads(result.stdout)


def describe(label: str, data: dict) -> dict:
    content = data.get("result") or ""
    usage = data.get("usage") or {}
    urls = _URL_PATTERN.findall(content)
    print(f"### {label}")
    print(f"  응답 앞부분: {content.strip()[:160]!r}")
    print(f"  URL 개수: {len(urls)}  {urls[:3]}")
    print(f"  usage: {json.dumps(usage, ensure_ascii=False)}")
    print(f"  turns: {data.get('num_turns')}  duration_ms: {data.get('duration_ms')}")
    print()
    return {"content": content, "urls": urls, "usage": usage, "num_turns": data.get("num_turns")}


def total_input(usage: dict) -> int:
    """입력 계열 토큰 합계. 캐시 읽기/쓰기도 입력으로 센다(한도를 소모하는 건 같다)."""
    return sum(
        int(usage.get(key) or 0)
        for key in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    )


def main() -> int:
    print("claude 단발 호출(`generate` 경로)의 웹 접근 여부 + 토큰 소모 실측")
    print(f"인자: {' '.join(_ARGS)}  (ClaudeCliProvider._invoke와 동일)")
    print("구독 호출 2회를 씁니다.\n")

    search = describe("1. 검색 요구 프롬프트", call_claude(SEARCH_PROMPT))
    control = describe("2. 대조군(검색 미요구)", call_claude(CONTROL_PROMPT))

    print("## 판정 — 웹 접근")
    said_no = "검색 불가" in search["content"]
    if search["urls"] and not said_no:
        print("  ⚠️ URL이 응답에 포함됐다. 다만 이것만으로 '검색했다'고 단정할 수 없다 —")
        print("     모델이 기억하는 URL을 쓴 것일 수도 있다. num_turns가 1보다 크거나")
        print("     usage에 도구 사용 흔적이 있으면 실제 검색으로 볼 근거가 된다.")
        print(f"     num_turns: 검색={search['num_turns']} / 대조={control['num_turns']}")
    elif said_no:
        print("  ✅ 모델이 '검색 불가'라고 답했다 — 이 경로에 검색 도구가 열려 있지 않다.")
    else:
        print("  URL도 없고 '검색 불가'도 아니다 — 응답 전문을 보고 사람이 판단할 것.")
    print()

    print("## 판정 — 토큰 소모 (적용 여부의 근거)")
    s_in, c_in = total_input(search["usage"]), total_input(control["usage"])
    s_out = int(search["usage"].get("output_tokens") or 0)
    c_out = int(control["usage"].get("output_tokens") or 0)
    print(f"  입력: 검색 {s_in:,} vs 대조 {c_in:,}" + (f"  → {s_in / c_in:.1f}배" if c_in else ""))
    print(f"  출력: 검색 {s_out:,} vs 대조 {c_out:,}")
    print(
        "  구독 백엔드는 cost_usd가 None이라 금액으로 안 보이지만 5시간/주간 롤링 한도는\n"
        "  토큰을 소모한다 — 입력이 몇 배 늘면 그만큼 한도가 빨리 마른다."
    )
    print()
    print("## 이 결과로 정할 것")
    print("  - 검색이 안 되면: rubric의 `출처 신뢰성`은 달성 불가 항목이므로 제거 검토")
    print("  - 검색이 되면: 백엔드별로 달성 가능성이 달라 비교가 오염된다 —")
    print("    rubric을 유지할지, 백엔드를 고정할지, 도구를 명시적으로 끌지 결정 필요")
    print("  - 입력 토큰이 크게 늘면: 되더라도 상시 사용은 비싸다(선택적 사용 검토)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
