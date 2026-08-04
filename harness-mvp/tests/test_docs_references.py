"""문서의 참조와 설정값이 실제와 일치하는지 기계적으로 검사한다 (2026-08-03 신설).

**왜 필요했나**: 작업 규칙의 "감시는 사람 기억이 아니라 테스트로 한다"를 서술문에도
적용한다. 이미 있던 기계적 점검(테스트 개수, 모듈 크기, import 계층, rubric 일치)은
전부 **코드↔코드** 또는 **숫자 하나**만 봤고, **문서가 가리키는 것이 아직 존재하는지**는
아무도 안 봤다. 그래서 2026-08-03 하루에 낡은 서술을 다섯 건 손으로 찾아 고쳤다:

- 작업 규칙이 `fan_out_judge`/`hierarchical_delegation`을 "측정 대기"로 적고 있었다
  (ADR 0009/0010으로 결론 난 뒤에도) — **구조 정리를 실제로 막는 살아 있는 규칙**이었다
- README/인수인계 문서의 ADR 목록이 0008에 멈춰 있었다(실제 0011)
- README의 config 예시가 `max_subscription_candidates: 1`이었다(실제 2)
- 인수인계 v20 → v21 전환에서 `v20 §N` 포인터 15건을 grep으로 찾아 고쳤다 —
  하나라도 놓쳤으면 조용히 끊어졌다

세 번째·네 번째 유형만 기계가 볼 수 있다. "측정 대기"가 아직 사실인지 같은 **서술의
진실성은 자동화가 안 된다** — 그건 사람이 phase 종료 시 재검토하는 몫으로 남는다.

**오탐을 만들지 않는 게 이 테스트의 최우선 조건이다.** 시끄러운 검사는 무시되다가
지워진다. 그래서 placeholder(`<worktree>`, `vN`, `NNNN`)와 코드 블록 안 예시는
의도적으로 건너뛴다 — 놓치는 것보다 오탐이 더 나쁘다.
"""
from __future__ import annotations

import json
import re
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PROGRESS_DIR = _REPO_ROOT / "docs" / "03_진행상황"
_ADR_DIR = _REPO_ROOT / "harness-mvp" / "docs" / "adr"


def _markdown_files() -> list[Path]:
    """검사 대상 파일. 워크트리 사본은 본 저장소 파일의 복사본이라 제외한다.

    **`.py`도 본다**(2026-08-03 확장). 처음엔 `.md`만 봤는데 **소스 주석의 포인터를
    통째로 놓쳤다** — `learning.py`와 `base.py`의 주석이 각각 두 버전 전, 세 버전 전
    인수인계 문서를 가리키고 있었다. 코드 주석은 "왜 이렇게 했나"를 설명하며 인수인계
    문서를 가리키는 경우가 많고, 그 링크가 끊기면 근거를 찾을 수 없다. 함수 이름은
    이력 때문에 `_markdown_files`로 유지한다.
    """
    files: list[Path] = []
    for base in (_REPO_ROOT / "docs", _REPO_ROOT / "harness-mvp"):
        for pattern in ("*.md", "*.py"):
            files.extend(
                p
                for p in base.rglob(pattern)
                if ".claude" not in p.parts
                and "_workspace" not in p.parts
                and "__pycache__" not in p.parts
            )
    files.append(_REPO_ROOT / "CLAUDE.md")
    return [p for p in files if p.is_file()]


def _latest_handoff() -> Path | None:
    summaries = list(_PROGRESS_DIR.glob("harness-handoff-summary-v*-ko.md"))
    if not summaries:
        return None
    return max(summaries, key=lambda p: int(re.search(r"-v(\d+)-", p.name).group(1)))


class DocsAreReachableTest(unittest.TestCase):
    """공개 미러에는 `docs/03_진행상황`이 없다(화이트리스트 제외) — 그때는 건너뛴다."""

    def setUp(self) -> None:
        if not _PROGRESS_DIR.is_dir():
            self.skipTest("공개 미러에는 docs/03_진행상황이 없다")


