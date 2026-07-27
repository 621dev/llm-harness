"""Agent Runner: 자율 에이전트 실행을 감싸는 하네스 층 (agentic_task 전용, ADR 0007).

`subagent_runner.py`가 hierarchical_delegation 전용 실행을 맡는 것과 같은 자리다.
차이는 감싸는 대상의 성격이다 — subagent_runner는 "단발 완성 호출"을 순서대로
엮지만, 여기서는 **스스로 도구를 호출하며 여러 턴을 진행하는 에이전트**를 감싼다.
하네스는 루프 안에 개입하지 않고, 대신 경계와 기록을 책임진다:

1. 작업공간 격리 — run 디렉토리 안의 빈 폴더를 만들어 그 안에서만 일하게 한다
   (에이전트가 사용자의 실제 프로젝트를 건드릴 수 없다)
2. 실제 산출물 확인 — 에이전트의 자기 보고가 아니라 실행 후 파일 시스템을 직접
   스캔해서 무엇이 만들어졌는지 판정한다
3. 행동 기록 — 턴별 도구 호출을 agent_turns.json으로 남긴다(judging.json/
   refinement.json과 같은 관례). 감쌀 대상의 내부를 관측 못 하면 감싼다고 할 수 없다

도구 허용목록/턴 상한 같은 "실행 시점 제약"은 provider가 CLI 인자로 강제한다
(`providers/cli_subscription_provider.py`의 `ClaudeAgentProvider`) — 하네스가
사후에 검사하는 게 아니라 애초에 못 하게 막는 쪽이 안전하기 때문이다.
"""
from __future__ import annotations

from pathlib import Path
from typing import Protocol

from . import run_store
from .schemas import AgentRunResult

AGENT_WORKSPACE_DIRNAME = "artifacts/agent_workspace"
AGENT_TURNS_FILENAME = "agent_turns.json"

# 산출물 스캔에서 제외할 이름 — 에이전트가 만들 수 있는 도구/캐시 부산물이지
# 작업 산출물이 아니다.
_IGNORED_NAMES = {".claude", "__pycache__", ".git"}


class AgentProvider(Protocol):
    """`ClaudeAgentProvider`가 만족하는 최소 계약.

    구체 클래스가 아니라 Protocol에 의존해서, 테스트가 실제 CLI 없이 대역을
    넘길 수 있게 한다(자동 테스트는 실제 CLI를 절대 호출하지 않는다는 규칙).
    """

    def run_agent(self, prompt: str, workspace: Path, *, max_turns: int) -> AgentRunResult:
        ...


def run_agent_task(provider: AgentProvider, prompt: str, run_dir: Path, *, max_turns: int) -> AgentRunResult:
    """에이전트를 격리된 작업공간에서 실행하고, 산출물/행동 기록을 채워 반환한다."""
    workspace = agent_workspace(run_dir)
    workspace.mkdir(parents=True, exist_ok=True)

    result = provider.run_agent(prompt, workspace, max_turns=max_turns)
    result.produced_files = list_produced_files(workspace)

    run_store.write_json(
        run_dir, AGENT_TURNS_FILENAME, [turn.model_dump(mode="json") for turn in result.turns]
    )
    return result


def agent_workspace(run_dir: Path) -> Path:
    """에이전트가 파일을 쓸 수 있는 유일한 위치."""
    return run_dir / AGENT_WORKSPACE_DIRNAME


def list_produced_files(workspace: Path) -> list[str]:
    """작업공간에 실제로 남은 파일을 워크스페이스 기준 상대경로로 나열한다.

    에이전트가 "만들었다"고 말한 것이 아니라 실제로 존재하는 것만 센다 — 이
    구분이 partial 판정(뭐라도 남겼는가)과 Safety 스캔 대상 선정의 근거가 된다.
    """
    if not workspace.is_dir():
        return []
    files = [
        path.relative_to(workspace).as_posix()
        for path in workspace.rglob("*")
        if path.is_file() and not _is_ignored(path.relative_to(workspace))
    ]
    return sorted(files)


def read_produced_texts(workspace: Path, produced_files: list[str], *, max_bytes: int = 200_000) -> list[str]:
    """생성된 파일 내용을 Safety 스캔용으로 읽는다.

    텍스트로 못 읽는 파일(바이너리 등)은 건너뛴다 — 규칙 기반 Safety 스캐너가
    다룰 수 있는 대상이 아니고, 억지로 디코딩하면 오탐만 는다. 파일 하나가
    지나치게 크면 앞부분만 본다(스캔이 run을 멈춰 세우지 않도록).
    """
    texts: list[str] = []
    for name in produced_files:
        path = workspace / name
        try:
            texts.append(path.read_text(encoding="utf-8")[:max_bytes])
        except (OSError, UnicodeDecodeError):
            continue
    return texts


def _is_ignored(relative: Path) -> bool:
    return any(part in _IGNORED_NAMES for part in relative.parts)
