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

from typing import Optional

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
      design_review/implementation_review/content_finalization)별로 다른
      모델을 쓰고 싶을 때만 채운다(역할 분담, 2026-07-14). 명시 안 된 역할은
      delegation_model로 대체된다. 빈 dict(기본값)면 이전 동작과 완전히
      동일 — 전체 역할이 delegation_model 하나로 통일된다.
    - delegation_role_fallback_models: 역할별 1차 모델이 호출 한도(quota/
      rate-limit)로 실패했을 때 대신 쓸 모델(2026-07-27,
      `providers.fallback_provider.QuotaFallbackProvider`). 예:
      `{"research": "codex"}` — research 자리 모델(예: gemini)이 429로
      실패하면 codex로 자동 전환. 명시 안 된 역할은 폴백 없이 기존처럼
      실패가 그대로 기록된다. 한도 소진이 아닌 다른 실패(응답 형식 오류,
      인증 실패 등)는 폴백하지 않고 그대로 전파된다 — "실패를 조용히
      감추지 않는다"는 원칙은 quota 케이스 하나로만 예외를 둔다.
    - max_subscription_candidates: fan_out_judge 한 run에서 동시에 쓸 수
      있는 auth_mode="cli_subscription" provider 최대 개수(Section 9 구독
      한도 보호).
    - max_refinement_rounds: iterative_refinement의 라운드 상한(ADR 0006).
      라운드마다 generator+evaluator 2회 호출이 발생하므로 비용에 직결된다 —
      최악의 경우 LLM 호출 수는 이 값 × 2 (+재시도).
    - max_agent_turns: agentic_task에서 에이전트가 도구를 호출하며 진행할 수
      있는 최대 턴 수(ADR 0007). 구독 사용량과 "얼마나 많은 파일을 만들 수
      있는가"에 직결되는 값이라 코드 밖으로 뺐다.
    - budget_usd / budget_subscription_calls: run 하나의 예산 상한
      (2026-07-29). 위 max_* 값들은 **횟수** 상한이고, 이 둘은 **소모량** 상한이다 —
      라운드 수가 같아도 프롬프트/응답이 길면 금액은 몇 배가 될 수 있다.
      금액과 구독 한도는 서로 다른 자원이라 합칠 수 없어 따로 둔다(구독 호출은
      cost_usd가 None이라 금액 지표에 안 잡힌다). 둘 다 null(기본값)이면 아무것도
      막지 않는다. 상한에 걸리면 run은 error가 아니라 partial로 끝난다 — 이미 만든
      산출물을 버리면 그때까지 쓴 비용이 통째로 낭비되므로.
    - agent_system_prompt: agentic_task 에이전트에 주입할 시스템 프롬프트
      (2026-07-29). null이면 기본값 — 작업공간이 격리돼 있다는 것, 쓸 수 있는
      도구가 Read/Write/Edit뿐이라는 것, 결과물이 응답 요약이 아니라 파일이라는
      것을 미리 알려준다(실측에서 에이전트가 초반 2~3턴을 방향 파악과 차단된
      도구 시도에 썼다 — 턴은 곧 구독 한도다). 도메인 성격에 맞게 바꿀 수 있고,
      빈 문자열을 주면 주입을 끈다.
    - use_learned_notes: `learned.md`(사람이 쓴 학습 메모)를 다음 run 프롬프트에
      주입할지 (2026-07-29, 기본 true). run 관측 **기록**은 이 값과 무관하게 항상
      자동으로 쌓인다 — 끄는 건 반영 쪽뿐이다. 자동 집계
      (`learned/observations.jsonl`)는 애초에 주입되지 않는다: 사람이 읽고 판단해
      `learned.md`에 쓴 것만 반영된다("기록은 자동, 반영은 명시적").
    - judge_fallback_model / candidate_fallback_model: judge와 fan_out 후보가 호출
      한도로 실패할 때 전환할 2차 모델 (2026-07-29). null이면 폴백 없음(그전 동작).
      체인 역할에만 있던 `delegation_role_fallback_models`를 이 두 자리로 확장한 것 —
      judge가 종량제(gemini)였을 때는 한도가 사실상 안 말라 필요가 없었는데, judge를
      구독(codex)으로 옮기면서 **한도가 마르면 fan_out run 전체가 실패**하는 경로가
      생겼다. "claude 메인 / codex 서브 / gemini 보조(넘침 처리)" 구성의 '보조'가
      실제로 동작하려면 이 두 자리에도 폴백이 필요하다.
    """

    candidate_models: list[str] = Field(default_factory=lambda: ["claude", "codex", "gemini"])
    judge_model: str = "gemini"
    delegation_model: str = "claude"
    delegation_role_models: dict[str, str] = Field(default_factory=dict)
    delegation_role_fallback_models: dict[str, str] = Field(default_factory=dict)
    max_subscription_candidates: int = 1
    max_refinement_rounds: int = 3
    max_agent_turns: int = 8
    budget_usd: Optional[float] = None
    budget_subscription_calls: Optional[int] = None
    agent_system_prompt: Optional[str] = None
    use_learned_notes: bool = True
    judge_fallback_model: Optional[str] = None
    candidate_fallback_model: Optional[str] = None


def load_config(path: Path | None = None) -> HarnessConfig:
    """config.json을 읽어 HarnessConfig로 만든다. 파일이 없으면 기본값을 쓴다."""
    config_path = path if path is not None else DEFAULT_CONFIG_PATH
    if not config_path.exists():
        return HarnessConfig()

    data = json.loads(config_path.read_text(encoding="utf-8"))
    return HarnessConfig.model_validate(data)
