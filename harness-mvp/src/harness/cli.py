"""CLI 진입점: run / replay / approve / reject / safety-queue / safety-approve /
safety-reject (Step 8 + Phase 4).

harness-implementation-plan-ko.md Section 7 Step 8, Section 11(DoD), Phase 4(Safety
Release Gate)를 구현한다.

사용 예 (harness-mvp 디렉토리에서):
  python -m harness.cli run --task examples/task.fan_out.json
  python -m harness.cli run --task examples/task.fan_out.json --models claude,gemini  # 후보 모델 선택
  python -m harness.cli replay <run_id>
  python -m harness.cli approve <run_id>
  python -m harness.cli reject <run_id>
  python -m harness.cli safety-queue                # 검토 대기 중인 run 목록
  python -m harness.cli safety-approve <run_id>      # 오탐으로 판단, 보류 내용 공개
  python -m harness.cli safety-reject <run_id>       # 위험하다고 확정, 계속 보류
  python -m harness.cli analyze-failures             # 전체 run의 실패 패턴 집계 (Phase 5)
  python -m harness.cli dashboard                    # 패턴별 성공/경고/실패율·비용·지연 HTML 리포트 (Phase 6)
  python -m harness.cli worktree-sync                # 모든 도메인 worktree에 origin/main을 한 번에 merge
  python -m harness.cli worktree-check-cleanup       # merge된 PR의 브랜치를 아직 갖고 있는 worktree 탐지(삭제는 안 함)

Phase 3에서 실제 provider(api_provider.py/cli_subscription_provider.py)를 만들고
개별 검증까지 했지만, 이 CLI의 기본 흐름은 한동안 MockProvider에 묶여 있었다.
_default_providers()를 아래처럼 실제 provider로 교체하면서 배선을 마쳤다(진행상황
문서 참고) — orchestrator/cli의 나머지 코드는 그대로 재사용된다.

실사용 전 필요한 자격증명: `claude auth login` / `codex login`(둘 다 구독 로그인)과
`GEMINI_API_KEY` 환경변수. 자격증명이 없거나 호출이 실패해도 별도 fallback은 두지
않는다 — 기존 model_runner의 1회 재시도 + status="error" Candidate 기록 경로가
그대로 처리한다(Section 6 복구 전략 재사용).

ADR 0004(Judge를 규칙 기반에서 단일 실제 LLM 판단으로 승격) 이후, fan_out_judge는
judge용 provider도 필요하다(`orchestrator.JUDGE_PROVIDER_KEY`로 등록).

2026-07-10부터 후보 모델/judge 모델/delegation 모델/구독 한도 상한을
`config.json`(harness-mvp 루트)으로 뺐다 — 코드를 안 고치고도 설정을 바꿀
수 있게 하기 위해서다. 파일이 없으면 `harness.config.HarnessConfig`의
기본값(지금까지의 하드코딩 값과 동일)을 쓴다. `run`/`approve`의 `--models`는
그 실행 한 번만 `config.json`의 `candidate_models`를 오버라이드한다.
judge/delegation 모델은 `--models`로 안 바꾼다 — 판단자를 후보 생성 모델과
분리해두는 게 낫다는 ADR 0004의 원칙을 지키려면 실수로 같이 안 바뀌는 게
안전하다(바꾸려면 `config.json`을 직접 수정).

2026-07-14부터 hierarchical_delegation의 역할(research/design_review/
implementation_review)별로 다른 모델을 쓰는 역할 분담이 가능하다 —
`config.json`의 `delegation_role_models`(예: `{"research": "gemini"}`)에
명시한 역할만 그 모델을 쓰고, 안 된 역할은 `delegation_model`로 대체된다.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Callable, Iterable, Sequence

from providers.api_provider import GeminiApiProvider
from providers.base import Provider
from providers.cli_subscription_provider import ClaudeAgentProvider, ClaudeCliProvider, CodexCliProvider
from providers.fallback_provider import QuotaFallbackProvider

from . import dashboard, failure_analysis, live_status, orchestrator
from .config import HarnessConfig, load_config
from .schemas import ProviderConfig, TaskInput

# fan_out_judge 후보/judge/delegation에 쓸 수 있는 모델 레지스트리. 각 팩토리는
# provider_id를 인자로 받아서 같은 백엔드(claude/codex/gemini)를 역할이 다른
# 자리(후보/judge/delegation)에 재사용할 수 있게 한다. Gemini만
# auth_mode="api_key"(종량제), 나머지는 구독 CLI.
_CANDIDATE_PROVIDER_REGISTRY: dict[str, Callable[[str], Provider]] = {
    "claude": lambda provider_id: ClaudeCliProvider(
        ProviderConfig(provider_id=provider_id, model_id="claude-cli", auth_mode="cli_subscription")
    ),
    "codex": lambda provider_id: CodexCliProvider(
        ProviderConfig(provider_id=provider_id, model_id="codex-cli", auth_mode="cli_subscription")
    ),
    "gemini": lambda provider_id: GeminiApiProvider(
        ProviderConfig(provider_id=provider_id, model_id="gemini-2.5-flash", auth_mode="api_key")
    ),
}

# hierarchical_delegation 기본 provider 역할 — planner.py가 delegation_chain을
# 만들 때 provider_id를 "{role}-mock"으로 하드코딩하므로(test_step4_planner.py에서도
# 이 문자열을 검증) 이름은 그대로 유지한다. content_finalization은 2026-07-27
# planner.py의 _DEFAULT_DELEGATION_ROLES["research"]에 3번째 역할로 추가됨(세부는
# 그쪽 주석 참고) — 여기 등록 안 하면 그 역할의 provider_id를 못 찾아 KeyError.
_DELEGATION_ROLES = ("research", "design_review", "implementation_review", "content_finalization")


def _wrap_with_quota_fallback(role: str, primary_model: str, fallback_model: str | None) -> Provider:
    """역할별 provider를 만든다. `fallback_model`이 있으면 1차가 호출 한도(quota)로
    실패할 때 2차로 자동 전환하는 `QuotaFallbackProvider`로 감싼다(2026-07-27,
    `config.py`의 `delegation_role_fallback_models` 문서 참고)."""
    provider_id = f"{role}-mock"
    primary = _CANDIDATE_PROVIDER_REGISTRY[primary_model](provider_id)
    if fallback_model is None:
        return primary
    fallback = _CANDIDATE_PROVIDER_REGISTRY[fallback_model](f"{provider_id}-fallback")
    return QuotaFallbackProvider(
        primary=primary,
        fallback=fallback,
        config=ProviderConfig(provider_id=provider_id, model_id=primary.model_id),
    )


def _validate_model_names(names: Iterable[str]) -> None:
    unknown = [name for name in names if name not in _CANDIDATE_PROVIDER_REGISTRY]
    if unknown:
        raise ValueError(f"알 수 없는 모델: {unknown} (사용 가능: {sorted(_CANDIDATE_PROVIDER_REGISTRY)})")


def _parse_models(raw: str | None, default: Sequence[str]) -> tuple[str, ...]:
    """--models 인자("claude,gemini")를 파싱한다. 안 주면 config.json의 기본값."""
    if raw is None:
        return tuple(default)

    models = tuple(name.strip() for name in raw.split(",") if name.strip())
    _validate_model_names(models)
    return models


def _default_providers(models: Sequence[str], config: HarnessConfig) -> dict[str, Provider]:
    _validate_model_names(models)

    # 역할별 모델(delegation_role_models)이 명시된 역할은 그 모델을, 안 된 역할은
    # delegation_model(기존 동작)을 쓴다 — delegation_role_models가 빈 dict(기본값)면
    # 전체 역할이 delegation_model 하나로 통일되던 이전 동작과 완전히 같다.
    role_models = {role: config.delegation_role_models.get(role, config.delegation_model) for role in _DELEGATION_ROLES}
    fallback_models = config.delegation_role_fallback_models
    _validate_model_names([config.judge_model, config.delegation_model, *role_models.values(), *fallback_models.values()])

    providers: dict[str, Provider] = {name: _CANDIDATE_PROVIDER_REGISTRY[name](name) for name in models}

    providers.update(
        {
            f"{role}-mock": _wrap_with_quota_fallback(
                role, role_models[role], fallback_models.get(role)
            )
            for role in _DELEGATION_ROLES
        }
    )

    providers[orchestrator.JUDGE_PROVIDER_KEY] = _CANDIDATE_PROVIDER_REGISTRY[config.judge_model]("judge")

    # agentic_task 전용 에이전트 provider (ADR 0007). 모델 레지스트리를 안 거치고
    # claude로 고정한다 — codex는 stream 이벤트 형식이 달라 이번 범위 밖이고,
    # gemini는 애초에 CLI 구독 모드가 없다(모듈 docstring 참고). 이 provider는
    # AGENT_PROVIDER_KEY로만 등록돼 다른 패턴에는 절대 안 섞인다.
    providers[orchestrator.AGENT_PROVIDER_KEY] = ClaudeAgentProvider(
        ProviderConfig(provider_id="agent", model_id="claude-cli", auth_mode="cli_subscription")
    )
    return providers


def _providers_from_args(args: argparse.Namespace) -> dict[str, Provider]:
    """cmd_run/cmd_approve가 공유하는 조립 로직: config.json 로드 -> --models로
    candidate_models 오버라이드 -> provider dict 구성. max_subscription_candidates도
    여기서 orchestrator에 반영한다(단일 프로세스 CLI라 전역 설정으로 충분,
    Section 9)."""
    config = load_config()
    orchestrator.MAX_SUBSCRIPTION_CANDIDATES = config.max_subscription_candidates
    orchestrator.MAX_REFINEMENT_ROUNDS = config.max_refinement_rounds
    orchestrator.MAX_AGENT_TURNS = config.max_agent_turns
    models = _parse_models(args.models, config.candidate_models)
    return _default_providers(models, config)


def _load_task(task_path: Path) -> TaskInput:
    data = json.loads(task_path.read_text(encoding="utf-8"))
    return TaskInput.model_validate(data)


def cmd_run(args: argparse.Namespace) -> int:
    task = _load_task(Path(args.task))
    try:
        providers = _providers_from_args(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    root = Path(args.root) if args.root else None
    observation = orchestrator.run(task, providers, root=root)
    print(f"[{observation.status}] {observation.summary}")
    return 1 if observation.status == "error" else 0


def cmd_replay(args: argparse.Namespace) -> int:
    try:
        files = orchestrator.replay(args.run_id)
    except FileNotFoundError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    if not files:
        print(f"저장된 산출물이 없다: {args.run_id}", file=sys.stderr)
        return 1

    for name, content in files.items():
        print(f"--- {name} ---")
        print(content)
    return 0


def cmd_approve(args: argparse.Namespace) -> int:
    try:
        providers = _providers_from_args(args)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    observation = orchestrator.resume(args.run_id, "approved", providers)
    print(f"[{observation.status}] {observation.summary}")
    return 1 if observation.status == "error" else 0


def cmd_reject(args: argparse.Namespace) -> int:
    # reject 경로는 orchestrator.resume() 안에서 provider를 아예 안 쓰고
    # 조기 반환하므로(candidate/chain 실행 없음), 기본 설정으로 조립해도 무해하다.
    config = load_config()
    providers = _default_providers(config.candidate_models, config)
    observation = orchestrator.resume(args.run_id, "rejected", providers)
    print(f"[{observation.status}] {observation.summary}")
    return 0


def cmd_safety_queue(_args: argparse.Namespace) -> int:
    queue = orchestrator.list_safety_review_queue()
    if not queue:
        print("검토 대기 중인 run이 없다.")
        return 0
    for item in queue:
        print(f"{item['run_id']}: {item['reason']}")
    return 0


def cmd_safety_approve(args: argparse.Namespace) -> int:
    observation = orchestrator.resolve_safety_review(args.run_id, "approved")
    print(f"[{observation.status}] {observation.summary}")
    return 1 if observation.status == "error" else 0


def cmd_safety_reject(args: argparse.Namespace) -> int:
    observation = orchestrator.resolve_safety_review(args.run_id, "rejected")
    print(f"[{observation.status}] {observation.summary}")
    return 0


def cmd_analyze_failures(_args: argparse.Namespace) -> int:
    report = failure_analysis.analyze_failures()
    print(
        f"총 {report.total_runs_scanned}개 run 중 errors {report.runs_with_errors}개, "
        f"safety 검토 {report.runs_with_safety_review}개"
    )
    if report.error_categories:
        print("\n[errors.json stage별 집계]")
        for category in report.error_categories:
            print(f"  {category.count:>3}회  {category.key}  (예: {', '.join(category.example_run_ids)})")
    if report.safety_categories:
        print("\n[safety_review.json finding별 집계]")
        for category in report.safety_categories:
            print(f"  {category.count:>3}회  {category.key}  (예: {', '.join(category.example_run_ids)})")
    if not report.error_categories and not report.safety_categories:
        print("집계된 실패 패턴이 없다.")
    return 0


_HARNESS_MVP_ROOT = Path(__file__).resolve().parents[2]  # harness/cli.py -> harness -> src -> harness-mvp
_REPO_ROOT = _HARNESS_MVP_ROOT.parent


def _discover_workspace_roots_under(checkout_root: Path) -> list[Path]:
    """주어진 저장소 체크아웃 루트(메인 또는 특정 git worktree) 밑에서 harness-mvp
    자체 workspace + domains/*/_workspace 중 실제로 존재하는 후보들의 runs 경로를
    모은다. `_workspace/runs`가 아직 없어도(cloud-ops처럼 LLM run 없이 `estimates/`만
    있는 경우) `_workspace` 자체만 있으면 후보에 넣는다 — list_domain_activity()가
    runs/estimates 둘 다 알아서 확인하므로 여기서 runs 존재를 미리 걸러내면 안 된다."""
    candidates = [checkout_root / "harness-mvp" / "_workspace" / "runs"]
    domains_dir = checkout_root / "domains"
    if domains_dir.is_dir():
        for domain_dir in sorted(domains_dir.iterdir()):
            if (domain_dir / "_workspace").is_dir():
                candidates.append(domain_dir / "_workspace" / "runs")
    return candidates


def _parse_worktree_porcelain_output(output: str) -> list[Path]:
    """`git worktree list --porcelain` 출력에서 worktree 경로만 뽑는다(순수 함수,
    실제 git 호출 없이 테스트 가능)."""
    prefix = "worktree "
    return [Path(line[len(prefix):]) for line in output.splitlines() if line.startswith(prefix)]


def _discover_git_worktrees() -> list[Path]:
    """`git worktree list --porcelain`으로 이 저장소에 딸린 모든 worktree 절대경로를
    찾는다(메인 체크아웃도 포함됨). git이 없거나 이 디렉터리가 git 저장소가 아니면
    빈 리스트로 우아하게 저하한다."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=_REPO_ROOT,
            capture_output=True,
            text=True,
            check=True,
            timeout=10,
        )
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return []
    return _parse_worktree_porcelain_output(result.stdout)


def _discover_all_workspace_roots() -> list[Path]:
    """`--all-domains`용: `git worktree list`로 찾은 모든 worktree(메인 체크아웃
    포함) 각각에서 harness-mvp 자체 workspace + domains/*/_workspace를 전부 모은다
    (2026-07-20 사용자 요청: "웹으로 전체 도메인을 한번에 확인할 순 없나" — worktree는
    물리적으로 분리된 디렉터리라 git한테 직접 물어봐야 찾을 수 있음). git worktree
    조회가 실패하면(git 없음 등) 최소한 지금 이 체크아웃만이라도 대상으로 한다."""
    worktrees = _discover_git_worktrees() or [_REPO_ROOT]
    roots: list[Path] = []
    for worktree_root in worktrees:
        roots.extend(_discover_workspace_roots_under(worktree_root))
    return roots


def cmd_status(args: argparse.Namespace) -> int:
    """지금 뭐가 돌고 있는지 실시간으로 보여준다 — dashboard(회고적 집계)와 달리
    run_meta.json의 pid 생존 여부로 "실행 중"과 "중간에 죽음"을 구분한다.

    기본은 CWD 기준 workspace 하나만 본다. --root를 반복 지정하면 여러
    workspace를 한 번에 합쳐서 보여주고, --all-domains는 `git worktree list`로
    찾은 **모든 worktree**(메인 체크아웃 포함) 각각의 모든 도메인 workspace를
    자동으로 찾아 합친다(2026-07-20 사용자 요청: "여러 도메인의 작업들을
    총체적으로 확인할 순 없을가" → "웹으로 전체 도메인을 한번에 확인할 순 없나"
    → worktree는 물리적으로 분리된 디렉터리라 처음엔 실행 위치의 worktree만
    봤는데, git한테 직접 물어보도록 고쳐서 다른 worktree까지 자동으로 포함되게
    함). --root로 명시한 경로가 있으면 자동 탐색 결과에 추가로 합쳐진다(중복
    제거). cloud-ops처럼 LLM run 없이 결정론적으로 파일만 생성하는 도메인
    작업(`_workspace/estimates/`)도 team_pattern "direct_output"으로 같이
    보여준다(2026-07-20 사용자 요청: "cloud-ops 비용 계산 작업들을 대시보드에서
    확인할 수 있게").

    --output을 주면 그 시점 스냅샷을 정적 HTML로도 남긴다(자동 새로고침 없음,
    dashboard와 같은 원칙 — live_status.render_html() 참고). "이 표를 보는 법"
    가이드는 별도 파일(guide.html, 같은 폴더)로 함께 생성되고 대시보드에서
    링크로 연결된다(2026-07-23 사용자 요청: "가이드를 따로 페이지로 빼줘")."""
    roots: list[Path] = []
    if args.all_domains:
        roots.extend(_discover_all_workspace_roots())
    if args.root:
        roots.extend(Path(r) for r in args.root)
    roots = list(dict.fromkeys(roots))  # 중복 제거(순서 유지)

    show_domain = bool(roots)
    statuses = live_status.list_domain_activity_multi(roots) if roots else live_status.list_domain_activity()

    if not statuses:
        print("run이 없다.")
    for item in statuses:
        label = live_status.STATUS_LABELS.get(item["status"], item["status"])
        elapsed = live_status.format_elapsed(item["started_at"])
        elapsed_note = f" ({elapsed} 경과)" if elapsed else ""
        task_name = f"[{item['task_id']}] " if item.get("task_id") else ""
        domain_prefix = f"({item['domain']}) " if show_domain else ""
        print(f"{domain_prefix}{task_name}{item['run_id']}  [{item['team_pattern']}]  {label}{elapsed_note}")
        if item.get("prompt"):
            preview = item["prompt"] if len(item["prompt"]) <= 60 else item["prompt"][:60] + "..."
            print(f"    └ {preview}")

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        guide_path = output_path.parent / live_status.DEFAULT_GUIDE_FILENAME
        output_path.write_text(
            live_status.render_html(statuses, guide_href=guide_path.name), encoding="utf-8"
        )
        guide_path.write_text(
            live_status.render_guide_html(back_href=output_path.name), encoding="utf-8"
        )
        print(f"\n스냅샷 HTML 생성 완료: {output_path} (자동 새로고침 없음, run {len(statuses)}개)")
        print(f"가이드 페이지 생성 완료: {guide_path}")
    return 0


def _git(args: list[str], *, cwd: Path) -> subprocess.CompletedProcess:
    """`git` 서브커맨드 하나를 실행하고 결과를 그대로 반환한다(예외로 죽지 않음 —
    호출부가 returncode/stdout으로 직접 상태를 판단하는 패턴이라 `check=True`를
    안 쓴다)."""
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)


def _out(result: subprocess.CompletedProcess) -> str:
    """stdout/stderr를 합쳐 문자열로 돌려준다.

    `capture_output=True`면 둘 다 str이어야 하는데, 2026-07-28 실제 실행에서
    `result.stdout`이 None이라 `TypeError`로 죽은 적이 있다(원인은 특정하지 못했다).
    진단 문구를 만드는 자리에서 죽는 건 얻는 것보다 잃는 게 크므로 방어한다.
    """
    return ((result.stdout or "") + (result.stderr or "")).strip()


def _has_unmerged_paths(path: Path) -> bool:
    """이 worktree가 충돌(unmerged) 상태인지 **git 인덱스로** 판정한다.

    stdout에서 "CONFLICT" 문구를 찾는 방식은 두 가지로 취약하다: git이 그 안내를
    stderr로 보내면 놓치고(2026-07-28 실측 — squash merge된 브랜치에 main을 다시
    병합해 실제로 충돌했는데 `conflict`가 아니라 `error`로 라벨링되고 출력도 비어
    나왔다), 로케일/git 버전에 따라 문구 자체도 달라진다.

    이 프로젝트에서 출력 문구에 의존한 판정으로 데인 게 세 번째라(PR #45의
    "Already up to date" 매칭 → HEAD SHA 비교로 교체) 여기서는 인덱스를 직접 본다.
    """
    return bool((_git(["ls-files", "--unmerged"], cwd=path).stdout or "").strip())


def _current_branch(path: Path) -> str:
    return (_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=path).stdout or "").strip()


def _current_commit(path: Path) -> str:
    return (_git(["rev-parse", "HEAD"], cwd=path).stdout or "").strip()


def sync_worktree_with_main(path: Path) -> dict[str, str]:
    """이 worktree(path)에 origin/main을 merge한다. 브랜치가 main 자신이면(메인
    체크아웃) merge commit을 만들 필요가 없으니 --ff-only로 안전하게 pull만 한다
    — fast-forward가 안 된다면 그 자체가 로컬에 main 전용 커밋이 있다는 이상 신호.

    up_to_date/merged 판정은 git stdout 문구("Already up to date" 등) 매칭이 아니라
    merge 전후 HEAD 커밋 SHA를 직접 비교해서 한다 — 문구 매칭은 로케일/git 버전에
    따라 실제 출력이 달라질 수 있다(2026-07-27 실측: PR #44 merge 직후 이 worktree들에
    실제로 fast-forward/merge 커밋이 생겼는데도 stdout에 "Already up to date"가
    섞여 있어 up_to_date로 잘못 라벨링되는 걸 발견 — SHA 비교는 이 문제 자체가
    성립하지 않음)."""
    try:
        branch = _current_branch(path)
        # 이전 병합이 충돌 상태로 멈춰 있으면 merge 자체가 시작되지 않는다
        # ("Merging is not possible because you have unmerged files"). 그대로
        # 진행하면 원인이 안 드러나는 error로 보고되므로 먼저 걸러서 안내한다
        # (2026-07-28 실측: 재실행할 때마다 같은 실패를 반복해 혼란스러웠다).
        if _has_unmerged_paths(path):
            return {
                "path": str(path),
                "branch": branch,
                "status": "conflict",
                "output": "이전 병합이 충돌 상태로 멈춰 있다 — 해당 디렉터리에서 충돌을"
                " 해결하거나 `git merge --abort`로 되돌린 뒤 다시 실행할 것",
            }
        before_commit = _current_commit(path)
        if branch == "main":
            result = _git(["merge", "--ff-only", "origin/main"], cwd=path)
        else:
            result = _git(["merge", "origin/main", "--no-edit"], cwd=path)
    except OSError as exc:
        # 등록은 `git worktree list`에 남아 있는데 디렉터리가 사라진 worktree
        # (앱이 제거했거나 사람이 지운 경우 — 이 프로젝트에서 실제로 여러 번 생겼다).
        # 예전에는 이 하나 때문에 subprocess가 예외를 던져 **나머지 worktree 동기화까지
        # 통째로 중단**됐다(2026-07-28 실측). 하나가 사라진 게 나머지를 못 맞출 이유는
        # 없으므로, 이 worktree만 실패로 보고하고 계속 진행한다.
        return {
            "path": str(path),
            "branch": "?",
            "status": "missing",
            "output": f"디렉터리에 접근할 수 없음({exc}) — `git worktree prune`으로 등록을 정리할 것",
        }

    output = _out(result)
    if result.returncode != 0:
        # 문구 매칭이 아니라 인덱스 상태로 판정한다(_has_unmerged_paths 참고).
        status = "conflict" if _has_unmerged_paths(path) else "error"
    else:
        after_commit = _current_commit(path)
        status = "up_to_date" if after_commit == before_commit else "merged"
    return {"path": str(path), "branch": branch, "status": status, "output": output}


def sync_all_worktrees(worktrees: Sequence[Path]) -> list[dict[str, str]]:
    return [sync_worktree_with_main(wt) for wt in worktrees]


_SYNC_STATUS_LABELS = {
    "up_to_date": "이미 최신",
    "merged": "동기화 완료",
    "conflict": "충돌 — 수동 해결 필요",
    "error": "오류",
    "missing": "디렉터리 없음 — git worktree prune 필요",
}


def cmd_worktree_sync(args: argparse.Namespace) -> int:
    """존재하는 모든 도메인 worktree에 origin/main을 한 번에 merge한다(2026-07-24
    사용자 요청: "워크트리 관리 자동화" — 지금까지 "각 도메인 worktree도 main이랑
    동기화해줘"라고 요청할 때마다 worktree마다 손으로 git merge main을 실행해온 걸
    명령어 하나로 대체). 충돌은 자동으로 풀지 않고 보고만 한다 — 실제로 PR #31에서
    한 번 발생했던 것처럼 사람이 직접 봐야 하는 판단이라서다."""
    worktrees = _discover_git_worktrees() or [_REPO_ROOT]

    fetch = _git(["fetch", "origin", "main"], cwd=_REPO_ROOT)
    if fetch.returncode != 0:
        print(f"[fatal] git fetch origin main 실패:\n{fetch.stderr}")
        return 1

    had_problem = False
    for result in sync_all_worktrees(worktrees):
        label = _SYNC_STATUS_LABELS.get(result["status"], result["status"])
        print(f"{result['path']}  [{result['branch']}]  {label}")
        # missing도 사람이 조치해야 하는 상태다(등록만 남은 worktree → prune 필요).
        # 다만 나머지 worktree 동기화를 막지는 않는다(sync_worktree_with_main 참고).
        if result["status"] in ("conflict", "error", "missing"):
            had_problem = True
            print(f"    {result['output']}")

    if had_problem:
        print("\n충돌/오류가 발생한 worktree는 해당 디렉터리에서 직접 확인 후 수동으로 해결하세요.")
        return 1
    return 0


def find_stale_worktree_branches(worktrees: Sequence[Path]) -> list[dict[str, str]]:
    """main에 이미 완전히 반영돼서 지금 이 worktree만 남기고 있을 이유가 없는
    도메인 worktree를 찾는다.

    처음엔 `gh pr list --state merged`로 "이 브랜치로 merge된 PR이 있었나"를
    물어봤는데, 실제로 `worktree-check-cleanup`을 돌려보니(2026-07-24) 4개
    도메인 worktree 전부가 걸렸다 — **틀린 결과**였다. 이 프로젝트는 PR이 한 번
    merge된 뒤에도 같은 브랜치에서 도메인 작업을 계속 이어가는 패턴이라(예:
    centos-eol-migration-plan-49a2d3는 PR #33 이후로도 계속 커밋이 쌓임), "과거에
    이 브랜치로 PR이 merge된 적 있다"는 "지금 지워도 안전하다"의 근거가 될 수
    없다. 그래서 브랜치 히스토리가 아니라 **지금 이 순간의 실제 상태**로 판단하도록
    바꿨다: (1) `git diff main HEAD`가 비어있어(트리 내용이 main과 완전히 같음)
    main에 없는 고유한 커밋 내용이 하나도 없고, (2) 커밋 안 된 변경사항도 없으면
    (`git status --porcelain`) — 이 worktree를 지금 지워도 잃을 게 없다는 뜻이다.
    삭제는 하지 않고 보고만 한다(오래된 worktree를 실수로 지우는 게 더 위험한
    작업이라 항상 사람이 확인 후 직접 지우게 함)."""
    stale = []
    for worktree_path in worktrees:
        branch = _current_branch(worktree_path)
        if branch == "main":
            continue
        if _git(["status", "--porcelain"], cwd=worktree_path).stdout.strip():
            continue  # 커밋 안 된 변경사항이 있으면 절대 정리 대상 아님
        if _git(["diff", "--quiet", "main", "HEAD"], cwd=worktree_path).returncode == 0:
            stale.append({"path": str(worktree_path), "branch": branch})
    return stale


def cmd_worktree_check_cleanup(args: argparse.Namespace) -> int:
    worktrees = _discover_git_worktrees() or [_REPO_ROOT]
    stale = find_stale_worktree_branches(worktrees)

    if not stale:
        print("정리할 worktree 없음(모든 worktree가 main에 없는 고유 내용을 갖고 있음).")
        return 0

    print("main과 완전히 같아서(고유 내용 없음) 지금 지워도 잃을 게 없는 worktree:")
    for item in stale:
        print(f"  {item['path']}  [{item['branch']}]")
    print("\n삭제는 자동으로 하지 않습니다 — 계속 쓸 계획이면 그냥 두세요. 정리하려면")
    print("Claude Code 앱 worktree 목록에서 지우거나 git worktree remove <경로> &&")
    print("git branch -D <브랜치>로 직접 정리하세요.")
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else None
    report = dashboard.build_dashboard(root=root)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(dashboard.render_html(report), encoding="utf-8")
    print(f"대시보드 생성 완료: {output_path} (run {report.total_runs_scanned}개, 패턴 {len(report.patterns)}개)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m harness.cli")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="task.json으로 run을 처음부터 실행한다")
    run_parser.add_argument("--task", required=True, help="TaskInput JSON 파일 경로")
    run_parser.add_argument(
        "--models",
        default=None,
        help=(
            "fan_out_judge 후보로 쓸 모델 목록, 콤마 구분 (예: claude,gemini). "
            f"사용 가능: {sorted(_CANDIDATE_PROVIDER_REGISTRY)}. "
            "기본값: config.json의 candidate_models(없으면 셋 다)"
        ),
    )
    run_parser.add_argument(
        "--root",
        default=None,
        help="run 저장 위치(기본: CWD 기준 _workspace/runs) — 도메인별 workspace를 명시적으로 지정할 때 사용",
    )
    run_parser.set_defaults(func=cmd_run)

    replay_parser = subparsers.add_parser("replay", help="저장된 run의 산출물을 다시 보여준다")
    replay_parser.add_argument("run_id")
    replay_parser.set_defaults(func=cmd_replay)

    approve_parser = subparsers.add_parser("approve", help='"pending" 상태 run을 승인하고 이어서 실행한다')
    approve_parser.add_argument("run_id")
    approve_parser.add_argument(
        "--models", default=None, help="fan_out_judge 후보로 쓸 모델 목록, 콤마 구분 (run 때와 동일 옵션)"
    )
    approve_parser.set_defaults(func=cmd_approve)

    reject_parser = subparsers.add_parser("reject", help='"pending" 상태 run을 반려하고 종료한다')
    reject_parser.add_argument("run_id")
    reject_parser.set_defaults(func=cmd_reject)

    safety_queue_parser = subparsers.add_parser("safety-queue", help="Safety 검토 대기 중인 run 목록을 보여준다")
    safety_queue_parser.set_defaults(func=cmd_safety_queue)

    safety_approve_parser = subparsers.add_parser(
        "safety-approve", help="Safety 검토 결과 오탐으로 판단, 보류했던 내용을 공개한다"
    )
    safety_approve_parser.add_argument("run_id")
    safety_approve_parser.set_defaults(func=cmd_safety_approve)

    safety_reject_parser = subparsers.add_parser(
        "safety-reject", help="Safety 검토 결과 위험하다고 확정, 계속 보류한다"
    )
    safety_reject_parser.add_argument("run_id")
    safety_reject_parser.set_defaults(func=cmd_safety_reject)

    analyze_failures_parser = subparsers.add_parser(
        "analyze-failures", help="전체 run의 errors.json/safety_review.json을 모아 실패 패턴을 집계한다"
    )
    analyze_failures_parser.set_defaults(func=cmd_analyze_failures)

    status_parser = subparsers.add_parser(
        "status", help="지금 실행 중/중단됨/대기 중인 run을 실시간으로 보여준다(dashboard와 달리 회고적이지 않음)"
    )
    status_parser.add_argument(
        "--output",
        default=None,
        help="지정하면 그 시점 스냅샷을 정적 HTML로도 생성한다(자동 새로고침 없음, 기본: 생성 안 함)",
    )
    status_parser.add_argument(
        "--root",
        action="append",
        default=None,
        help="조회할 workspace 경로(반복 지정해서 여러 도메인을 한 번에 볼 수 있음, 기본: CWD 기준 _workspace/runs 1곳)",
    )
    status_parser.add_argument(
        "--all-domains",
        action="store_true",
        help="harness-mvp 자체 workspace + domains/*/_workspace/runs 전부를 자동으로 찾아 합쳐서 보여준다",
    )
    status_parser.set_defaults(func=cmd_status)

    dashboard_parser = subparsers.add_parser(
        "dashboard", help="저장된 run들의 패턴별 성공/경고/실패율·평균 latency/cost를 정적 HTML로 만든다"
    )
    dashboard_parser.add_argument(
        "--output", default="_workspace/dashboard.html", help="출력 HTML 경로 (기본: _workspace/dashboard.html)"
    )
    dashboard_parser.add_argument(
        "--root",
        default=None,
        help="집계할 workspace 경로(기본: CWD 기준 _workspace/runs) — 도메인별 workspace를 명시적으로 지정할 때 사용",
    )
    dashboard_parser.set_defaults(func=cmd_dashboard)

    worktree_sync_parser = subparsers.add_parser(
        "worktree-sync", help="존재하는 모든 도메인 worktree에 origin/main을 한 번에 merge한다"
    )
    worktree_sync_parser.set_defaults(func=cmd_worktree_sync)

    worktree_check_cleanup_parser = subparsers.add_parser(
        "worktree-check-cleanup",
        help="PR이 이미 merge됐는데 아직 남아있는 worktree를 찾아 보고한다(삭제는 안 함)",
    )
    worktree_check_cleanup_parser.set_defaults(func=cmd_worktree_check_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    # Windows 기본 콘솔 코드페이지(cp949 등)는 요약 메시지에 쓰는 em-dash(—) 같은 문자를
    # 인코딩하지 못해 UnicodeEncodeError로 죽는다. UTF-8을 강제해서 플랫폼 무관하게 만든다.
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")

    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
