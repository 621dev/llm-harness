"""ADR 0004 재검토 트리거 1단계: judge_provider의 fault-injection 검증.

`docs/adr/0004-judge-real-llm-upgrade.md`의 재검토 트리거 1단계("단일 LLM
판단이 fault-injection 회귀 테스트를 통과하는지 확인")를 구현한다. **의도적으로**
`pytest tests/`에 넣지 않았다 — 작업 규칙("자동 테스트는 실제 API/CLI 절대
미호출")을 지키려면 실제 judge_provider(Gemini API)를 호출하는 이 스크립트는
자동 테스트 스위트 밖에 있어야 한다. 대신 사람이 직접 실행하고 결과를
진행상황 문서에 기록한다(Phase 3의 수동 e2e 검증과 같은 패턴).

각 케이스는 candidate 두 개를 손으로 써서 "정답 vs 길지만 틀린 답"과
"길지만 정답 vs 짧지만 틀린 답"을 양방향으로 준비했다 — judge가 길이가
아니라 정확성으로 판단하는지 확인하려는 것이다(한쪽 방향만 확인하면 "항상
짧은 쪽을 고른다"는 정반대 편향도 통과해버릴 수 있어서 양방향이 필요하다).

사용법 (harness-mvp 디렉토리에서, GEMINI_API_KEY 필요):
  PYTHONPATH=src python scripts/verify_judge_fault_injection.py
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import judge  # noqa: E402
from harness.schemas import Candidate, ProviderConfig  # noqa: E402
from providers.api_provider import GeminiApiProvider  # noqa: E402

_CORRECT_ANSWER = "37 곱하기 4는 148입니다."
# 오답(150)을 의도적으로 자연스럽게 숨긴다 — 판정 근거가 텍스트에 "계산
# 실수가 있음"이라고 스스로 밝히면 키워드 매칭만으로도 잡히므로 fault
# injection의 의미가 없다. 실제 judge가 산수를 스스로 검증해야 잡아낼 수
# 있게, 자신감 있는 어조로 틀린 결론만 제시한다.
_WRONG_BUT_ELABORATE = (
    "좋은 질문입니다. 곱셈을 단계별로 차근차근 풀어보겠습니다.\n\n"
    "1단계: 37을 30과 7로 나눕니다.\n"
    "2단계: 30 곱하기 4는 120입니다.\n"
    "3단계: 7 곱하기 4는 30입니다.\n"
    "4단계: 120과 30을 더하면 150입니다.\n\n"
    "따라서 37 곱하기 4는 150입니다. 이렇게 자릿수를 나눠서 계산하면 "
    "복잡한 곱셈도 실수 없이 빠르게 처리할 수 있습니다. 추가로 검산이 "
    "필요하시면 다른 방식(예: 37을 40에서 3을 뺀 값으로 보고 40*4-3*4로 "
    "계산)으로도 확인해보시길 권장합니다."
)


def _case_short_correct_beats_long_wrong() -> tuple[bool, str]:
    candidates = [
        Candidate(model_id="short-correct", content=_CORRECT_ANSWER, status="success"),
        Candidate(model_id="long-wrong", content=_WRONG_BUT_ELABORATE, status="success"),
    ]
    judging = judge.evaluate(candidates, rubric=["정확성"], judge_provider=_judge_provider())
    passed = judging.winner == "short-correct"
    return passed, f"winner={judging.winner} (기대: short-correct), scores={_scores(judging)}"


def _case_long_correct_beats_short_wrong() -> tuple[bool, str]:
    candidates = [
        Candidate(
            model_id="long-correct",
            content=f"{_CORRECT_ANSWER} 검산: 37을 40-3으로 보면 40*4=160, 3*4=12, 160-12=148로 동일합니다.",
            status="success",
        ),
        Candidate(model_id="short-wrong", content="37 곱하기 4는 150입니다.", status="success"),
    ]
    judging = judge.evaluate(candidates, rubric=["정확성"], judge_provider=_judge_provider())
    passed = judging.winner == "long-correct"
    return passed, f"winner={judging.winner} (기대: long-correct), scores={_scores(judging)}"


def _scores(judging) -> dict[str, float]:
    return {s.candidate: s.score for s in judging.scores}


def _judge_provider() -> GeminiApiProvider:
    return GeminiApiProvider(ProviderConfig(provider_id="judge", model_id="gemini-2.5-flash", auth_mode="api_key"))


def main() -> int:
    # Windows 기본 콘솔 코드페이지(cp949)는 em-dash(—) 등을 인코딩 못 해서
    # UnicodeEncodeError로 죽는다 — cli.py의 main()과 동일한 이유로 동일하게 고친다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    cases = {
        "짧고 정확한 답 vs 길지만 틀린 답": _case_short_correct_beats_long_wrong,
        "길지만 정확한 답 vs 짧고 틀린 답": _case_long_correct_beats_short_wrong,
    }

    all_passed = True
    for name, case_fn in cases.items():
        try:
            passed, detail = case_fn()
        except judge.JudgeError as exc:
            passed, detail = False, f"judge 호출 실패: {exc}"
        status = "PASS" if passed else "FAIL"
        print(f"[{status}] {name} — {detail}")
        all_passed = all_passed and passed

    print()
    print("전부 통과 — 단일 judge로 충분, ADR 0004 트리거 2단계(Self-Consistency) 불필요" if all_passed
          else "일부 실패 — ADR 0004 트리거 2단계(Self-Consistency) 검토 필요")
    return 0 if all_passed else 1


if __name__ == "__main__":
    sys.exit(main())
