# ADR 0005: 역할별 확장은 스키마 분기가 아니라 "공유 엔진 + 독립 도메인 폴더"로 한다

- 상태: 확정 (1차 검증: `domains/cloud-ops` 완료)
- 관련 플랜: `harness-implementation-plan-ko.md`, ADR 0001(팀 패턴 분기)

## 배경

"클라우드 운영/장르 소설/일일 비서"처럼 성격이 다른 여러 역할로 하네스를 나눠 쓸 수
있는지 논의하면서, 처음에는 `TaskInput.domain` 필드 + `domains/<domain>/config.json`
+ `_workspace/domains/<domain>/runs/`처럼 **하네스 내부에 도메인 라우팅 축을 추가**하는
안을 검토했다.

방향을 정하기 전에 원래 참고했던 4개 레포 중 `revfactory/harness`와, 거기서 파생된
`revfactory/harness-100`을 "역할별 분리를 어떻게 하는가"라는 질문으로 다시 분석했다.
결과는 명확했다 — 두 레포 다 **하나의 러타임이 내부에서 도메인을 구분하지 않는다.**
`revfactory/harness`는 도메인 설명을 입력받아 그 도메인 전용의 완전히 새로운 프로젝트
(`.claude/agents/` + `.claude/skills/`)를 생성하는 "Team-Architecture Factory"이고,
"다중 도메인 처리는 반복 실행으로 구현된다"고 스스로 명시한다. `harness-100`은 이
패턴이 실제로 100개 규모로 검증된 사례다 — `{NN}-{harness-name}/.claude/` 형태로
도메인마다 완전히 독립된 폴더(콘텐츠 제작/DevOps/데이터-AI/비즈니스 등 10개 카테고리)를
두고, 각각 "독립적이고 병렬 실행 가능"하다.

## 결정

역할별 확장은 하네스 스키마에 `domain` 라우팅 축을 추가하는 대신, **공유 엔진 +
독립 도메인 폴더** 구조로 한다.

```
multi-llm-harness/
  harness-mvp/              # 공유 엔진(변경 없음) — src/harness/, src/providers/, src/fetchers/
  domains/
    cloud-ops/               # 도메인 1: 독립 config.json + examples/ + _workspace/
      config.json
      examples/
      _workspace/runs/
    (novel-writing/, assistant/ 등은 실제로 필요해질 때 같은 패턴으로 추가)
```

- `TaskInput`/`Plan` 등 스키마에 `domain` 필드를 추가하지 않는다. "어느 도메인이냐"는
  "어느 폴더에서 실행했느냐"로 결정된다.
- `harness/config.py`의 `DEFAULT_CONFIG_PATH`를 패키지 설치 위치 기준(`parents[2]`)이
  아니라 **cwd 기준 상대경로**(`Path("config.json")`, `run_store.DEFAULT_WORKSPACE_ROOT`와
  동일한 원칙)로 바꾼 것이 이 구조가 성립하는 유일한 전제 조건이었다 — 그 외
  `run_store`/`dashboard`는 이미 cwd 기준 상대경로라 손댈 필요가 없었다.
- Planner/Judge/Safety 로직은 도메인과 무관하게 완전히 공유한다(복제하지 않음). 도메인별로
  달라지는 건 `config.json`(어떤 모델을 쓰는지)과 `examples/`(어떤 task를 다루는지)뿐이다.

## Fetcher: 도메인이 필요로 하는 새 컴포넌트 종류

`cloud-ops` 도메인(서버 비용 견적)을 1차 검증 대상으로 골랐는데, 이 도메인은 기존
`Provider`(LLM 텍스트 생성)로는 처리할 수 없는 요구가 있었다 — AWS/NCP의 **실제 가격
데이터를 읽어와야** 한다. 이건 "생성"이 아니라 "조회"라서 새 추상화
(`fetchers.Fetcher`, `harness-mvp/src/fetchers/`)를 추가했다: 읽기 전용, 아무것도
바꾸지 않음(액션 실행이 아님). `AwsEc2PriceFetcher`(AWS Price List Bulk API, 인증
불필요)와 `NcpServerPriceFetcher`(NCP Billing API, 계정 키 필요)를 실제 계정으로
검증했다.

Fetcher를 실제 task 파이프라인에 연결하는 배선 코드(`domains/cloud-ops/
run_cost_estimate.py`)는 harness-mvp가 아니라 도메인 폴더에 둔다 — 다른 도메인이
클라우드 가격 조회를 몰라도 되게, "공유 엔진은 도메인 무관하게 유지한다"는 원칙을
그대로 지킨다.

## 이유

- 참고 레포가 이미 대규모(100개)로 검증한 패턴을 따르는 것이 새로 설계하는 것보다
  안전하다.
- 스키마에 `domain` 라우팅 축을 추가하면 harness-mvp 코드(Planner/Router/CLI)가 도메인을
  알아야 하게 되어 "공유 엔진은 도메인 무관"이라는 원칙이 깨진다. 폴더 분리는 코드
  변경 없이(경로 해석 한 줄만 수정) 같은 효과를 낸다.
- Agent Soup 방지/"필요할 때만 만듦" 원칙과도 맞는다 — 도메인 3개를 한꺼번에
  설계·구현하지 않고, `cloud-ops` 하나만 실제로 끝까지(Fetcher 연동 + 실제 API 검증 +
  실제 LLM 호출로 최종 산출물까지) 검증한 뒤 이 패턴을 다른 도메인에도 반복 적용할지
  판단한다.

## 실제 검증 중 발견한 버그 2건 (2026-07-13)

도메인 폴더 자체와는 별개로, `cloud-ops`를 실제 API/LLM으로 끝까지 돌리는 과정에서
공유 엔진의 버그 2건을 발견해 고쳤다(둘 다 harness-mvp의 기존 관행대로 "실제 연동은
기능당 1회 수동 확인"에서 나온 발견).

1. **NCP 서명 URI 접두사 누락**: `NcpServerPriceFetcher`가 서명 대상 URI에서
   `/billing/v1` 접두사를 빠뜨려 401 "Invalid authentication information"이 났다.
   호스트 기준 전체 경로로 서명하도록 수정.
2. **Windows에서 claude `.CMD` 긴 인자 손상**: `ClaudeCliProvider`가 프롬프트를
   커맨드라인 인자로 넘겼는데, judge 프롬프트(candidate 여러 개를 합쳐 약 8KB 이상)처럼
   길어지면 Windows의 `.CMD` 경유 과정에서 멀티바이트(UTF-8) 인자가 깨지는 걸
   재현해서 확인했다. 인자 대신 stdin(`--input-format text` + `input=`)으로 넘기도록
   수정 — 명령줄 길이 제한 자체를 안 탄다. **Codex는 이 환경에 CLI가 없어 같은 수정을
   검증하지 못했다**(구조상 같은 위험이 있을 가능성이 높음, `cli_subscription_provider.py`
   docstring에 후속 확인 필요 사항으로 남겨둠).

## 영향

- `schemas.py`: 변경 없음(`FetchResult` 추가는 스키마 확장이지 도메인 라우팅과 무관)
- `harness/config.py`: `DEFAULT_CONFIG_PATH`를 cwd 기준 상대경로로 변경
- `providers/cli_subscription_provider.py`: `ClaudeCliProvider._invoke()`가 프롬프트를
  stdin으로 전달하도록 변경(버그 수정)
- 신규: `src/fetchers/`(base/aws_price_fetcher/ncp_price_fetcher), `domains/cloud-ops/`
- 문서: 이 ADR, `docs/03_진행상황/*` 갱신
