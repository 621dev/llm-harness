"""`measure_pattern_value.py`의 로직을 mock provider로 확인한다 (비용 0).

**왜 필요한가**: 측정 스크립트는 실제 API를 호출하므로 `pytest tests/` 밖에 있고
(작업 규칙), 그래서 **로직 결함이 돈을 쓴 뒤에야 드러난다.** 실제로 그렇게 됐다:

- 4차 측정에서 `step_input_tokens`가 전부 `None`으로 나왔다(`plan.json`의
  `output_ref`가 실행 전 저장이라 null인 걸 몰랐다)
- `direct` 조건에만 `step_input_tokens` 키가 없어 증폭 배수를 계산할 수 없었다
- 체인 역할이 3개로 늘었을 때 provider 등록 누락으로 스크립트가 `KeyError`로 죽었다

셋 다 **mock 한 번이면 잡혔을 것들**이다. 이 스크립트는 측정을 시작하기 전에
`--conditions` 전 조건을 mock으로 한 바퀴 돌려서, 실제 호출로 확인할 것을
"품질 숫자"만 남긴다.

**확인 대상은 측정 장치이지 품질이 아니다** — mock의 합격/불합격 결과에는 아무
의미가 없다. 보는 것은 구조뿐이다:

1. 조건마다 결과 dict가 필요한 키를 다 채우는가(특히 `step_input_tokens`)
2. `fan_out`에서 후보 산출물이 **서로 덮어쓰이지 않는가**(`_LabeledProvider`)
3. 정면 비교가 조건을 되돌려 짚는가(blind 레이블 → 조건 이름)
4. 예상 호출 수 계산이 실제 호출 수와 맞는가

사용법 (harness-mvp 디렉토리에서):
  PYTHONPATH=src python scripts/verify_measure_script.py
"""
from __future__ import annotations

import io  # noqa: F401  (measure_pattern_value가 stdout 래핑에 쓴다 — 위 주석 참고)
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

# **여기서 sys.stdout을 감싸지 않는다.** measure_pattern_value가 import 시점에
# `sys.stdout = io.TextIOWrapper(sys.stdout.buffer, ...)`를 하는데, 이쪽에서 먼저
# 감싸두면 그 wrapper가 GC될 때 밑의 buffer까지 닫혀서 이후 print가 전부
# `ValueError: I/O operation on closed file`로 죽는다(실제로 겪음).
# 인코딩 처리는 아래 import가 대신 해준다.
import measure_pattern_value as m  # noqa: E402
from harness.schemas import Candidate, ProviderConfig  # noqa: E402
from providers.base import Provider  # noqa: E402

# 실제 호출 횟수를 세서 `_CALLS_PER_ATTEMPT` 추정과 맞는지 확인한다.
_CALL_LOG: list[str] = []


class _FakeProvider(Provider):
    """호출을 기록하고 판정 가능한 JSON을 돌려주는 mock.

    judge/evaluator로도 쓰이므로 프롬프트를 보고 어떤 형식을 원하는지 판단한다 —
    실제 provider는 프롬프트가 뭐든 텍스트를 돌려주고 파싱은 judge가 하므로,
    여기서도 **judge의 파서를 실제로 통과하는 응답**을 만들어야 검증에 의미가 있다.
    """

    def generate(self, prompt: str, *, temperature: float = 0.7) -> Candidate:
        _CALL_LOG.append(self.model_id)
        if "출력 형식 (JSON, 키는 반드시" in prompt:
            # judge.evaluate(N개 비교) — 프롬프트에 등장한 레이블을 그대로 채운다.
            labels = [ln.split()[-1] for ln in prompt.splitlines() if ln.startswith("### 후보 ")]
            body = ", ".join(
                f'"{label}": {{"score": {70 + 5 * i}, "flaws": ["mock 결함"]}}'
                for i, label in enumerate(labels)
            )
            content = "{" + body + "}"
        elif '"passed"' in prompt:
            content = '{"unmet_items": [], "passed": true, "feedback": ""}'
        else:
            content = f"mock 본문 ({self.model_id}) — 방화벽/포트/DNS 설명 " + "가" * 200
        return Candidate(
            model_id=self.model_id, content=content, tokens=120, input_tokens=48,
            latency_ms=10, cost_usd=0.0001, status="success",
        )


def _fake(provider_id: str) -> Provider:
    return _FakeProvider(ProviderConfig(provider_id=provider_id, model_id="mock-model", auth_mode="api_key"))


def _check(label: str, ok: bool, detail: str = "") -> bool:
    print(f"  [{'ok' if ok else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")
    return ok


