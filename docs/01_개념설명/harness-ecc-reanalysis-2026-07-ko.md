# affaan-m/ECC 재분석 — 지금 우리 구조에 적용할 것 (2026-07-29)

`affaan-m/ECC`는 프로젝트 **초기 분석 4개 레포 중 하나**다(당시 정리:
`harness-repo-summary-technical-ko.md` §2). 그때는 이 레포에서 eval 지표
(pass@1/pass@3/pass^3, cost per success)와 품질 4축(Action/Observation/Recovery/
Context Budget)을 가져왔다. 그 뒤 우리 구조가 패턴 4종 + 측정 + 에이전트까지
왔으니, **같은 레포를 지금 눈으로 다시 보면 무엇이 남았는지** 확인한 기록이다.

## 0. 먼저 짚을 것 — 레포의 성격이 그때와 달라졌다

| | 초기 분석 시점 | 2026-07-29 실측 |
|---|---|---|
| 정체성 | 하네스 운영·평가 **원칙 문서** | "Agent Harness Operating System" |
| 규모 | 스킬 2개(`agent-harness-construction`, `eval-harness`) 중심 | skills **281**, agents **67**, commands **94** |
| 대상 | 하네스 설계자 | Claude Code / Codex / Cursor / OpenCode 등 **하네스 사용자** |

**중요한 층위 차이**: ECC는 **Claude Code 자체를 튜닝하는 설정 모음**이다
(agents/skills/hooks가 전부 마크다운 프롬프트). 우리 것은 **여러 LLM을 호출하는
Python 오케스트레이션 엔진**이다. 그래서 281개 스킬 대부분은 우리에게 **부품으로
들어올 수 없다** — 언어별 security/verification 스킬, TDD 게이트, 슬래시 커맨드
같은 건 층이 다르다. 가져올 수 있는 건 **소수의 설계 원칙**뿐이고, 아래가 그
전부다.

(참고: 스타 수가 234,809로 표시되는데 이 성격의 레포로는 비현실적으로 큰
숫자다. 인기도를 근거로 삼지 말고 내용만 보고 판단하는 게 맞다.)

## 1. 재시도 오류 분류 — 실제 결함이었다 (1순위, **2026-07-29 수정 완료 → §7**)

> 아래는 발견 당시 기록이다. 인용한 코드는 이미 고쳐졌으므로 현재 코드로 읽지 말 것.

ECC `cost-aware-llm-pipeline`의 원칙과 안티패턴:

> 일시적 오류(연결 실패/rate limit/서버 오류)에만 재시도하고, 인증·잘못된 요청
> 오류는 즉시 실패시킨다. **"모든 오류에 재시도하는 것"은 영구적 실패에 예산을
> 낭비하는 안티패턴이다.**

우리 `model_runner.generate_with_retry()`(`harness-mvp/src/harness/model_runner.py:77`):

```python
except Exception as exc:  # noqa: BLE001
    last_error = exc
```

**전부 잡아서 무조건 재시도한다.** 그런데 우리는 이미 판별 수단을 갖고 있다 —
`ProviderError.is_quota_error`(2026-07-27 `QuotaFallbackProvider`와 함께 추가,
발생 지점에서 HTTP 429를 보고 표시). 다만 이 플래그를 **`fallback_provider`만 보고
`model_runner`는 안 본다.**

결과적으로 지금 벌어지는 일:

- **한도 초과에도 재시도한다** → 이미 소진된 한도에 호출을 한 번 더 던진다.
  `subscription_calls`가 2로 기록되는 것으로 실제 소모도 확인된다.
- **인증 실패에도 재시도한다** → 키가 틀린 건 두 번 해도 틀리다. 지연만 2배.

Gemini 일 20회 한도로 측정이 막혔던 게 이번 주 실제 경험이라, **한도 오류에
재시도를 얹는 건 가장 아픈 자리에 낭비를 더하는 것**이다.

