"""출력이 큰 작업을 codex/gemini에 넘기고, **요약과 경로만** 돌려받는다 (2026-08-13).

## 왜 필요한가

`gaebalai/claude-code-orchestrator`가 세운 규칙 — "출력이 큰 작업은 반드시 서브에이전트를
경유" — 을 이 저장소의 세션 작업에 적용하는 도구다. 아끼려는 자원은 **매니저(claude
세션)의 컨텍스트와 구독 한도**다.

측정으로 확인된 사실이 근거다: 이 세션의 토큰은 대화 길이가 아니라 **도구가 퍼올린 양**이
지배한다(파일 36% / 셸 29%). 그러니 3,000자짜리 문서를 세션이 직접 써서 응답에 담으면
그 3,000자가 이후 모든 턴의 컨텍스트에 실려 복리로 붙는다. 같은 문서를 codex가 쓰고
세션은 "12KB, 파일 여기 있음"만 받으면, 세션 컨텍스트에는 그 한 줄만 남는다.

**엔진(`harness-mvp/src/`)과 무관한 세션 층 도구다.** 하네스의 팀 패턴이 아니라, 사람과
claude가 이 저장소에서 일할 때 쓰는 스크립트다.

## 무엇을 지키는가

1. **본문은 stdout에 절대 싣지 않는다.** 이게 도구의 존재 이유다 — 본문을 찍으면
   세션 컨텍스트로 다시 들어와서 아낀 게 0이 된다. `--head`로 앞 몇 줄만 볼 수 있고,
   기본값은 0(아무것도 안 보여줌)이다.
2. **워커의 "만들었다"는 주장을 믿지 않는다.** 실행 후 파일이 실제로 존재하고 비어
   있지 않은지 직접 확인한다 — claude CLI가 "파일을 저장했습니다"라고 답하고 실제로는
   아무것도 안 만든 것을 실측으로 겪었다(`verify_candidate_boundary.py`).
3. **프롬프트는 파일로 받는다**(`--prompt-file`). 인자로 넘기면 8KB대 멀티바이트
   프롬프트가 Windows `.CMD` 경유에서 깨진다(2026-07-13 실측). 작업 규칙의 "`\n`이
   들어가는 내용은 heredoc으로 만들지 않는다"도 같이 지켜진다.
4. **격리된 임시 디렉터리에서 돌린다.** codex가 이 저장소를 실제 프로젝트로 인식해
   무관한 상태를 응답에 섞거나 파일을 고치는 걸 막는다. 워커에게 필요한 맥락은
   프롬프트에 담아 보낸다.
5. **키 값을 어떤 출력에도 찍지 않는다.**
6. **위임 기록을 남긴다**(`_workspace/delegation-log.jsonl`). 이 도구의 정당성은
   "컨텍스트를 얼마나 아꼈나"이므로 처음부터 계량한다 — 안 재면 ADR 0009와 같은
   실수(값이 없는 경로를 감으로 유지)를 반복한다.

## 쓰는 법

    # 1) 프롬프트를 파일로 쓴다 (Write 도구로 — heredoc 금지)
    # 2) 위임한다
    python harness-mvp/scripts/delegate.py \
        --backend codex --prompt-file <프롬프트> --out <산출물>

    # 3) 결과를 검증할 때만 필요한 구간을 읽는다 (전문 재독 금지)

## 언제 쓰고 언제 쓰지 않나

**쓴다** — 산출물이 문서/파일이고 세션이 본문을 머리에 담을 필요가 없을 때(가이드·절차서
초안, 대량 보일러플레이트, 큰 자료의 조사·요약).

**쓰지 않는다** — (a) 다음 판단을 하려면 내용이 세션 머리에 있어야 할 때(설계 판단,
특정 실패 디버깅), (b) 산출물이 작을 때(수십 줄 이하 — 위임 왕복이 절약보다 크다),
(c) 워커가 갖지 못한 대화 맥락이 필요할 때.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

_SCRIPT_DIR = Path(__file__).resolve().parent
_HARNESS_ROOT = _SCRIPT_DIR.parent
sys.path.insert(0, str(_HARNESS_ROOT / "src"))

# cp949 콘솔에서 한글 출력이 UnicodeEncodeError로 죽는 걸 막는다(이 저장소의 반복 함정).
if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

BACKENDS = ("codex", "gemini")
DEFAULT_TIMEOUT_SEC = 900.0  # 큰 산출물을 맡기는 용도라 후보 생성(420초)보다 넉넉하게

_LOG_PATH = _HARNESS_ROOT / "_workspace" / "delegation-log.jsonl"

# 한국어 혼합 텍스트의 대략적인 문자/토큰 비. 정확한 값이 아니라 **자릿수 감각용**이다 —
# 실제 토큰은 백엔드가 알려주는 값(codex의 turn.completed, gemini의 usageMetadata)을 쓴다.
_CHARS_PER_TOKEN_ROUGH = 1.6


def _resolve_executable(name: str) -> str:
    """Windows에서 npm이 만든 `.CMD` 래퍼를 찾는다(`cli_subscription_provider`와 같은 문제).

    `shutil.which`가 확장자까지 붙은 실제 경로를 돌려주므로 그대로 쓴다.
    """
    found = shutil.which(name)
    if not found:
        raise SystemExit(
            f"[fatal] `{name}` 실행 파일을 찾지 못했다. 설치 여부와 PATH를 확인할 것."
        )
    return found


def _load_env_file() -> None:
    """`harness-mvp/.env`를 프로세스 환경으로 주입한다. **값은 출력하지 않는다.**"""
    path = _HARNESS_ROOT / ".env"
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def run_codex(prompt: str, *, timeout: float) -> tuple[str, dict]:
    """codex CLI에 위임한다. 최종 응답은 `--output-last-message` 파일로 받는다.

    인자 구성은 `providers/cli_subscription_provider.py`가 실제 계정으로 검증한 것을
    그대로 쓴다 — 프롬프트는 stdin, cwd는 격리된 임시 디렉터리, `--skip-git-repo-check`.
    """
    from providers.cli_subscription_provider import _extract_codex_output_tokens

    executable = _resolve_executable("codex")
    with tempfile.TemporaryDirectory(prefix="delegate-codex-") as tmp:
        last_message = Path(tmp) / "last-message.txt"
        result = subprocess.run(
            [executable, "exec", "--json", "--skip-git-repo-check",
             "--output-last-message", str(last_message)],
            input=prompt,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            cwd=tmp,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"codex 종료 코드 {result.returncode}: {(result.stderr or '').strip()[:400]}"
            )
        if not last_message.exists():
            raise RuntimeError("codex가 최종 응답 파일을 만들지 않았다(--output-last-message)")
        content = last_message.read_text(encoding="utf-8").strip()
        if not content:
            raise RuntimeError("codex의 최종 응답이 비어 있다")
        return content, {"output_tokens": _extract_codex_output_tokens(result.stdout)}


def run_gemini(prompt: str, *, timeout: float, model: str) -> tuple[str, dict]:
    """gemini에 위임한다. **CLI가 없어서 하네스의 검증된 REST 경로를 재사용한다.**

    `GeminiApiProvider`는 입력/출력 토큰과 추정 비용까지 채워주므로, 종량제인 이 백엔드는
    위임 기록에 실제 금액이 남는다(구독인 codex는 금액이 None이고 호출 수로만 잡힌다).
    """
    from harness.schemas import ProviderConfig
    from providers.api_provider import GeminiApiProvider

    _load_env_file()
    if not os.environ.get("GEMINI_API_KEY"):
        raise SystemExit(
            "[fatal] GEMINI_API_KEY가 없다 — harness-mvp/.env 또는 환경변수를 확인할 것."
        )
    provider = GeminiApiProvider(
        ProviderConfig(provider_id="delegate", model_id=model, auth_mode="api_key"),
        timeout_sec=timeout,
    )
    candidate = provider.generate(prompt, temperature=0.7)
    if candidate.status != "success" or not candidate.content.strip():
        raise RuntimeError(f"gemini 응답이 비었거나 실패다: {candidate.content[:300]}")
    return candidate.content.strip(), {
        "output_tokens": candidate.tokens,
        "input_tokens": candidate.input_tokens,
        "cost_usd": candidate.cost_usd,
    }


def _append_log(record: dict) -> None:
    """위임 1건을 기록한다. 실패해도 위임 자체를 망치지 않는다(기록은 부수적)."""
    try:
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with _LOG_PATH.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        print(f"  (기록 실패 — 위임 자체는 성공했다: {exc})")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="출력이 큰 작업을 codex/gemini에 위임하고 요약과 경로만 돌려받는다.",
    )
    parser.add_argument("--backend", required=True, choices=BACKENDS)
    parser.add_argument(
        "--prompt-file", required=True, type=Path,
        help="지시문 파일. 인자로 직접 넘기지 않는 이유는 모듈 docstring 참고.",
    )
    parser.add_argument("--out", required=True, type=Path, help="산출물을 쓸 경로")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SEC)
    parser.add_argument("--model", default="gemini-2.5-flash", help="gemini 백엔드에만 적용")
    parser.add_argument(
        "--head", type=int, default=0, metavar="N",
        help="산출물 앞 N줄을 보여준다. 기본 0 — 본문을 stdout에 싣지 않는 게 이 도구의 목적이다.",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="--out이 이미 있으면 덮어쓴다. 기본은 거부(실수로 산출물을 지우지 않게).",
    )
    args = parser.parse_args()

    if not args.prompt_file.is_file():
        raise SystemExit(f"[fatal] 프롬프트 파일이 없다: {args.prompt_file}")
    if args.out.exists() and not args.overwrite:
        raise SystemExit(
            f"[fatal] 산출물 경로가 이미 있다: {args.out}\n"
            f"        덮어쓰려면 --overwrite를 줄 것(무엇을 지우는지 먼저 확인하고)."
        )

    prompt = args.prompt_file.read_text(encoding="utf-8")
    if not prompt.strip():
        raise SystemExit(f"[fatal] 프롬프트 파일이 비어 있다: {args.prompt_file}")

    print(f"위임 → {args.backend}"
          + (f" ({args.model})" if args.backend == "gemini" else " (구독)"))
    print(f"  지시문 {len(prompt):,}자 / 상한 {args.timeout:.0f}초")
    print("  본문은 여기에 찍지 않는다 — 그게 이 도구의 목적이다.")
    print()

    started = time.monotonic()
    try:
        if args.backend == "codex":
            content, usage = run_codex(prompt, timeout=args.timeout)
        else:
            content, usage = run_gemini(prompt, timeout=args.timeout, model=args.model)
    except subprocess.TimeoutExpired:
        print(f"[실패] {args.timeout:.0f}초 상한 초과 — 산출물 없음")
        return 1
    except (RuntimeError, Exception) as exc:  # noqa: BLE001 - 어떤 실패든 그대로 보고한다
        if isinstance(exc, SystemExit):
            raise
        print(f"[실패] {type(exc).__name__}: {exc}")
        return 1
    elapsed = time.monotonic() - started

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(content, encoding="utf-8")

    # **주장을 믿지 않고 파일을 직접 확인한다.**
    if not args.out.is_file() or args.out.stat().st_size == 0:
        print(f"[실패] 산출물이 실제로 만들어지지 않았다: {args.out}")
        return 1

    chars = len(content)
    lines = content.count("\n") + 1
    saved_tokens_rough = int(chars / _CHARS_PER_TOKEN_ROUGH)

    print(f"[성공] {elapsed:.1f}초")
    print(f"  산출물 : {args.out}")
    print(f"  크기   : {chars:,}자 / {lines:,}줄 / {args.out.stat().st_size:,}바이트")
    if usage.get("output_tokens") is not None:
        print(f"  출력토큰: {usage['output_tokens']:,} (백엔드 보고값)")
    if usage.get("cost_usd") is not None:
        print(f"  비용   : ${usage['cost_usd']}")
    else:
        print("  비용   : 구독 호출 1회 (금액 미집계)")
    print(f"  세션이 안 실은 양: 약 {saved_tokens_rough:,} 토큰 (문자수/{_CHARS_PER_TOKEN_ROUGH} 추정)")

    if args.head > 0:
        print()
        print(f"  --- 앞 {args.head}줄 (검증용) ---")
        for line in content.splitlines()[: args.head]:
            print(f"  {line}")

    _append_log({
        "at": datetime.now(timezone.utc).isoformat(),
        "backend": args.backend,
        "model": args.model if args.backend == "gemini" else "codex",
        "prompt_chars": len(prompt),
        "output_chars": chars,
        "output_lines": lines,
        "out_path": str(args.out),
        "latency_sec": round(elapsed, 1),
        "output_tokens": usage.get("output_tokens"),
        "input_tokens": usage.get("input_tokens"),
        "cost_usd": usage.get("cost_usd"),
        "context_tokens_avoided_rough": saved_tokens_rough,
    })
    print()
    print(f"  기록: {_LOG_PATH.relative_to(_HARNESS_ROOT.parent)} (gitignore 대상)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
