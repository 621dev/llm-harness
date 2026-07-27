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
import unittest
from pathlib import Path

_HARNESS_PACKAGE_DIR = Path(__file__).resolve().parents[1] / "src" / "harness"

_ALLOWED_INTERNAL_IMPORTS: dict[str, set[str]] = {
    "__init__": set(),
    "schemas": set(),
    "config": set(),
    "run_store": set(),
    "router": {"schemas"},
    "safety": {"schemas"},
    "synthesizer": {"schemas"},
    "model_runner": {"run_store", "schemas"},
    "planner": {"router", "schemas"},
    "judge": {"model_runner", "schemas"},
    "subagent_runner": {"run_store", "model_runner", "schemas"},
    "agent_runner": {"run_store", "schemas"},
    "orchestrator": {
        "agent_runner",
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
    "cli": {"dashboard", "failure_analysis", "live_status", "orchestrator", "config", "schemas"},
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
