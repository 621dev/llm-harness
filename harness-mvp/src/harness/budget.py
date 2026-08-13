"""run 하나의 예산 상한 (2026-07-29, ECC `cost-aware-llm-pipeline` 재분석에서 도입).

**왜 필요했나**: 기존에는 `metrics.json`에 비용을 **사후 기록**만 했고, `src/` 전체에
`budget` 개념이 없었다. 라운드/턴 **횟수** 상한(`MAX_REFINEMENT_ROUNDS`,
`MAX_AGENT_TURNS`)은 있었지만 **금액 상한이 없어서**, 한 run이 얼마를 쓰든 도중에
멈출 장치가 없었다. ECC의 원칙은 "배치를 처리하기 전에 명시적 예산 상한을 두고,
초과하면 일찍 실패한다".

**상한이 두 종류인 이유**: 우리 provider 절반이 구독 CLI라 `cost_usd`가 `None`이다
(`subscription_calls`를 따로 세는 이유와 같다 — Section 9 Cost Blindness 방지).
금액과 구독 한도는 **서로 다른 자원이라 합칠 수 없으므로** 각각 상한을 둔다.

- `limit_usd` — 종량제(`auth_mode="api_key"`) 누적 금액
- `limit_calls` — 구독 호출 횟수

둘 다 `None`이면(기본값) 아무것도 막지 않는다 — 기존 run 동작이 그대로 유지된다.

**상한에 걸린 run은 error가 아니라 partial이다.** 이미 만든 산출물을 버리면 그때까지
쓴 비용이 통째로 낭비된다. 체인 중단·`max_turns` 도달과 같은 관례를 따른다.

**막는 시점은 호출 "전"이다.** 이미 쓴 돈은 되돌릴 수 없으니 상한이 할 수 있는 일은
"다음 호출을 시작하지 않는 것"뿐이다.
"""
from __future__ import annotations

import threading
from typing import Optional

from .schemas import Candidate

# 금액 비교용 허용 오차. 작은 비용을 여러 번 더하면 부동소수점 오차로 상한에
# **못 닿는다** — `0.09 + 0.01 = 0.09999999999999999 < 0.10`이라 상한을 그냥
# 지나가는 것을 테스트가 잡았다(2026-07-29). $1e-9는 어떤 실제 과금 단위보다도
# 작아서 "덜 쓴 것으로 봐주는" 오차는 무의미하고, 상한을 놓치는 쪽이 위험하다.
# cost_usd 자체가 이미 추정치(`_estimate_cost`)라는 점도 같은 방향이다.
_USD_EPSILON = 1e-9


class BudgetTracker:
    """run 하나가 지금까지 쓴 양과 상한. 호출부가 만들어서 호출 경로에 넘긴다.

    상태를 모듈 전역이 아니라 인스턴스로 두는 이유: run이 병렬로 돌 때 서로의
    예산을 깎으면 안 되고, 테스트가 격리되어야 한다.
    """

    def __init__(self, *, limit_usd: Optional[float] = None, limit_calls: Optional[int] = None) -> None:
        self.limit_usd = limit_usd
        self.limit_calls = limit_calls
        self.spent_usd = 0.0
        self.subscription_calls = 0
        # fan-out 후보가 병렬로 돌 때 `add()`가 여러 스레드에서 동시에 불린다
        # (2026-08-13, `model_runner._generate_in_parallel`). `x += y`는 읽기-더하기-쓰기
        # 세 단계라 원자적이지 않아서, 두 스레드가 같은 값을 읽으면 **한쪽 비용이
        # 조용히 사라진다** — 비용을 실제보다 적게 보고하는 건 Cost Blindness 그 자체다.
        self._lock = threading.Lock()

    @property
    def unlimited(self) -> bool:
        return self.limit_usd is None and self.limit_calls is None

    @property
    def exhausted(self) -> bool:
        """다음 호출을 시작해선 안 되는 상태인가.

        루프(fan-out 후보, refinement 라운드)가 매 반복 전에 이걸 보고 일찍 빠져나온다 —
        상한에 걸린 뒤 남은 provider마다 실패 기록을 쌓는 소음을 막기 위함이다.
        """
        if self._usd_exhausted:
            return True
        return self.limit_calls is not None and self.subscription_calls >= self.limit_calls

    @property
    def _usd_exhausted(self) -> bool:
        return self.limit_usd is not None and self.spent_usd >= self.limit_usd - _USD_EPSILON

    @property
    def reason(self) -> str:
        """어느 상한에 걸렸는지 사람이 읽을 문장. errors.json에 그대로 들어간다."""
        if self._usd_exhausted:
            return (
                f"예산 상한 도달: 누적 ${self.spent_usd:.4f} >= 상한 ${self.limit_usd:.4f} "
                f"(budget_usd) — 다음 호출을 시작하지 않았다"
            )
        if self.limit_calls is not None and self.subscription_calls >= self.limit_calls:
            return (
                f"구독 호출 상한 도달: {self.subscription_calls}회 >= 상한 {self.limit_calls}회 "
                f"(budget_subscription_calls) — 다음 호출을 시작하지 않았다"
            )
        return "예산 상한에 걸리지 않았다"

    def add(self, candidate: Candidate) -> None:
        """호출 결과를 누적한다.

        **실패한 호출도 누적한다** — 재시도로 소모한 한도는 되돌아오지 않는다
        (`generate_with_retry`가 `subscription_calls`를 attempts로 세는 것과 같은 이유).
        `cost_usd`가 `None`인 구독 호출은 금액에 0을 더하고 횟수로만 잡힌다.
        """
        with self._lock:
            self.spent_usd += candidate.cost_usd or 0.0
            self.subscription_calls += candidate.subscription_calls

    def snapshot(self) -> dict:
        """metrics/errors에 남길 현재 상태."""
        return {
            "spent_usd": round(self.spent_usd, 6),
            "subscription_calls": self.subscription_calls,
            "limit_usd": self.limit_usd,
            "limit_calls": self.limit_calls,
        }
