"""harness/*.py 모듈 간 의존 방향이 항상 지금의 계층을 지키는지 검증한다
(2026-07-24, "아키텍처 불변량 강제" 사용자 요청).

실제 import 그래프를 조사해보니 이미 역방향 의존이 하나도 없는 깨끗한 구조였다:

    cli.py
      -> orchestrator.py
           -> judge / subagent_runner / synthesizer / safety / planner / model_runner
                -> router
                     -> run_store / config
                          -> schemas (아무 내부 모듈도 import 안 함)
    (dashboard / failure_analysis / live_status는 orchestrator와 무관하게
     run_store/schemas에만 의존하는 독립된 "조회/집계" 모듈군)

"강제"의 목적은 뭔가를 새로 바로잡는 게 아니라 **지금의 깨끗한 상태를 앞으로도
유지**하는 것이다 — 새 의존성(import-linter 등) 추가 없이 stdlib `ast`만으로
검증해서, 누군가(사람이든 에이전트든) 실수로 역방향 import를 추가하면 이 테스트가
`pytest tests/`만으로 바로 잡아낸다. 이 프로젝트엔 CI가 없어서 "린터"보다
"테스트"로 구현하는 게 기존 관행(phase/step 끝날 때 전체 테스트 실행)과 바로
맞물린다.

새 모듈을 추가하거나 새로운 의존을 만들 때는 `_ALLOWED_INTERNAL_IMPORTS`도 같이
갱신해야 한다 — 이 목록이 허용된 계층 구조의 유일한 출처다.
"""
from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

_HARNESS_PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "harness"
# 크기 감시는 harness/만이 아니라 src/ 전체를 본다 — providers/evals/fetchers도 같은
# 방식으로 자랄 수 있고, 계층 허용표와 달리 크기는 패키지를 가릴 이유가 없다.
_SRC_DIR = Path(__file__).resolve().parents[1] / "src"

_ALLOWED_INTERNAL_IMPORTS: dict[str, set[str]] = {
    "__init__": set(),
    "schemas": set(),
    "config": set(),
    "run_store": set(),
    # budget은 schemas만 본다 — 예산은 "얼마 썼나"만 아는 값 객체이고, 어디서
    # 어떻게 호출하는지는 모른다(호출부가 넘겨받아 쓴다).
    "budget": {"schemas"},
    # learning은 run 산출물을 읽고 쓰기만 한다 — 어떤 패턴이 돌았는지, 누가
    # 호출했는지는 모른다(orchestrator가 끝난 run을 넘겨준다).
    "learning": {"run_store", "schemas"},
    # finalization은 run 종료 경로(Safety -> metrics -> final.md). 어떤 패턴이 돌았는지는
    # 모르고, orchestrator가 결과를 넘겨준다 — 그래서 orchestrator를 import하지 않는다.
    "finalization": {"learning", "run_store", "safety", "schemas"},
    "router": {"schemas"},
    "safety": {"schemas"},
    "synthesizer": {"schemas"},
    "model_runner": {"budget", "run_store", "schemas"},
    "planner": {"router", "schemas"},
    "judge": {"budget", "model_runner", "schemas"},
    "subagent_runner": {"budget", "run_store", "model_runner", "schemas"},
    # delegation은 매니저-워커 위임(ADR 0014). subagent_runner(역할 체인)와 같은 자리라
    # 의존도 같다 — 어떤 패턴이 자기를 불렀는지는 모르고 orchestrator가 순서를 준다.
    "delegation": {"budget", "run_store", "model_runner", "schemas"},
    "agent_runner": {"run_store", "schemas"},
    "orchestrator": {
        "agent_runner",
        "budget",
        "delegation",
        "finalization",
        "learning",
        "judge",
        "live_status",
        "model_runner",
        "planner",
        "router",
        "run_store",
        "safety",
        "subagent_runner",
        "synthesizer",
        "schemas",
    },
    "dashboard": {"run_store", "schemas"},
    "failure_analysis": {"run_store", "schemas"},
    "live_status": {"run_store"},
    "cli": {
        "dashboard",
        "failure_analysis",
        "learning",  # `learn` 서브커맨드가 집계를 읽어 사람에게 보여준다
        "live_status",
        # config의 max_parallel_candidates를 반영하는 자리(2026-08-13). 다른 설정값은
        # 소비자가 orchestrator라 그쪽에 넣지만, 후보 병렬 실행을 실제로 쓰는 건
        # model_runner다 — 값을 orchestrator에 두고 다시 넘기면 같은 값이 전역 두 곳에
        # 생긴다. cli는 최상위 계층이라 아래를 직접 설정해도 방향이 뒤집히지 않는다.
        "model_runner",
        "orchestrator",
        "config",
        "schemas",
    },
}


