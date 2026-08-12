"""도메인 폴더 스캐폴딩 자동화 (2026-07-16, "손으로 두 번 반복한 건 스크립트로"
원칙 — ncp-snapshot-drill/centos-eol-migration 두 도메인을 만들며 매번 손으로
반복한 절차를 스크립트화함).

**적용 범위**: Fetcher/커스텀 실행 스크립트 없이 `config.json` +
`examples/task.*.json`만으로 harness-mvp CLI를 그대로 쓰는 "가벼운" 도메인 전용
(ncp-snapshot-drill, centos-eol-migration이 이 패턴). 팀 패턴 4종을 모두
지원한다(`--pattern`). cloud-ops처럼 Fetcher/xlsx 파이프라인이 필요한 도메인은
매번 요구사항이 달라 자동화 대상이 아니다.

**하는 일**:
1. `domains/<name>/config.json` 생성 — 실제 e2e로 검증된 역할별 모델 매핑
   (research=gemini, design_review=claude, implementation_review=codex)을 그대로
   쓰고, 선택한 패턴에서 실제로 의미 있는 필드가 무엇인지(+ 그 패턴의 비용 상한
   knob) `_설명`에 명시한다
2. `domains/<name>/examples/task.<task-id>.json` 생성 — 주어진 prompt로 TaskInput
   작성. **`iterative_refinement`/`agentic_task`는 키워드 자동 라우팅이 없는
   opt-in 전용 패턴이라 `constraints`에 `"team_pattern:<이름>"`을 자동으로 넣는다**
   (이게 없으면 프롬프트와 무관하게 fan_out_judge로 폴백된다)
3. **로컬 검증(무료, LLM 미호출)**: `planner.create_plan()`으로 team_pattern이
   기대한 대로(`--pattern`) 분류되는지 확인 — 키워드 라우팅 패턴은 프롬프트에
   라우팅 키워드("조사"/"리서치"/"설계 리뷰" 등, `router.py`의 `_TASK_TYPE_RULES`
   참고)가 없으면 fan_out_judge로 폴백되므로 이 자동 검증이 실수를 바로 잡아준다
4. 남은 수동 작업(README.md 코드 구조 표 행 추가, 진행상황 문서 갱신)은 이
   스크립트가 자동으로 하지 않는다 — 도메인의 실제 배경/의사결정 서술은 사람이
   써야 의미가 있다. 대신 체크리스트를 출력해서 잊지 않게 한다.

사용법 (harness-mvp 디렉토리에서):
  PYTHONPATH=src python scripts/new_domain.py ncp-example-domain \
      --task-id ncp-example-task \
      --prompt "NCP OO에 대해 조사해줘. 그 다음 절차서를 설계하고 검토해줘." \
      --pattern hierarchical_delegation
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Optional

_HARNESS_MVP_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT = _HARNESS_MVP_ROOT.parent
_DEFAULT_DOMAINS_ROOT = _REPO_ROOT / "domains"

sys.path.insert(0, str(_HARNESS_MVP_ROOT / "src"))

from harness import planner  # noqa: E402
from harness.cli import _default_providers  # noqa: E402
from harness.config import load_config  # noqa: E402
from harness.schemas import TaskInput  # noqa: E402

SUPPORTED_PATTERNS = (
    "hierarchical_delegation",
    "fan_out_judge",
    "iterative_refinement",
    "agentic_task",
)
# **2026-08-10 수정**: 기본값이 `hierarchical_delegation`이었다 — 아래 주석이 "품질 차이가
# 없는 경로를 기본값으로 둘 수 없다"고 적어둔 그 패턴이다. ADR 0009 강등이 이 상수에
# 반영되지 않아, 새 도메인이 **강등된 패턴을 opt-in 제약으로 강제로 물고** 태어났다
# (비용 1.5~3.2배, 우위 미입증). `fan_out_judge`는 유일한 자동 진입 패턴이므로(ADR 0010)
# 기본값일 때 제약을 넣지 않는 게 맞고, 그래야 planner의 키워드 라우팅이 살아난다.
DEFAULT_PATTERN = "fan_out_judge"

# 키워드 자동 라우팅이 없어 `constraints`의 "team_pattern:<이름>" opt-in으로만
# 진입하는 패턴. 고비용이거나 되돌리기 어려운 부수 효과가 있어 실수로 걸리면 안 되기
# 때문에 planner가 일부러 자동 분류에서 빼놨다 — 그래서 이 스크립트가 task json에
# 제약을 직접 넣어줘야 한다.
#
# - iterative_refinement/agentic_task: 도입 시점부터 opt-in (ADR 0006/0007)
# - hierarchical_delegation: **2026-07-29 강등**(ADR 0009). 네 번 측정해서 direct_call
#   대비 우위를 입증하지 못했고, 결함 없는 4차 측정에서 세 조건이 전부 만점인데 체인이
#   1.5배(3역할)~3.2배(5역할) 비쌌다. 품질 차이가 없는 경로를 기본값으로 둘 수 없다.
_OPT_IN_PATTERNS = frozenset(
    {"iterative_refinement", "agentic_task", "hierarchical_delegation"}
)

# ncp-snapshot-drill/centos-eol-migration에서 실제 e2e로 검증한 조합을 그대로
# 기본값으로 쓴다(harness-mvp/config.json의 delegation_role_models와 동일).
_DEFAULT_CONFIG: dict[str, Any] = {
    "_설명": {
        "_주의": "",  # 패턴별로 render_config_json()이 채운다
        "candidate_models": "fan_out_judge 후보 모델 (iterative_refinement에서는 첫 번째가 generator)",
        "judge_model": "fan_out_judge의 judge 겸 iterative_refinement의 evaluator",
        "delegation_model": "delegation_role_models에 없는 역할에 적용되는 기본 모델",
        "delegation_role_models": (
            "hierarchical_delegation 역할별 모델. harness-mvp/config.json에서 실제 e2e로 "
            "검증한 조합(research=조사에 강한 gemini, design_review=설계 검토에 강한 "
            "claude, implementation_review=codex)을 그대로 가져옴"
        ),
        "max_subscription_candidates": "구독 CLI(claude/codex) 동시 사용 후보 수 제한",
    },
    "candidate_models": ["claude", "codex", "gemini"],
    "judge_model": "gemini",
    "delegation_model": "claude",
    "delegation_role_models": {
        "research": "gemini",
        "design_review": "claude",
        "implementation_review": "codex",
    },
    "max_subscription_candidates": 1,
}

# 패턴마다 실제로 쓰이는 필드가 달라서, 생성된 config.json만 봐도 "이 도메인에서
# 뭘 만지면 되는지" 알 수 있게 _주의 문구를 다르게 써준다. 어느 패턴이든
# _default_providers가 전체 provider를 등록하므로 미사용 필드도 유효한 값이어야 한다.
_PATTERN_CONFIG_NOTES: dict[str, str] = {
    "hierarchical_delegation": (
        "이 도메인은 hierarchical_delegation을 쓴다 — delegation_model/"
        "delegation_role_models만 실제로 쓰이고 candidate_models/judge_model은 미사용이다."
    ),
    "fan_out_judge": (
        "이 도메인은 fan_out_judge를 쓴다 — candidate_models(후보 생성)와 "
        "judge_model(심사)이 핵심이고 delegation_* 는 미사용이다. 후보가 최소 2개 "
        "성공해야 심사로 넘어간다(MIN_CANDIDATES). judge는 후보 생성 모델과 분리해 "
        "두는 게 안전하다(ADR 0004)."
    ),
    "iterative_refinement": (
        "이 도메인은 iterative_refinement를 쓴다(ADR 0006) — candidate_models의 첫 "
        "번째가 generator, judge_model이 evaluator다. 라운드마다 두 모델을 각각 1회씩 "
        "호출하므로 max_refinement_rounds가 곧 비용 상한이다. generator를 구독 "
        "CLI(claude/codex)로 두면 라운드마다 구독 한도를 소모하니 종량제(gemini) 권장."
    ),
    "agentic_task": (
        "이 도메인은 agentic_task를 쓴다(ADR 0007) — 에이전트 provider는 claude로 "
        "고정이라 여기 모델 설정의 영향을 받지 않는다. max_agent_turns가 비용/생성 "
        "규모 상한이다. 에이전트가 실제 파일을 만드는 패턴이라 매 run이 사람 승인을 "
        "거친다(planner가 risk_level=high 강제)."
    ),
}

# 패턴별로 추가할 비용 상한 knob (해당 패턴을 쓸 때만 넣는다 — 안 쓰는 knob까지
# 전부 넣으면 "이 중에 뭘 만져야 하지"가 흐려진다).
_PATTERN_EXTRA_CONFIG: dict[str, dict[str, Any]] = {
    "iterative_refinement": {
        "max_refinement_rounds": (3, "rubric 통과까지 반복할 최대 라운드 수(라운드당 LLM 2회 호출)")
    },
    "agentic_task": {
        "max_agent_turns": (8, "에이전트가 도구를 호출하며 진행할 최대 턴 수")
    },
}


class DomainAlreadyExistsError(Exception):
    """대상 도메인 폴더가 이미 있을 때 — 실수로 덮어쓰지 않기 위해 막는다."""


def render_config_json(pattern: str = DEFAULT_PATTERN) -> dict[str, Any]:
    """선택한 패턴에서 실제로 의미 있는 필드가 뭔지 설명이 붙은 config를 만든다."""
    config = copy.deepcopy(_DEFAULT_CONFIG)
    config["_설명"]["_주의"] = (
        "harness-mvp/src/harness/config.py의 HarnessConfig 필드를 그대로 따른다. "
        + _PATTERN_CONFIG_NOTES[pattern]
    )
    for key, (value, description) in _PATTERN_EXTRA_CONFIG.get(pattern, {}).items():
        config["_설명"][key] = description
        config[key] = value
    return config


def render_task_json(task_id: str, prompt: str, pattern: str = DEFAULT_PATTERN) -> dict[str, Any]:
    """opt-in 전용 패턴이면 `constraints`에 team_pattern override를 넣어준다.

    이걸 빠뜨리면 프롬프트 내용과 무관하게 fan_out_judge로 폴백된다 — 이 패턴들은
    planner가 일부러 키워드 자동 라우팅에서 빼놨기 때문이다(`_OPT_IN_PATTERNS` 참고).
    """
    constraints = [f"team_pattern:{pattern}"] if pattern in _OPT_IN_PATTERNS else []
    return {"task_id": task_id, "prompt": prompt, "constraints": constraints}


def create_domain(
    name: str,
    *,
    task_id: str,
    prompt: str,
    pattern: str = DEFAULT_PATTERN,
    domains_root: Path = _DEFAULT_DOMAINS_ROOT,
) -> Path:
    """domains/<name>/config.json + examples/task.<task_id>.json을 만든다.

    이미 존재하는 도메인 폴더는 덮어쓰지 않고 예외를 던진다.
    """
    domain_dir = domains_root / name
    if domain_dir.exists():
        raise DomainAlreadyExistsError(f"이미 존재하는 도메인 폴더: {domain_dir}")

    examples_dir = domain_dir / "examples"
    examples_dir.mkdir(parents=True)

    config_path = domain_dir / "config.json"
    config_path.write_text(
        json.dumps(render_config_json(pattern), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    task_path = examples_dir / f"task.{task_id}.json"
    task_path.write_text(
        json.dumps(render_task_json(task_id, prompt, pattern), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    return domain_dir


def verify_domain(domain_dir: Path, *, task_id: str, expected_pattern: str) -> dict[str, Any]:
    """방금 만든 도메인을 로컬에서(LLM 미호출) 검증한다.

    - task 프롬프트가 실제로 expected_pattern으로 분류되는지(router 키워드 매칭)
    - 도메인 config.json이 정확히 로드되는지
    - provider 레지스트리가 에러 없이 구성되는지
    """
    task_path = domain_dir / "examples" / f"task.{task_id}.json"
    task = TaskInput.model_validate(json.loads(task_path.read_text(encoding="utf-8")))
    plan = planner.create_plan(task)

    # load_config()는 CWD 기준 상대경로("config.json")를 읽으므로, 스크립트가 어느
    # 디렉터리에서 실행되든 동작하도록 domain_dir로 직접 지정해서 읽는다.
    config = load_config(domain_dir / "config.json")
    providers = _default_providers(config.candidate_models, config)

    return {
        "task_type": plan.task_type,
        "team_pattern": plan.team_pattern,
        "delegation_chain": [(step.role, step.provider_id) for step in plan.delegation_chain],
        "pattern_matches_expected": plan.team_pattern == expected_pattern,
        "provider_keys": sorted(providers.keys()),
    }


def render_pattern_notice(pattern: str) -> list[str]:
    """실행 전에 알아야 실수를 막을 수 있는 패턴별 주의사항.

    config.json의 `_설명`은 나중에 파일을 열어봐야 보이지만, 이건 스캐폴딩
    직후 화면에 바로 뜬다 — 첫 run에서 당황할 만한 것(승인 대기로 멈춤, 구독
    한도 소모)을 미리 알려주는 게 목적이다.
    """
    if pattern == "agentic_task":
        return [
            "[안내] agentic_task는 run할 때마다 사람 승인이 필요합니다(risk_level=high 강제).",
            "       `cli.py run ...` → 승인 대기 → `cli.py approve <run_id>` 순서로 진행하세요.",
            "       에이전트는 artifacts/agent_workspace/ 안에만 실제 파일을 만듭니다(ADR 0007).",
        ]
    if pattern == "iterative_refinement":
        return [
            "[안내] iterative_refinement는 라운드마다 generator+evaluator를 각각 1회 호출합니다.",
            "       구독 한도를 아끼려면 `--models gemini`처럼 종량제 모델로 실행하세요(ADR 0006).",
        ]
    return []


def render_followup_checklist(name: str, task_id: str, *, repo_root: Path = _REPO_ROOT) -> str:
    """남은 수동 작업 체크리스트를 만든다. `docs/03_진행상황/` 항목은 그 폴더가
    실제로 있을 때만 넣는다 — 이 폴더는 도메인 실제 업무 내용과 진행 이력이 섞여
    있어 공개 구조 미러(`621dev/llm-harness`)에서는 아예 빠져 있으므로, 없는데도
    "갱신하라"고 안내하면 혼란만 준다(2026-07-24 실제로 공개 미러를 clone해 이
    스크립트를 돌려보다가 발견)."""
    lines = [
        "",
        "남은 수동 작업 (이 스크립트가 하지 않음):",
        f"  [ ] domains/{name}/examples/task.{task_id}.json의 prompt를 실제 요구사항으로 다듬기",
        "  [ ] harness-mvp/README.md 코드 구조 표에 새 도메인 행 추가",
    ]
    if (repo_root / "docs" / "03_진행상황" / "harness-progress-checklist-ko.md").exists():
        lines.append("  [ ] docs/03_진행상황/harness-progress-checklist-ko.md에 날짜 붙여 진행 상황 기록")
    lines.append("  [ ] (선택) references/ 폴더에 절차서 초안 작성")
    return "\n".join(lines) + "\n"


def main() -> int:
    # Windows 기본 콘솔 코드페이지(cp949 등)는 이 스크립트가 출력하는 일부 문자를
    # 인코딩하지 못해 UnicodeEncodeError로 죽는다(cli.py/setup_worktree.py/
    # sync_to_public.py와 동일한 문제 — 2026-07-24 공개 미러 clone에서 이 스크립트를
    # 실제로 실행하다 재현/확인). UTF-8을 강제해서 플랫폼 무관하게 만든다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name", help="도메인 폴더 이름 (예: ncp-example-domain)")
    parser.add_argument("--task-id", required=True, help="examples/task.<task-id>.json의 task_id")
    parser.add_argument("--prompt", required=True, help="TaskInput.prompt (라우팅 키워드 포함 권장)")
    parser.add_argument(
        "--pattern",
        default=DEFAULT_PATTERN,
        choices=list(SUPPORTED_PATTERNS),
        help=(
            f"쓸 team_pattern (기본: {DEFAULT_PATTERN}) — 검증 단계에서 실제 분류와 대조한다. "
            "iterative_refinement/agentic_task는 opt-in 전용이라 task json의 constraints에 "
            "자동으로 override가 들어간다"
        ),
    )
    parser.add_argument(
        "--domains-root",
        type=Path,
        default=_DEFAULT_DOMAINS_ROOT,
        help="도메인 폴더들의 상위 디렉터리 (기본: 저장소 루트의 domains/)",
    )
    args = parser.parse_args()

    try:
        domain_dir = create_domain(
            args.name,
            task_id=args.task_id,
            prompt=args.prompt,
            pattern=args.pattern,
            domains_root=args.domains_root,
        )
    except DomainAlreadyExistsError as exc:
        print(f"[fatal] {exc}")
        return 1

    print(f"[ok] 생성됨: {domain_dir}")

    result = verify_domain(domain_dir, task_id=args.task_id, expected_pattern=args.pattern)
    print(f"  task_type: {result['task_type']}")
    print(f"  team_pattern: {result['team_pattern']}")
    print(f"  delegation_chain: {result['delegation_chain']}")
    print(f"  provider 키: {result['provider_keys']}")

    if not result["pattern_matches_expected"]:
        if args.pattern in _OPT_IN_PATTERNS:
            # 제약을 넣었는데도 안 맞으면 프롬프트 문제가 아니라 엔진 쪽 문제다 —
            # 엉뚱한 곳(프롬프트 키워드)을 뒤지게 만들지 않도록 안내를 분리한다.
            print(
                f"[경고] constraints에 team_pattern:{args.pattern} 를 넣었는데도 "
                f"{result['team_pattern']!r}로 분류됐습니다. planner의 override 처리"
                "(_team_pattern_override)를 확인하세요."
            )
        else:
            print(
                f"[경고] 기대한 team_pattern={args.pattern!r}이 아니라 "
                f"{result['team_pattern']!r}로 분류됐습니다. --prompt에 라우팅 키워드"
                f'("조사"/"리서치"/"설계 리뷰"/"구현 리뷰" 등, router.py의 _TASK_TYPE_RULES 참고)'
                "가 있는지 확인하세요."
            )

    for note in render_pattern_notice(args.pattern):
        print(note)

    print(render_followup_checklist(args.name, args.task_id, repo_root=args.domains_root.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