**다음 작업 목록의 "백오프 재시도"보다 이게 먼저다.** 영구적 실패에 백오프를
붙이면 "더 느리게 실패"할 뿐이다. 순서는 **분류 → 그다음 백오프**.

적용 범위도 작다: `generate_with_retry`의 `except` 한 곳 + 테스트. 재시도하지
않을 조건을 `is_quota_error`로 시작해 필요하면 넓히면 된다.

## 2. 예산 상한 — 우리에게 **개념 자체가 없다** (2순위)

ECC는 호출 **전에** 누적 비용을 확인하고 초과 시 중단한다:

```python
if tracker.over_budget:
    raise BudgetExceededError(tracker.total_cost, tracker.budget_limit)
```

우리는 `metrics.json`에 `estimated_cost_usd`를 **사후 기록**만 한다.
`src/` 전체에 `budget` 문자열이 0건이다. 즉 **run이 얼마를 쓰든 멈출 장치가
없다.** 원리상 상한이 없는 자리들:

| 패턴 | 최대 호출 |
|---|---|
| `fan_out_judge` | 후보 N + judge 1 (+ 재시도) |
| `iterative_refinement` | 라운드 상한 × (생성 + 판정) |
| `agentic_task` | 턴 상한 × 모델 내부 호출 |

라운드/턴 **횟수** 상한은 있지만 **금액** 상한은 없다. 우리 구조에 맞게 옮기려면
ECC와 다른 점 하나를 반영해야 한다: 우리 provider 절반이 구독 CLI라 `cost_usd`가
`None`이다. 그래서 **상한이 두 종류**여야 한다.

- `budget_usd` — 종량제(`auth_mode="api_key"`) 누적 금액
- `budget_subscription_calls` — 구독 호출 횟수(이미 집계 중인 `subscription_calls` 재사용)

둘 다 `HarnessConfig`에 두면 기존 `max_refinement_rounds`/`max_agent_turns`와
같은 결이 된다(비용·부수효과 직결 값은 코드 밖으로 — 기존 선례).

주의할 설계점: 상한에 걸린 run을 `error`로 끝내면 이미 만든 산출물이 버려진다.
우리는 이미 **partial 승격**이라는 관례가 있으니(체인 중단, `max_turns` 도달)
예산 초과도 같은 규칙으로 다루는 게 일관된다.

## 3. 복잡도 기반 모델 라우팅 — **부분만 해당** (낮은 우선순위)

ECC는 입력 길이/항목 수 임계값으로 저렴한 모델을 먼저 쓴다(Haiku 1x → Sonnet 4x
→ Opus 19x).

우리에게 이미 대응물이 있다 — **적합성 게이트**가 "하네스를 걸칠 가치가 있나"를
Planner보다 먼저 싸게 판정하고 탈락 시 `direct_call`로 보낸다(Section 12.1, 이
원칙의 출처가 애초에 ECC의 cost per success다). 즉 **거친 단위의 같은 아이디어를
이미 갖고 있다.**

차이는 게이트가 통과된 뒤 **모델 등급 선택은 없다**는 점이다(역할별 provider가
config 고정). 다만 우리 provider 구성이 구독 CLI(정액) + Gemini 무료 티어라
**금액 기준 라우팅의 이득이 애초에 작다.** 종량제 모델을 여러 등급 쓰게 되면
그때 다시 볼 항목으로 남긴다.

## 4. run 간 학습(Memory Vault) — 개념적으로는 가장 큰 갭, 그러나 그대로는 못 가져온다

ECC `unified-memory`는 `ecc.memory.v1` 마크다운을 project/team/user 스코프로 두고
여러 하네스가 공유한다. `continuous-learning`은 git 이력에서 패턴을 추출해
"instinct"로 만든다.

우리 하네스는 **run 사이에 아무것도 배우지 않는다.** 매 run이 백지에서 시작하고,
축적은 전부 사람이 읽는 문서(`docs/03_진행상황/`)에만 쌓인다.

