"""도메인 폴더 스캐폴딩 자동화 (2026-07-16, "손으로 두 번 반복한 건 스크립트로"
원칙 — ncp-snapshot-drill/centos-eol-migration 두 도메인을 만들며 매번 손으로
반복한 절차를 스크립트화함).

**적용 범위**: `hierarchical_delegation` 기반의 "가벼운" 도메인 전용이다
(Fetcher/커스텀 실행 스크립트 없이 `config.json` + `examples/task.*.json`만으로
harness-mvp CLI를 그대로 쓰는 패턴 — ncp-snapshot-drill, centos-eol-migration이
이 패턴). cloud-ops처럼 Fetcher/xlsx 파이프라인이 필요한 도메인은 매번 요구사항이
달라 자동화 대상이 아니다.

**하는 일**:
1. `domains/<name>/config.json` 생성 — 실제 e2e로 검증된 역할별 모델 매핑
   (research=gemini, design_review=claude, implementation_review=codex)을 그대로
   사용
2. `domains/<name>/examples/task.<task-id>.json` 생성 — 주어진 prompt로 TaskInput
   작성
3. **로컬 검증(무료, LLM 미호출)**: `planner.create_plan()`으로 team_pattern이
   기대한 대로(`--pattern`) 분류되는지 확인 — 프롬프트에 라우팅 키워드("조사"/
   "리서치"/"설계 리뷰" 등, `router.py`의 `_TASK_TYPE_RULES` 참고)가 없으면
   fan_out_judge로 폴백되므로 이 자동 검증이 실수를 바로 잡아준다
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

# ncp-snapshot-drill/centos-eol-migration에서 실제 e2e로 검증한 조합을 그대로
# 기본값으로 쓴다(harness-mvp/config.json의 delegation_role_models와 동일).
_DEFAULT_CONFIG: dict[str, Any] = {
    "_설명": {
        "_주의": (
            "harness-mvp/src/harness/config.py의 HarnessConfig 필드를 그대로 따른다. "
            "이 도메인은 hierarchical_delegation만 쓰므로 candidate_models/judge_model은 "
            "실질적으로 미사용(다만 _default_providers가 항상 전체 provider를 등록하므로 "
            "유효한 값이어야 함)."
        ),
        "candidate_models": "fan_out_judge 후보 모델(이 도메인은 fan_out_judge를 쓰지 않아 미사용)",
        "judge_model": "fan_out_judge judge 모델(이 도메인은 fan_out_judge를 쓰지 않아 미사용)",
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


class DomainAlreadyExistsError(Exception):
    """대상 도메인 폴더가 이미 있을 때 — 실수로 덮어쓰지 않기 위해 막는다."""


def render_config_json() -> dict[str, Any]:
    return copy.deepcopy(_DEFAULT_CONFIG)


def render_task_json(task_id: str, prompt: str) -> dict[str, Any]:
    return {"task_id": task_id, "prompt": prompt, "constraints": []}


def create_domain(
    name: str, *, task_id: str, prompt: str, domains_root: Path = _DEFAULT_DOMAINS_ROOT
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
        json.dumps(render_config_json(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    task_path = examples_dir / f"task.{task_id}.json"
    task_path.write_text(
        json.dumps(render_task_json(task_id, prompt), ensure_ascii=False, indent=2) + "\n",
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
        default="hierarchical_delegation",
        choices=["hierarchical_delegation", "fan_out_judge"],
        help="기대하는 team_pattern (기본: hierarchical_delegation) — 검증 단계에서 실제 분류와 대조",
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
            args.name, task_id=args.task_id, prompt=args.prompt, domains_root=args.domains_root
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
        print(
            f"[경고] 기대한 team_pattern={args.pattern!r}이 아니라 "
            f"{result['team_pattern']!r}로 분류됐습니다. --prompt에 라우팅 키워드"
            f'("조사"/"리서치"/"설계 리뷰"/"구현 리뷰" 등, router.py의 _TASK_TYPE_RULES 참고)'
            "가 있는지 확인하세요."
        )

    print(render_followup_checklist(args.name, args.task_id, repo_root=args.domains_root.parent))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
