# 새 머신 환경설정 가이드

작성일: 2026-07-14 (완전히 새 머신(Python/Node.js/claude·codex CLI 전부
미설치 상태)에서 실제로 처음부터 세팅하며 검증한 절차)

용도: 다른 머신(또는 완전 초기화된 환경)에서 이 프로젝트를 처음 세팅할 때
그대로 따라 할 수 있는 순서. 각 단계는 실제로 한 번 이상 실행해서 확인했다.
막히면 각 절의 "겪은 문제" 항목을 먼저 볼 것.

## 0. 전제

- Windows(PowerShell 사용). WSL/Linux/Mac은 명령어만 다르고 순서는 동일.
- GitHub 저장소(`621dev/multi-llm-harness`, private)에 접근 권한이 있어야 함.
- Claude(claude.ai Pro 이상 구독)와 OpenAI(ChatGPT 구독, Codex 사용 가능한 플랜)
  계정, Gemini API 키(Google AI Studio), 필요시 NCP 계정(NCP_ACCESS_KEY/
  NCP_SECRET_KEY)이 필요.

## 1. 저장소 클론

```powershell
git clone https://github.com/621dev/multi-llm-harness.git
```

## 2. Python 설치

```powershell
winget install --id Python.Python.3.12 -e --source winget --accept-package-agreements --accept-source-agreements
```

문서 기준 검증 버전은 3.12.1/3.12.10 계열(`harness-mvp/README.md`의
"Local 환경" 절 참고). 설치 후 **새 터미널 창**을 열어야 PATH가 반영된다(같은
창에서 계속 쓰면 `python`이 "찾을 수 없음"으로 나온다 — 아래 5절 참고).

## 3. Node.js 설치 (claude/codex CLI용)

```powershell
winget install --id OpenJS.NodeJS.LTS -e --source winget --accept-package-agreements --accept-source-agreements
```

## 4. claude/codex CLI 설치 + 로그인

```powershell
npm install -g @anthropic-ai/claude-code
npm install -g @openai/codex
```

### claude 로그인

```powershell
claude auth login
```

브라우저가 자동으로 안 열리면 터미널에 뜨는 URL을 직접 열어 로그인하고,
콜백 페이지에 나오는 코드를 터미널에 붙여넣는다(`--claudeai`가 기본값,
Claude 구독 계정 기준).

### codex 로그인

```powershell
codex login
```

기본은 `http://localhost:1455`로 로컬 콜백 서버를 띄우는 방식이라 브라우저와
같은 머신에서 실행해야 정상 동작한다. **원격/헤드리스 환경**(에이전트가 대신
실행하는 등 브라우저와 분리된 세션)에서는 이 방식이 안 먹을 수 있으니 대신:

```powershell
codex login --device-auth
```

URL과 1회용 코드(예: `5363-Q4JSO`, 15분 내 만료)가 출력되고, 사용자가 그
URL을 아무 브라우저에서나 열어 코드를 입력하면 로그인이 완료된다(로컬 포트
연결이 필요 없어 원격 환경에서도 동작 확인됨).

### 로그인 상태 확인 (값 노출 없이)

```powershell
claude auth status
codex login status
```

## 5. 겪은 문제 — PATH가 새 터미널 창에서도 안 잡힐 때

npm 전역 설치 경로(`%APPDATA%\npm`)가 사용자 PATH에는 등록되지만, **이미 열려
있던 터미널 창**은 그 PATH 변경을 못 읽는다 — 반드시 완전히 새 창을 열어야
한다. 새 창에서도 안 되면:

```powershell
$env:PATH -split ';' | Select-String npm      # 이 줄에 %APPDATA%\npm이 나오는지 확인
Test-Path "$env:APPDATA\npm\claude.ps1"        # 파일이 실제로 있는지 확인
```

