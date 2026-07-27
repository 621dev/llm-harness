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

## 7. 한 걸음 더 — 나머지 두 패턴

여기까지는 프롬프트의 키워드를 보고 자동으로 골라지는 두 패턴
(`hierarchical_delegation`, `fan_out_judge`)만 썼다. 패턴은 두 개가 더 있는데,
**자동으로는 절대 안 걸리고 task 파일에 명시해야만 쓰인다**. 비용이 크거나
(반복 호출) 실제 파일을 만드는 부수 효과가 있어서, 모르고 걸리는 일이 없도록
일부러 그렇게 해뒀다.

### `iterative_refinement` — 통과할 때까지 고쳐 쓰기

한 번 생성하고 끝내는 대신, **평가자가 기준(rubric) 충족 여부를 판정하고
불통과면 피드백을 반영해 다시 쓰는** 걸 반복한다. 치트시트·요약표처럼 "형식을
갖춘 완성물"이 필요할 때 쓴다.

task 파일에 `constraints`만 넣으면 된다:

```json
{
  "task_id": "cheatsheet",
  "prompt": "리눅스 필수 명령어 치트시트를 표로 작성해줘.",
  "constraints": ["team_pattern:iterative_refinement"]
}
```

저장소에 예제가 이미 있으니 바로 돌려볼 수 있다:

```bash
python -m harness.cli run --task examples/task.iterative_refinement.json --models gemini
```

- 라운드마다 생성자 1회 + 평가자 1회를 부르므로 **비용이 라운드 수만큼 는다.**
  상한은 `config.json`의 `max_refinement_rounds`(기본 3).
- 구독 CLI(claude/codex)를 생성자로 쓰면 라운드마다 구독 한도를 깎으니
  위처럼 `--models gemini`(종량제)를 권한다.
- 품질이 이미 기준을 넘으면 1라운드에 끝난다 — 그때 추가 비용은 평가자 1회뿐이라
  일종의 보험으로 생각하면 된다.
- 라운드별 기록은 run 폴더의 `refinement.json`에 남는다.

### `agentic_task` — 에이전트가 실제 파일을 만든다

앞의 셋과 성격이 다르다. 지금까지는 하네스가 "다음에 뭘 할지"를 정했지만,
여기서는 **에이전트(claude CLI)가 스스로 도구를 호출하며 진행하고** 하네스는
울타리와 기록을 맡는다. 결과물이 텍스트가 아니라 **실제 마크다운 파일 여러 개**
같은 형태여야 할 때 쓴다.

```json
{
  "task_id": "build-guide",
  "prompt": "리눅스 학습 자료를 주제별 마크다운 파일 3개로 작성해줘.",
  "constraints": ["team_pattern:agentic_task"]
}
```

이것도 예제(`examples/task.agentic.json`, task_id는 `agentic-demo`)로 바로
확인할 수 있다:

```bash
python -m harness.cli run --task examples/task.agentic.json
# → "사람 승인 대기 중"에서 멈춘다. 확인 후:
python -m harness.cli approve run-agentic-demo
```

**처음 쓰면 당황할 수 있는 두 가지**를 미리 알아둘 것:

1. **run이 한 번에 안 끝난다.** 되돌리기 어려운 작업이라 항상 사람 승인을 거치게
   돼 있다(`risk_level=high` 강제). 위처럼 `run` → 확인 → `approve` 2단계다.
2. **파일은 run 폴더 안에만 생긴다** —
   `_workspace/runs/<run_id>/artifacts/agent_workspace/`. 에이전트는 그 폴더
   밖의 파일을 읽지도 쓰지도 못하고, 명령 실행(Bash)·네트워크 도구도 막혀 있다.
   내 프로젝트가 망가질 걱정은 안 해도 된다.

에이전트가 턴마다 무슨 도구를 썼는지는 `agent_turns.json`에, 울타리가 막아낸
시도가 있으면 그것도 함께 기록된다. 상한은 `config.json`의 `max_agent_turns`
(기본 8). claude 구독 로그인이 필요하다(Gemini 키로는 이 패턴을 못 쓴다).

> 두 패턴 모두 `new_domain.py`로 도메인을 만들 때
> `--pattern iterative_refinement` / `--pattern agentic_task`를 주면
> `constraints`와 관련 설정을 알아서 넣어준다.

## 다음에 볼 것

- **전체 스펙이 궁금하면**: `docs/02_구현플랜/harness-implementation-plan-ko.md`
  (비개발자용 요약은 `harness-implementation-plan-summary-beginner-ko.md`)
- **코드 구조가 궁금하면**: `harness-mvp/README.md`(패턴 4종 비교표 포함).
  7절 두 패턴의 설계 배경과 알려진 한계는 `harness-mvp/docs/adr/`의
  `0006-iterative-refinement-pattern.md` / `0007-agentic-task-pattern.md`
- **cloud-ops처럼 Fetcher(실측 가격 조회)까지 쓰는 "무거운" 도메인을 만들고
  싶다면**: `harness-mvp/docs/adr/0005-domain-folder-architecture.md`의 설계
  결정을 참고해서 `harness-mvp/src/fetchers/`의 기존 Fetcher를 재사용하는
  방식으로 직접 스크립트를 작성할 것(`new_domain.py`는 Fetcher 없는 "가벼운"
  도메인 전용이라 이 패턴은 자동화 대상이 아님)
- **막히면**: `harness-new-machine-setup-guide-ko.md`의 "겪은 문제" 절 확인