그런데 ECC 것을 그대로 도입하는 건 맞지 않는다:

- 별도 npm 런타임(`ecc-universal`) 설치가 전제다 — 우리 Python 단일 스택에 런타임
  하나를 더 얹는 비용 대비 이득이 불분명하다.
- 목적이 다르다. ECC의 vault는 **IDE에서 에이전트 간 인수인계**용이다. 우리는
  단일 엔진이 배치로 도는 구조라 "다른 하네스에 넘긴다"는 시나리오가 없다.

우리에게 맞는 좁은 조각은 하나뿐이고, **이미 다음 작업 목록에 있다**:
`iterative_refinement`의 **피드백 히스토리 누적**(현재는 라운드마다 직전 피드백만
전달 → 같은 지적이 반복되는 것을 모델이 모른다). 이건 run 안의 학습이고 외부
런타임이 필요 없다. **ECC 재분석의 결론은 "새 항목 추가"가 아니라 "그 항목의
우선순위 근거 보강"이다.**

## 4.5 범용 엔지니어링 스킬 검토 (도메인 업무 스킬 제외)

281개 중 언어/프레임워크 패턴 54 + 테스트 24 + 보안 13 = **91개가 "코딩 표준
프롬프트"**다(Python/Rust/Go/Kotlin/Swift/React/Django/Laravel/Spring/Quarkus…
프레임워크별로 `-patterns`/`-tdd`/`-testing`/`-security`/`-verification` 세트 반복).
우리 스택(Python)과 겹치는 2개를 빼면 해당 없고, 우리 코딩 규칙은 이미
`docs/00_작업규칙`에 프로젝트 실정에 맞춰 쌓여 있다.

**채택: `ai-regression-testing`.** 주장 두 개가 우리 테스트 규칙을 정면으로 겨냥한다 —
"같은 모델이 코드를 쓰고 리뷰하면 양쪽에 같은 맹점을 들고 간다", **"AI가 만드는 회귀
1위는 sandbox 경로와 production 경로의 불일치"**. 우리 대응물은 `MockProvider` 경로
vs 실제 provider 경로이고, 규칙상 테스트 전부가 mock 경로만 본다. 이미 세 번 데였다:

| 사고 | mock 테스트가 못 잡은 이유 |
|---|---|
| `agentic_task` 경계 뚫림 | 우리가 만든 fake CLI는 `--allowedTools`를 규칙대로 해석. 실제 CLI는 "사전 승인"으로 해석 |
| `"CONFLICT" in stdout` 오판 | 우리가 stdout에 넣은 fake 문자열로 통과. 실제 git은 stderr로 보냄 |
| `QuotaFallbackProvider.auth_mode` | 계약 검증 테스트가 없어 측정 중 우연히 발견 |

대응 관행은 이미 독립적으로 만들어 뒀다(`verify_*.py` 3종, pytest 밖 수동 실행).
남은 갭은 **Provider 구현체 8개의 계약 준수를 확인하는 테스트가 0건**이었다는 것 —
`test_provider_contract.py`로 메웠다(§7 참고).

**부분 채택: `error-handling`.** 대부분 TS 예제지만 원칙 하나가 걸렸다 — "모든
`catch`는 처리·재전파·로깅 중 하나는 해야 한다". `generate_with_retry`가
`last_error`로 덮어써서 1차 실패 원인을 버리고 있었다. §1과 같은 자리라 함께 고쳤다.

**제외: `regex-vs-llm-structured-text`.** "결정적 파싱 먼저, 낮은 신뢰도만 LLM으로
승급"인데 우리는 이미 그 원칙 위에 있다(deterministic grader 우선, ADR 0008 규칙 기반
합성 — 애초에 출처가 ECC `eval-harness`). 새로 얻을 게 없다.

## 5. 가져오지 않을 것 (명시적 제외)