def extract_internal_imports(source: str) -> set[str]:
    """`from . import x, y` / `from .x import ...` 형태의 상대 import에서 이
    패키지 내부 모듈 이름만 뽑는다(순수 함수 — 실제 파일 없이 하드코딩된 소스
    문자열로도 테스트 가능). `from ..` 이상(level != 1)은 이 패키지 밖을 향하는
    import라 대상이 아니고, `import json`/`from providers.base import ...` 같은
    절대 import도 `ast.ImportFrom.level`이 0이라 자연히 제외된다."""
    tree = ast.parse(source)
    imports: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom) or node.level != 1:
            continue
        if node.module:
            imports.add(node.module.split(".")[0])  # from .config import X -> "config"
        else:
            imports.update(alias.name for alias in node.names)  # from . import a, b
    return imports


class ArchitectureLayerTest(unittest.TestCase):
    def test_allowed_imports_map_covers_every_module_in_package(self) -> None:
        actual_modules = {p.stem for p in _HARNESS_PACKAGE_DIR.glob("*.py")}
        self.assertEqual(
            actual_modules,
            set(_ALLOWED_INTERNAL_IMPORTS),
            "harness/ 밑에 새 모듈이 생기거나 지워졌는데 _ALLOWED_INTERNAL_IMPORTS가 "
            "안 갱신됐다 — 새 모듈을 추가할 때마다 이 목록도 같이 갱신해야 한다.",
        )

    def test_every_module_only_imports_whats_allowed(self) -> None:
        violations = []
        for module_name, allowed in _ALLOWED_INTERNAL_IMPORTS.items():
            source = (_HARNESS_PACKAGE_DIR / f"{module_name}.py").read_text(encoding="utf-8")
            extra = extract_internal_imports(source) - allowed
            if extra:
                violations.append(f"{module_name}.py가 허용 안 된 모듈을 import함: {sorted(extra)}")
        self.assertEqual(violations, [], "\n".join(violations))


class ModuleSizeGuardTest(unittest.TestCase):
    """모듈이 조용히 god 모듈로 자라는 것을 잡는다 (2026-07-29 추가).

    **왜 필요했나**: 계층 테스트는 "누가 누구를 import하나"만 보고 **"한 모듈이 얼마나
    많은 일을 하나"는 안 본다.** 그 갭 때문에 `orchestrator.py`가 3주에
    367 → 987줄로(2.7배) 자란 걸 아무 테스트도 알려주지 않았다. 실측으로 확인한
    증가 패턴은 선형이 아니라 **패턴/기능이 늘 때마다 계단식**이다(패턴 핸들러 하나가
    100~130줄).

    "다음에 크면 나눠야지"를 사람 기억에 맡기면 이미 늦는다 — 넘는 순간 실패하게 해서
    그 시점을 놓치지 않게 한다. 이 검사는 5ms 정도로 전체 스위트의 0.1% 미만이다.

    상한을 넘으면 할 일: **패턴 핸들러/공용 경로를 별 모듈로 분리**한다. 상한을
    올리는 건 답이 아니다(그러면 이 테스트가 하는 일이 없어진다). 다만 폐기 검토 중인
    코드는 분리 대상에서 빼는 게 규칙이다(`docs/00_작업규칙` 참고).
    """

    # 1,200줄: 2026-07-29 현재 최대가 orchestrator.py 987줄이라 여유를 두되,
    # 다음 팀 패턴(핸들러 100~130줄)이 추가되면 걸리도록 잡았다.
    MAX_LINES = 1_200

    def test_no_module_exceeds_the_size_limit(self) -> None:
        oversized = [
            f"{path.relative_to(_SRC_DIR)} {len(path.read_text(encoding='utf-8').splitlines())}줄"
            for path in sorted(_SRC_DIR.rglob("*.py"))
            if len(path.read_text(encoding="utf-8").splitlines()) > self.MAX_LINES
        ]
        self.assertEqual(
            oversized,
            [],
            f"모듈이 {self.MAX_LINES}줄을 넘었다: {oversized}. "
            f"상한을 올리지 말고 분리할 것 — 상한을 올리면 이 테스트가 하는 일이 없어진다.",
        )

    def test_limit_is_not_already_met_by_accident(self) -> None:
        """상한이 현실과 동떨어지지 않았는지 — 너무 높으면 경고가 영원히 안 온다.

        가장 큰 모듈이 상한의 절반도 안 되면 상한이 사실상 없는 것이므로, 그때는
        상한을 내려 감시가 계속 유효하게 유지한다.
        """
        largest = max(len(p.read_text(encoding="utf-8").splitlines()) for p in _SRC_DIR.rglob("*.py"))

        self.assertGreater(
            largest,
            self.MAX_LINES // 2,
            f"가장 큰 모듈이 {largest}줄로 상한({self.MAX_LINES})의 절반 미만이다 — "
            f"상한을 낮춰서 감시가 계속 유효하게 할 것.",
        )