class HandoffPointerTest(DocsAreReachableTest):
    """`vN §M` 포인터가 **현재 최신 인수인계 문서**를 가리키는가.

    옛 버전은 최신에 흡수되고 삭제되는 게 규칙이라(작업 규칙 "문서"), 이전 버전 번호가
    남은 포인터는 그 자체로 끊어진 링크다.

    **버전만 언급하고 `§`가 안 붙은 이력 서술은 대상이 아니다**("당시 v18 →" 같은 것) —
    그건 "그때 그 버전이 있었다"는 사실 기록이고 포인터가 아니다.

    **이 파일에 옛 버전 포인터를 리터럴로 쓸 수 없다** — `.py`까지 검사하게 된 순간
    이 docstring의 예시가 스스로 걸렸다. 예시가 필요하면 버전 숫자를 빼고 서술할 것.
    """

    _POINTER = re.compile(r"\bv(\d+)\s*§\s*(\d+)")

    def test_pointers_target_the_latest_handoff_version(self) -> None:
        latest = _latest_handoff()
        self.assertIsNotNone(latest, "인수인계 문서를 못 찾았다")
        latest_version = int(re.search(r"-v(\d+)-", latest.name).group(1))

        stale: list[str] = []
        for path in _markdown_files():
            for version, section in self._POINTER.findall(path.read_text(encoding="utf-8")):
                if int(version) != latest_version:
                    stale.append(f"{path.relative_to(_REPO_ROOT)}: v{version} §{section}")

        self.assertEqual(
            stale,
            [],
            f"최신 인수인계 문서는 v{latest_version}인데 옛 버전을 가리키는 포인터가 있다"
            f"(절 번호는 버전 간 보존되므로 버전 숫자만 갈아주면 된다): {stale}",
        )

    def test_pointed_sections_exist_in_the_handoff(self) -> None:
        latest = _latest_handoff()
        text = latest.read_text(encoding="utf-8")
        existing = {int(m) for m in re.findall(r"(?m)^##\s+(\d+)\.", text)}
        self.assertTrue(existing, f"{latest.name}에서 `## N.` 절 제목을 못 찾았다")

        missing: list[str] = []
        for path in _markdown_files():
            for _version, section in self._POINTER.findall(path.read_text(encoding="utf-8")):
                if int(section) not in existing:
                    missing.append(f"{path.relative_to(_REPO_ROOT)}: §{section}")

        self.assertEqual(
            missing, [], f"{latest.name}에 없는 절을 가리킨다(있는 절: {sorted(existing)}): {missing}"
        )


class AdrReferenceTest(unittest.TestCase):
    """언급된 ADR 번호가 실제로 파일로 존재하는가.

    `ADR 0001~0011`(범위), `ADR 0003/0005`(열거), `ADR 0009`(단일)를 모두 다룬다.
    범위는 양 끝만 보지 않고 **사이 번호까지 전부** 확인한다 — 중간이 비면 그것도
    실제 결함이다(번호를 건너뛰고 만들었거나 파일이 사라졌거나).

    **날짜를 ADR 번호로 오인하지 않는다**: `ADR 0006, 2026-07-27` 같은 서술이 실제로
    있어서 첫 구현이 `2026`을 없는 ADR로 신고했다. 연도를 목록으로 막는 대신
    **뒤에 `-숫자`가 오면 날짜로 보고 건너뛴다**(구조적 신호 — 문구 매칭이 아니다).
    """

    _ADR_MENTION = re.compile(r"ADR\s+((?:\d{4}[\s/,~-]*)+)")

    def _existing(self) -> set[int]:
        return {int(p.name[:4]) for p in _ADR_DIR.glob("[0-9][0-9][0-9][0-9]-*.md")}

    def _mentioned_numbers(self, text: str) -> list[int]:
        found: list[int] = []
        for mention in self._ADR_MENTION.finditer(text):
            group, base = mention.group(1), mention.start(1)
            numbers: list[int] = []
            for number in re.finditer(r"\d{4}", group):
                after = base + number.end()
                if re.match(r"-\d", text[after : after + 2]):
                    continue  # `2026-07-27` 같은 날짜
                numbers.append(int(number.group()))
            if "~" in group and len(numbers) >= 2:
                numbers = list(range(min(numbers), max(numbers) + 1))
            found.extend(numbers)
        return found

    def test_every_mentioned_adr_number_has_a_file(self) -> None:
        self.assertTrue(_ADR_DIR.is_dir(), f"ADR 디렉터리가 없다: {_ADR_DIR}")
        existing = self._existing()
        self.assertTrue(existing, "ADR 파일을 하나도 못 찾았다 — 파일명 규칙이 바뀌었는지 확인")

        missing: list[str] = []
        for path in _markdown_files():
            for number in self._mentioned_numbers(path.read_text(encoding="utf-8")):
                if number not in existing:
                    missing.append(f"{path.relative_to(_REPO_ROOT)}: ADR {number:04d}")

        self.assertEqual(
            sorted(set(missing)),
            [],
            f"문서가 없는 ADR을 가리킨다(실제 존재: {sorted(existing)}). "
            f"ADR을 추가했으면 목록 서술(`ADR 0001~NNNN`)도 같이 늘릴 것: {sorted(set(missing))}",
        )


