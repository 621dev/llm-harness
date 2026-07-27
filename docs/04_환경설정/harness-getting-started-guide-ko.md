# 시작 가이드 (일반인용, CLI)

작성일: 2026-07-24

터미널을 직접 안 치고 Claude Desktop과의 대화로 진행하고 싶다면
`harness-getting-started-guide-claude-desktop-ko.md`를 볼 것.

용도: "이 코드가 정확히 뭘 하는지"보다 **일단 손으로 한 번 돌려보고 싶은 사람**을
위한 가이드. 개념 설명은 `docs/01_개념설명/`, 전체 스펙은 `docs/02_구현플랜/`을
보되, 여기서는 순서대로 명령어만 따라가면 된다. 각 단계는 실제로 실행해서
확인한 것만 적었다.

**전제**: Python 3.10 이상, 터미널 사용 가능. Python/Node.js/claude·codex CLI
자체가 하나도 없는 완전 초기 머신이라면 이 가이드 전에
`harness-new-machine-setup-guide-ko.md`부터 볼 것.

---

## 0. 이 가이드로 결국 하게 되는 것

1. 설치 확인(비용 없음)
2. 자격증명 없이 "도메인 만들기" 로직만 먼저 확인(비용 없음)
3. Gemini API 키 하나만으로 실제 LLM 호출 한 번 해보기(무료/저가)
4. 내 주제로 진짜 도메인 하나 만들어서 돌려보기

## 1. 설치

```bash
git clone <저장소 URL>   # private: 621dev/multi-llm-harness, public 미러: 621dev/llm-harness
cd multi-llm-harness/harness-mvp   # (또는 llm-harness/harness-mvp)
pip install -e .[dev]
```

## 2. 설치 확인 — 테스트 실행 (비용 없음)

```bash
python -m pytest tests/ -v
```

전부 통과해야 정상(현재 301개). 여기 있는 테스트는 전부 실제 LLM/외부 API를
호출하지 않는다(모킹) — 그냥 코드가 온전한지만 확인하는 단계라 자격증명이
전혀 없어도 된다.

## 3. 자격증명 없이 "도메인 만들기"부터 먼저 확인 (비용 없음)

이 하네스는 "도메인"(예: 비용 견적, 리서치, 절차서 검토 등 하나의 주제 영역)
단위로 쓴다. 진짜로 LLM을 부르기 전에, 어떤 프롬프트가 어떤 방식(팀 패턴)으로
분류되는지부터 공짜로 확인해볼 수 있다:

```bash
python scripts/new_domain.py my-first-domain \
  --task-id hello \
  --prompt "경쟁사 A/B/C의 가격 정책을 리서치해줘. 그 다음 설계 리뷰를 진행해줘." \
  --pattern hierarchical_delegation
```

성공하면:
- `domains/my-first-domain/config.json`, `domains/my-first-domain/examples/task.hello.json`이 생성됨
- 화면에 `team_pattern: hierarchical_delegation`이 뜨면 정상(프롬프트에 "리서치"/
  "설계 리뷰" 같은 키워드가 있어야 이렇게 분류된다 — 없으면 기본값인
  `fan_out_judge`로 분류되고 경고가 뜬다)
- 이 단계는 LLM을 전혀 호출하지 않는다(규칙 기반 분류 로직만 실행) — 마음껏
  여러 번 시도해봐도 비용이 안 든다

## 4. 실제 LLM으로 처음 실행해보기

여기서부터는 실제 LLM 호출이라 자격증명이 필요하다. 가장 빠르고 저렴한 방법은
**Gemini API 키 하나만** 발급받는 것이다(Google AI Studio에서 무료로 발급 가능,
소액 종량제).

```bash
# PowerShell
$env:GEMINI_API_KEY = "여기에_실제_키"
# bash
export GEMINI_API_KEY="여기에_실제_키"
```