파일이 없다고 나오면(설치는 성공했다고 나왔는데도) 설치를 실행한 셸과 확인하는
셸이 서로 다른 실행 환경일 가능성이 있다(예: 에이전트 도구가 격리된 셸에서
설치를 실행한 경우) — 같은 사용자 터미널에서 설치 명령을 다시 실행해서
확인할 것.

## 6. harness-mvp 의존성 설치

```powershell
cd harness-mvp
pip install -e .[dev]
python -m pytest tests/ -v
```

전부 통과해야 정상(2026-07-27 기준 294개 — 기능이 늘면 개수도 늘어나니 "전부
통과"가 기준이고 숫자 자체는 참고값이다. 정확한 최신 개수는
`harness-mvp/README.md`의 테스트 표 참고).

## 7. API 키 설정 — `harness-mvp/.env`

프로젝트 전체가 공유하는 단일 파일이다(도메인 폴더마다 따로 안 둠, 2026-07-14
사용자 요청으로 `.env.ncp`/`.env.gemini` 여러 파일에서 통합됨). `harness-mvp/`
디렉터리에 `.env` 파일을 만들고:

```
GEMINI_API_KEY=여기에_실제_키
NCP_ACCESS_KEY=여기에_실제_키
NCP_SECRET_KEY=여기에_실제_키
```

`.gitignore`에 `*.env`/`.env.*` 패턴이 있어 커밋 대상에서 자동 제외된다.
NCP 키가 없으면 `domains/cloud-ops`의 NCP 관련 Fetcher만 실패하고(다른 부분은
정상 진행), AWS 쪽과 컴퓨트 후보 생성/judge는 영향받지 않는다.

### 겪은 사고 — 키 존재 여부 확인 시 셸 문법 실수로 값이 노출됨

```bash
# 절대 이렇게 하지 말 것 — VAR가 설정돼 있으면 :- 가 실제 값을 그대로 반환한다
echo "${VAR:+yes}${VAR:-no}"

# 존재 여부만 확인하려면 이것만
echo "${VAR:+yes}"
```

`${VAR:-no}`는 VAR가 **비어있거나 unset일 때만** "no"를 반환하고, 값이 있으면
그 값 자체를 반환한다 — "yes/no"만 나올 거라고 착각하기 쉬운 함정이다.

## 8. 동작 확인

```powershell
cd domains/cloud-ops
python run_estimate.py examples/spec.simple-2-server.json --dry-run
```

AWS/NCP 실측 가격이 조회되고 prompt가 조립되면 세팅 완료. 실제 LLM까지
호출해보려면 `--dry-run` 없이 실행(비용 발생, claude 구독 1회 소모).

## 9. 참고 — 에이전트(Claude Code 등)가 대신 세팅하는 경우

이 프로젝트를 사람이 아니라 Claude Code 같은 에이전트가 대신 세팅해줄 때
2026-07-14에 실제로 겪은 것:

- 에이전트의 도구 실행 환경이 사용자가 보는 실제 터미널과 분리된 셸일 수
  있다(설치는 성공했다고 나오는데 사용자 터미널에서는 안 보이는 현상). 이
  경우 설치 자체를 사용자의 실제 터미널에서 다시 실행하거나, 에이전트가 여러
  종류의 도구(예: Bash와 PowerShell)를 갖고 있다면 그중 실제 시스템에
  반영되는 쪽으로 재실행하면 해결된다.
- `claude auth login`을 에이전트 환경에서 실행했을 때, 브라우저 상호작용
  없이도 로그인이 즉시 성공한 사례가 있었다 — 에이전트 자신이 이미 같은
  계정으로 인증된 세션 위에서 동작 중이라 자격증명을 재사용한 것으로
  추정된다(로그인된 계정 이메일이 실제 사용자 계정과 일치하는지로 문제
  없음을 확인했다). 반면 `codex login`(OpenAI 계정)은 별도 계정 체계라 이런
  재사용이 없었고, 실제 사용자가 URL을 열어 코드를 입력하는 과정이 필요했다.