class ConfigValueInDocsTest(unittest.TestCase):
    """문서에 JSON 형태로 적힌 설정값이 `config.json` 실제 값과 같은가.

    2026-08-03에 실제로 틀렸던 자리다 — README의 config 예시 블록이
    `max_subscription_candidates: 1`, `judge_model: "gemini"`로 남아 있었는데 실제는
    2와 `"codex"`였다. 예시 블록은 새 세션이 **그대로 복사해 쓰는 것**이라 틀리면
    바로 잘못된 설정이 된다.

    **JSON 형태(`"key": value`)만 본다.** 산문의 "(기본 2)" 같은 표기는 문맥에 따라
    "예전 기본값"을 설명하는 경우가 있어서 기계가 판정할 수 없다 — 오탐을 만들지 않는
    게 우선이다.
    """

    # 값이 틀리면 실제 동작/비용이 달라지는 것들만 본다. 문서에 안 나오면 검사 대상이 아니다.
    _WATCHED = (
        "candidate_models",
        "judge_model",
        "delegation_model",
        "max_subscription_candidates",
        "max_refinement_rounds",
        "max_agent_turns",
        "budget_usd",
        "budget_subscription_calls",
    )

    def setUp(self) -> None:
        config_path = _REPO_ROOT / "harness-mvp" / "config.json"
        self.assertTrue(config_path.is_file(), f"config.json이 없다: {config_path}")
        self.config = json.loads(config_path.read_text(encoding="utf-8"))

    def _documents(self) -> list[Path]:
        docs = [_REPO_ROOT / "harness-mvp" / "README.md"]
        latest = _latest_handoff() if _PROGRESS_DIR.is_dir() else None
        if latest:
            docs.append(latest)
        return [p for p in docs if p.is_file()]

    def test_documented_json_values_match_config(self) -> None:
        mismatches: list[str] = []
        compared = 0
        for path in self._documents():
            text = path.read_text(encoding="utf-8")
            for key in self._WATCHED:
                actual = self.config.get(key)
                for raw in re.findall(rf'"{key}"\s*:\s*(\[[^\]]*\]|"[^"]*"|[^,\n}}]+)', text):
                    documented = self._parse(raw)
                    if documented is _UNPARSED:
                        continue
                    compared += 1
                    if documented != actual:
                        mismatches.append(
                            f"{path.relative_to(_REPO_ROOT)}: {key} = {documented!r} "
                            f"(실제 {actual!r})"
                        )

        # **공허한 통과를 막는다**: 문서 형식이 바뀌어 아무것도 못 찾으면 이 테스트는
        # 조용히 무의미해진다. 오늘 "통과하는데 아무것도 검증하지 않는 테스트"를 한 번
        # 만들었어서 같은 함정을 여기서 막아둔다.
        self.assertGreater(
            compared,
            0,
            "문서에서 설정값을 하나도 못 찾았다 — README의 config 예시 블록 형식이 "
            f"바뀌었는지 확인할 것(감시 대상 키: {list(self._WATCHED)})",
        )
        self.assertEqual(
            sorted(set(mismatches)),
            [],
            "문서의 설정 예시가 config.json과 다르다. 새 세션이 예시를 그대로 복사해 쓰므로 "
            f"틀리면 잘못된 설정이 된다: {sorted(set(mismatches))}",
        )

    @staticmethod
    def _parse(raw: str):
        """문서에서 뽑은 값 조각을 JSON으로 읽는다. 못 읽으면 검사에서 뺀다.

        문서에는 `"budget_usd": 0.05,  // 주석` 같은 변형이 있을 수 있어서, 엄격히
        파싱되는 것만 비교한다 — 파싱 실패를 실패로 처리하면 오탐이 된다.
        """
        try:
            return json.loads(raw.strip().rstrip(","))
        except json.JSONDecodeError:
            return _UNPARSED


class _Unparsed:
    def __repr__(self) -> str:  # pragma: no cover - 진단 출력용
        return "<파싱 불가>"


_UNPARSED = _Unparsed()


if __name__ == "__main__":
    unittest.main()