def main() -> int:
    # **가로채는 지점은 최하단(`_make`)이다.** provider를 만드는 모든 경로가 여기를
    # 지나므로(`_generator`/`_evaluator`/`_with_safety_net` 전부), 스크립트에 새 경로가
    # 생겨도 자동으로 덮인다.
    #
    # 처음엔 `_generator`/`_evaluator`만 갈아꼈는데, `fan_out_mixed`를 추가할 때
    # `run_fan_out`이 `_with_safety_net`을 직접 부르게 바뀌면서 **그 경로가 가로채기를
    # 빠져나가 실제 claude/codex CLI를 호출했다**(구독 2회 소모, 2026-07-29). 비용 0을
    # 보장해야 하는 스크립트가 돈을 쓴 것이다 — 상위 함수를 patch하면 그 아래로 새 길이
    # 생길 때 조용히 구멍이 난다.
    #
    # 나머지(조건 함수, orchestrator, judge, 정면 비교)는 전부 실제 코드를 지난다.
    m._make = lambda backend, provider_id: _fake(provider_id)
    m._FALLBACK_WRAPPERS.clear()

    root = Path("_workspace/measurements/_verify_mock")
    root.mkdir(parents=True, exist_ok=True)

    failures = 0
    conditions = list(m.CONDITIONS)
    results = []

    for label in conditions:
        print(f"\n## 조건 {label!r}")
        _CALL_LOG.clear()
        result = m.evaluate(m._run_condition(label, 1, root))
        results.append(result)

        required = {"condition", "attempt", "ok", "content", "latency_ms", "cost_usd",
                    "subscription_calls", "llm_calls", "roles", "step_input_tokens",
                    "passed", "feedback"}
        missing = required - set(result)
        failures += not _check("결과 키 완비", not missing, f"누락 {sorted(missing)}" if missing else "")
        failures += not _check("실행 성공", result["ok"], (result.get("content") or "")[:80])
        # 4차 측정에서 전부 None으로 나왔던 자리 — 증폭 배수 계산의 전제다.
        tokens = result["step_input_tokens"]
        failures += not _check(
            "step_input_tokens 채워짐", bool(tokens) and all(t is not None for t in tokens), f"{tokens}"
        )
        failures += not _check(
            "예상 호출 수 == 실제", m._CALLS_PER_ATTEMPT[label] == len(_CALL_LOG),
            f"추정 {m._CALLS_PER_ATTEMPT[label]} / 실제 {len(_CALL_LOG)}회",
        )

        if label.startswith("fan_out"):
            expected_candidates = (
                len(m.MIXED_FAN_OUT_BACKENDS) if label == "fan_out_mixed" else m.FAN_OUT_CANDIDATES
            )
            cand_dir = root / f"run-measure-{label}-1" / "artifacts" / "candidates"
            files = sorted(p.name for p in cand_dir.glob("*.md")) if cand_dir.is_dir() else []
            # 슬롯 이름이 안 붙으면 파일 하나로 덮어써진다 — `_LabeledProvider`의 존재 이유.
            failures += not _check(
                f"후보 산출물 {expected_candidates}개가 각각 남음",
                len(files) == expected_candidates, f"{files}",
            )
            # 다양성 조건은 어느 백엔드를 썼는지가 곧 측정 대상이라 결과에 남아야 한다.
            if label == "fan_out_mixed":
                failures += not _check(
                    "후보 백엔드 조합이 결과에 기록됨",
                    result["roles"] == list(m.MIXED_FAN_OUT_BACKENDS), f"{result['roles']}",
                )

    print("\n## 정면 비교")
    _CALL_LOG.clear()
    pairs = m.head_to_head(results, conditions, pace=0)
    expected_pairs = len(conditions) - 1
    failures += not _check("비교 쌍 수", len(pairs) == expected_pairs, f"{len(pairs)}쌍 (기대 {expected_pairs})")
    failures += not _check("호출 수 == 쌍 수", len(_CALL_LOG) == expected_pairs, f"{len(_CALL_LOG)}회")
    # 승자가 조건 이름으로 되돌아오는지 — blind 레이블(A/B)이 그대로 새어나오면 실패다.
    winners = [p.get("winner") for p in pairs]
    failures += not _check(
        "승자가 조건 이름으로 기록됨", all(w in conditions for w in winners), f"{winners}"
    )
    summaries = m.summarize_head_to_head(pairs)
    failures += not _check("요약 생성", len(summaries) == expected_pairs, f"{len(summaries)}건")
    for s in summaries:
        print(f"    {s['other']} vs {s['baseline']}: "
              f"{s['other_wins']}승 {s['baseline_wins']}패 "
              f"(근소 {s['within_engine_tie_band']}쌍, "
              f"점수 {s['avg_score_other']} vs {s['avg_score_baseline']})")

    print(f"\n{'[ok] 전부 통과' if not failures else f'[fail] {failures}건 실패'}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