class DocumentedTestCountTest(unittest.TestCase):
    """문서에 적힌 테스트 개수가 실제와 맞는지 (2026-07-29 추가).

    **왜 필요했나**: 개수를 네 문서에서 손으로 맞춰왔고, 2026-07-29에 **네 번 연속
    조용히 어긋났다.** 원인은 문자열 치환이 잘못된 전제(`# 388개`)로 시작해 실제 값
    (`386개`)과 안 맞아 no-op이 됐고, 실패를 알려주는 게 없었기 때문이다. 그래서
    `README.md`와 인수인계 문서가 22개나 뒤처진 채로 커밋됐다.

    "다음에 잘 맞추자"는 사람 기억에 맡기는 방식이고 이미 실패했다 — 어긋나면
    테스트가 실패하게 한다. 진행상황 문서에 오래 방치된 "테스트 개수 자기검증" 항목이
    바로 이것이다.

    **AST로 센다**(pytest 실행이 아니라): 테스트 안에서 pytest를 다시 돌리면 재귀가
    되고, 수집만 해도 느리다. `def test_*` 개수가 pytest 보고 수와 정확히 일치하는 걸
    확인했다(2026-07-29: 둘 다 408) — parametrize를 쓰지 않는 코드베이스라 성립한다.
    subTest는 개수를 늘리지 않으므로(같은 테스트 함수 안에서 도는 것) 영향 없다.
    """

    def actual_count(self) -> int:
        tests_dir = Path(__file__).resolve().parent
        return sum(
            1
            for path in tests_dir.glob("test_*.py")
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
        )

    def documented_counts(self, text: str) -> list[int]:
        """"테스트 N개" / "# N개" 형태로 적힌 숫자를 뽑는다."""
        return [int(m) for m in re.findall(r"테스트[ ]*\*{0,2}(\d{3})개", text)] + [
            int(m) for m in re.findall(r"pytest tests/ -v\s*#\s*(\d{3})개", text)
        ]

    def test_readme_count_matches_reality(self) -> None:
        readme = Path(__file__).resolve().parents[1] / "README.md"
        documented = self.documented_counts(readme.read_text(encoding="utf-8"))

        self.assertTrue(documented, "README.md에서 테스트 개수를 못 찾았다 — 형식이 바뀌었는지 확인")
        for count in documented:
            with self.subTest(documented=count):
                self.assertEqual(
                    count,
                    self.actual_count(),
                    f"README.md의 테스트 개수가 실제({self.actual_count()})와 다르다. "
                    f"테스트를 추가/삭제했으면 문서도 같이 갱신할 것.",
                )

    def test_handoff_summary_count_matches_reality(self) -> None:
        """인수인계 문서(가장 최신 vN)도 같이 본다 — 새 세션이 처음 읽는 숫자다."""
        progress_dir = Path(__file__).resolve().parents[2] / "docs" / "03_진행상황"
        summaries = sorted(progress_dir.glob("harness-handoff-summary-v*-ko.md"))
        if not summaries:
            self.skipTest("공개 미러에는 docs/03_진행상황이 없다(화이트리스트 제외)")

        latest = max(summaries, key=lambda p: int(re.search(r"-v(\d+)-", p.name).group(1)))
        documented = self.documented_counts(latest.read_text(encoding="utf-8"))

        self.assertTrue(documented, f"{latest.name}에서 테스트 개수를 못 찾았다")
        for count in documented:
            with self.subTest(doc=latest.name, documented=count):
                self.assertEqual(count, self.actual_count())


class ExtractInternalImportsTest(unittest.TestCase):
    def test_from_dot_import_multiple(self) -> None:
        source = "from . import judge, model_runner\n"
        self.assertEqual(extract_internal_imports(source), {"judge", "model_runner"})

    def test_from_dot_module_import(self) -> None:
        source = "from .config import HarnessConfig, load_config\n"
        self.assertEqual(extract_internal_imports(source), {"config"})

    def test_ignores_absolute_and_stdlib_imports(self) -> None:
        source = "import json\nfrom pathlib import Path\nfrom providers.base import Provider\n"
        self.assertEqual(extract_internal_imports(source), set())

    def test_ignores_deeper_relative_imports(self) -> None:
        self.assertEqual(extract_internal_imports("from .. import something\n"), set())


if __name__ == "__main__":
    unittest.main()