| 항목 | 제외 이유 |
|---|---|
| `verification-loop`(build/typecheck/lint/test 게이트) | JS/TS 프로젝트의 IDE 세션용. 우리 CI 대응물은 `pytest`로 이미 있다 |
| TDD RED/GREEN/REFACTOR 스킬 | 우리 산출물은 코드가 아니라 문서/절차서 |
| 언어별 security/verification 스킬 다수 | 층위가 다름(코드 리뷰 프롬프트) |
| 슬래시 커맨드 / hooks / 플랫폼 어댑터 | Claude Code 설정층. 우리 엔진과 무관 |
| prompt caching | 우리 프롬프트는 태스크마다 달라 반복 system 프롬프트가 없다 |

## 6. 부수 확인 — 우리 안전 경계가 독립적으로 수렴했다

ECC `autonomous-agent-harness`의 "Consent and Safety Boundaries":

> 자율 실행은 사용자가 **명시적으로 요청하고 범위를 정해야** 한다. 승인 없이는
> 스케줄 생성·원격 디스패치·영속 메모리 기록·컴퓨터 제어를 하지 않는다.
> 반복/이벤트 기반 실행을 켜기 전에 **dry-run과 로컬 큐 파일을 우선**한다.

우리 `agentic_task`(ADR 0007)가 독립적으로 도달한 결론과 같다 — **opt-in 전용
(`constraints: ["team_pattern:agentic_task"]`) + `risk_level="high"` 강제 + 사람
승인 체크포인트.** `jikime/harness-lab`을 "우리 원칙이 다른 곳에서도 통용되는지
확인"용으로 썼던 것과 같은 성격의 교차 검증이다.

단, ECC에도 **프로세스 수준 격리는 없다**(우리와 동일한 한계). 우리 다음 작업의
"`agentic_task` Bash 허용 — 컨테이너 격리 선행"에 참고할 선례는 이 레포에 없다.

## 결론 — 실제로 잡을 것

| 순위 | 항목 | 근거 | 크기 |
|---|---|---|---|
| 1 | **재시도 오류 분류** | 현재 코드에 실제 낭비 존재. 판별 수단(`is_quota_error`)은 이미 있고 안 쓰고 있을 뿐 | 작음(`except` 한 곳 + 테스트) |
| 2 | **예산 상한**(금액 + 구독 호출 2종) | 개념 자체가 없음. 횟수 상한만 있고 금액 상한 없음 | 중간(config + finalize 경로) |
| — | 복잡도 기반 모델 라우팅 | 적합성 게이트로 거친 단위는 이미 있음. 종량제 다등급 쓸 때 재검토 | 보류 |
| — | run 간 학습 | 좁은 조각(피드백 히스토리)만 해당 — 기존 항목의 우선순위 근거로 반영 | 기존 항목 |

## 7. 실행 결과 (2026-07-29)

**재시도 오류 분류 + Provider 계약 테스트를 구현했다.** 세부 경위는
`docs/03_진행상황/harness-progress-detail-ko.md`의 "ECC 재분석 → …" 절.

- `ProviderError.is_auth_error` + 파생 `is_retryable` 추가, `api_provider`가 401/403
  표시, `model_runner`가 재시도 전에 분류. 시도별 오류를 전부 보존
- `test_provider_contract.py` 신규 — 리플렉션으로 구현체를 찾아 등록표 누락을 잡고,
  실패가 `ProviderError`인지까지 고정
- 작성 중 두 가지를 잡았다: (1) 계약 테스트가 `CliSubscriptionProvider`/`ApiProvider`가
  **중간 기반 클래스**임을 드러냈고(등록표를 `_CONCRETE`/`_ABSTRACT_BASES`로 분리),
  (2) API 키가 설정된 머신에서는 계약 테스트가 **실제 API를 호출**할 수 있었다(환경변수를
  명시적으로 비워서 차단, `requests.post`를 예외로 바꿔놓고 검증)
- 테스트 334 → **346개**

**예산 상한(§2)은 미착수** — 비용 직결이라 진행 여부를 따로 확인받는다.
