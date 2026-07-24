"""운영 설정 파일 로더 (harness-mvp/config.json).

fan_out_judge 후보 모델, judge 모델, hierarchical_delegation 모델, 구독
한도 보호 상한처럼 지금까지 cli.py/orchestrator.py에 하드코딩돼 있던
운영 설정을 파일로 뺐다 — 코드를 안 고치고도 설정을 바꿀 수 있게 하기
위해서다(사용자 요청, 2026-07-10).

파일이 없거나 특정 키가 없으면 아래 기본값(지금까지의 하드코딩 값과
동일)을 쓴다 — `config.json`을 안 만들어도 기존 동작 그대로 유지된다.

어떤 모델 이름이 유효한지(claude/codex/gemini)는 여기서 검증하지 않는다 —
provider 레지스트리는 cli.py에 있고, config.py가 cli.py를 가져오면 순환
참조가 생기므로, 유효성 검사는 cli.py가 담당한다.
"""
from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, Field

# run_store.DEFAULT_WORKSPACE_ROOT과 동일한 원칙: 패키지 설치 위치가 아니라 cwd
# 기준 상대경로로 둔다. 도메인 폴더(domains/<name>/)에서 실행하면 그 폴더의
# config.json을 읽고, harness-mvp/에서 실행하면 기존처럼 harness-mvp/config.json을
# 읽는다 — 하나의 공유 엔진을 여러 프로젝트 폴더에서 각자의 설정으로 재사용하기
# 위한 전제 조건이다.
DEFAULT_CONFIG_PATH = Path("config.json")


class HarnessConfig(BaseModel):
    """cli.py가 provider를 구성할 때 참고하는 운영 설정.

    필드 각각의 의미는 harness-mvp/config.json의 주석 대신 여기 docstring에
    적는다(JSON은 주석을 지원하지 않으므로).
    - candidate_models: fan_out_judge 후보로 쓸 모델 목록(레지스트리 이름).
      `run --models`로 매 실행마다 오버라이드 가능(CLI 인자가 우선).
    - judge_model: fan_out_judge 판단에 쓸 모델. ADR 0004 원칙(후보 생성
      모델과 분리해 self-preference bias 완화)을 지키는 선에서 바꿀 것.
    - delegation_model: hierarchical_delegation에서 delegation_role_models에
      명시 안 된 역할에 쓸 기본 모델(역할 분담을 안 쓰면 사실상 전체 역할에
      쓰임 — 기존 동작과 동일).
    - delegation_role_models: hierarchical_delegation의 역할(research/
      design_review/implementation_review)별로 다른 모델을 쓰고 싶을 때만
      채운다(역할 분담, 2026-07-14). 명시 안 된 역할은 delegation_model로
      대체된다. 빈 dict(기본값)면 이전 동작과 완전히 동일 — 전체 역할이
      delegation_model 하나로 통일된다.
    - max_subscription_candidates: fan_out_judge 한 run에서 동시에 쓸 수
      있는 auth_mode="cli_subscription" provider 최대 개수(Section 9 구독
      한도 보호).
    """

    candidate_models: list[str] = Field(default_factory=lambda: ["claude", "codex", "gemini"])
    judge_model: str = "gemini"
    delegation_model: str = "claude"
    delegation_role_models: dict[str, str] = Field(default_factory=dict)
    max_subscription_candidates: int = 1


def load_config(path: Path | None = None) -> HarnessConfig:
    """config.json을 읽어 HarnessConfig로 만든다. 파일이 없으면 기본값을 쓴다."""
    config_path = path if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return HarnessConfig()

    data = json.loads(config_path.read_text(encoding="utf-8"))
    return HarnessConfig.model_validate(data)