이 환경변수는 터미널을 새로 열면 사라진다(임시). 계속 쓰려면 시스템 환경변수로
등록하거나, 매번 새 터미널에서 다시 설정할 것.

기본 설정(`harness-mvp/config.json`)은 `fan_out_judge`에 claude/codex/gemini
3개를, `hierarchical_delegation` 역할별로도 서로 다른 모델을 쓰도록 돼 있다.
**Gemini 키 하나만 있다면** `hierarchical_delegation`이 더 쉽다 —
`fan_out_judge`는 최소 2개 모델이 성공해야 하는 구조라 모델 1개로는 애초에 안
된다. `harness-mvp/config.json`을 열어 아래처럼 전부 `gemini`로 맞춰두면
Gemini 키 하나로 끝까지 실행된다(파일이 원래 이 값이면 그대로 둬도 됨):

```json
{
  "candidate_models": ["gemini"],
  "judge_model": "gemini",
  "delegation_model": "gemini",
  "delegation_role_models": {}
}
```

이제 실제로 실행:

```bash
python -m harness.cli run --task examples/task.delegation.json
```

`run_id`가 출력되고 잠시 뒤 결과가 나온다. 다시 보고 싶으면:

```bash
python -m harness.cli replay run-delegation-demo
```

**주의**: 이 명령은 실제로 Gemini API를 호출해서 소액이지만 실제 비용이
발생한다(토큰 단위 종량제, 보통 한 번에 몇 원~몇십 원 수준). claude/codex
CLI까지 로그인해서 `fan_out_judge`(여러 모델이 후보를 만들고 서로 비교)까지
써보고 싶다면 `harness-new-machine-setup-guide-ko.md`의 4절(claude/codex CLI
설치+로그인)을 따라갈 것 — 이쪽은 구독 계정이 있으면 그 구독 사용량만
소모되고 별도 API 종량제 비용은 없다.

## 5. 내 주제로 진짜 도메인 만들어서 돌려보기

3번에서 만든 `my-first-domain`을 실제로 써보려면, 프롬프트를 실제 관심사로
바꾸고 그 도메인 폴더 안에서 실행하면 된다:

```bash
cd domains/my-first-domain
python -m harness.cli run --task examples/task.hello.json
```

이렇게 하면 결과(`_workspace/runs/`)가 `harness-mvp` 공용 workspace가 아니라
이 도메인 폴더 밑에 따로 쌓인다 — 도메인이 여러 개여도 서로 안 섞인다.

## 6. 지금까지 뭘 실행했는지 한눈에 보기

```bash
cd harness-mvp   # 또는 도메인 폴더 안에서
python -m harness.cli status --all-domains --output _workspace/overview.html
```

`_workspace/overview.html`을 브라우저로 열면 지금까지 실행한 모든 도메인의
run을 표로 볼 수 있다(도메인/team_pattern/상태 필터 포함). 자세한 설명은
생성된 페이지 안의 "이 표를 보는 법" 링크를 참고.

## 다음에 볼 것

- **전체 스펙이 궁금하면**: `docs/02_구현플랜/harness-implementation-plan-ko.md`
  (비개발자용 요약은 `harness-implementation-plan-summary-beginner-ko.md`)
- **코드 구조가 궁금하면**: `harness-mvp/README.md`
- **cloud-ops처럼 Fetcher(실측 가격 조회)까지 쓰는 "무거운" 도메인을 만들고
  싶다면**: `harness-mvp/docs/adr/0005-domain-folder-architecture.md`의 설계
  결정을 참고해서 `harness-mvp/src/fetchers/`의 기존 Fetcher를 재사용하는
  방식으로 직접 스크립트를 작성할 것(`new_domain.py`는 Fetcher 없는 "가벼운"
  도메인 전용이라 이 패턴은 자동화 대상이 아님)
- **막히면**: `harness-new-machine-setup-guide-ko.md`의 "겪은 문제" 절 확인
