# 시작 가이드 (Claude Desktop 전용)

작성일: 2026-07-24

용도: 터미널을 직접 안 치고, **Claude Desktop과 대화만으로** 이 하네스를
설치·실행하고 싶은 사람을 위한 가이드. `harness-getting-started-guide-ko.md`
(CLI 버전)와 순서/목표는 같고, 각 단계를 사람이 명령어로 치는 대신 Claude
Desktop에게 자연어로 시켜서 대신 실행하게 만드는 방식만 다르다.

**중요한 차이 — 이 문서는 CLI 가이드와 검증 방식이 다르다**: CLI
가이드는 이 세션에서 각 명령어를 실제로 실행해서 확인했다. 이 문서는 Claude
Desktop을 직접 조작할 방법이 없어(별도 데스크톱 앱, 이 세션의 도구 범위
밖) **공식 문서([modelcontextprotocol.io](https://modelcontextprotocol.io/docs/develop/connect-local-servers))를
조사해서 작성했고 실제 클릭까지 검증하지 못했다.** Claude Desktop의 메뉴/버튼
위치는 업데이트로 바뀔 수 있으니, 아래 설명과 실제 화면이 다르면 Claude
Desktop 자체의 최신 안내를 우선한다.

---

## 0. 핵심 개념 — Claude Desktop이 왜 기본으로는 파일/터미널을 못 만지는가

Claude Desktop은 기본 설치 상태로는 여러분 컴퓨터의 파일이나 터미널에 전혀
접근할 수 없다. 접근하려면 **MCP(Model Context Protocol) 서버**를 추가로
연결해야 한다 — Claude Desktop이 로컬에서 실행하는 작은 프로그램으로, "이
폴더는 읽고 써도 됨", "이 명령은 실행해도 됨" 같은 권한을 딱 필요한 만큼만
내준다. 이 가이드에서는 두 종류가 필요하다.

| 필요한 기능 | 서버 | 성격 |
| --- | --- | --- |
| 파일 읽기/쓰기(코드 보기, 설정 파일 수정 등) | **Filesystem MCP 서버** | Anthropic 공식 |
| 명령어 실행(`pip install`, `pytest`, `harness.cli run` 등) | **터미널/셸 실행 MCP 서버** | 서드파티(제작사 다양) |

**둘 다 필요하다** — 공식 Filesystem 서버는 파일을 읽고 쓸 수만 있지 명령어를
실행하지는 못한다(공식 문서에서 확인, "Reading file contents", "Creating new
files", "Moving/renaming", "Searching"만 나열됨). `pip install`이나
`pytest tests/`처럼 실제로 뭔가를 "실행"하려면 터미널 접근을 주는 서버가
따로 필요하다. 이건 Anthropic이 공식으로 배포하는 게 아니라 커뮤니티에서
만든 서버(예: Desktop Commander MCP 등 여러 종류가 있음)라 **설치 전에
직접 신뢰 여부를 판단할 것** — 셸 명령 실행 권한은 파일 접근보다 훨씬 강력한
권한이다.

## 1. 준비물

- Claude Desktop 설치 (claude.ai/download)
- Node.js 설치(대부분의 MCP 서버가 `npx`로 실행됨) — 터미널에서 `node
  --version`으로 확인, 없으면 `harness-new-machine-setup-guide-ko.md` 3절 참고
- 이 저장소를 이미 어딘가에 clone해뒀을 것(직접 하거나, 2단계에서 터미널
  MCP를 연결한 뒤 Claude Desktop에게 시켜도 됨)

## 2. Filesystem MCP 서버 연결 (파일 읽기/쓰기)

1. Claude Desktop 메뉴(윈도우 창 안이 아니라 시스템 메뉴/트레이) → **Settings**
2. **Developer** 탭 → **Edit Config** 클릭 — 아래 파일이 없으면 새로 생성됨,
   있으면 열림:
   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`
3. 저장소를 clone해둔 경로를 넣어 아래처럼 작성(Windows 예시, 실제 경로로
   교체):

   ```json
   {
     "mcpServers": {
       "filesystem": {
         "command": "npx",
         "args": [
           "-y",
           "@modelcontextprotocol/server-filesystem",
           "C:\\Users\\사용자명\\multi-llm-harness"
         ]
       }
     }
   }
   ```
4. 저장 후 **Claude Desktop 완전히 종료 → 재시작**(창만 새로고침하면 안 되고
   완전 종료 필요).
5. 재시작 후 대화창 왼쪽 아래 "+" 아이콘 → Connectors → Manage connectors에서
   `filesystem`이 보이면 연결 성공.

## 3. 터미널 실행 MCP 서버 연결 (명령어 실행)

파일만 읽고 쓸 수 있으면 `pip install`/`pytest`/`harness.cli run` 같은 실제
실행은 안 된다. 터미널 명령을 실행할 수 있는 MCP 서버를 하나 더 골라 같은
`claude_desktop_config.json`의 `mcpServers` 안에 추가한다(어떤 서버를 쓸지는
직접 검색해서 신뢰할 수 있는 걸 선택할 것 — 이 문서에서 특정 서드파티 서버를
추천하지 않는다. "MCP terminal server"/"MCP shell server" 등으로 검색하면
여러 선택지가 나온다).

**설치 전 확인할 것**: 이 서버는 여러분 컴퓨터에서 임의의 명령을 실행할 수
있는 권한을 갖는다 — 실행 전마다 Claude Desktop이 승인을 요청하는 방식인지,
어떤 명령까지 허용/차단할 수 있는지 문서를 먼저 읽을 것. `filesystem`
서버처럼 Anthropic 공식이 아니므로 제작자/사용자 평판을 직접 확인하는 게
좋다.

## 4. Claude Desktop에게 실제로 시켜보기

MCP 연결이 끝나면, 터미널 가이드의 각 단계를 그대로 자연어 요청으로 바꿔서
말하면 된다. 아래는 그대로 복사해서 써볼 수 있는 대화 예시(실제 CLI
가이드의 단계와 1:1 대응):

1. **설치 확인** (비용 없음)
   > "`C:\Users\사용자명\multi-llm-harness\harness-mvp` 폴더에서
   > `pip install -e .[dev]`를 실행하고, `python -m pytest tests/ -v`로
   > 테스트가 전부 통과하는지 확인해줘."

2. **자격증명 없이 도메인 로직만 확인** (비용 없음)
   > "같은 폴더에서 `scripts/new_domain.py`를 실행해서 `my-first-domain`이라는
   > 도메인을 만들어줘. task-id는 hello, prompt는 '경쟁사 A/B/C의 가격 정책을
   > 리서치해줘. 그 다음 설계 리뷰를 진행해줘.', pattern은
   > hierarchical_delegation으로."

3. **실제 LLM 첫 실행** (Gemini API 키 필요 — 발급 방법은 CLI 가이드 4절 참고)
   > "GEMINI_API_KEY 환경변수를 [내 키 값]으로 설정하고,
   > `harness-mvp/config.json`을 열어서 candidate_models/judge_model/
   > delegation_model을 전부 gemini로 바꾼 뒤,
   > `python -m harness.cli run --task examples/task.delegation.json`을
   > 실행해줘."

   **주의**: 이 요청은 실제 API를 호출해 비용이 발생한다. Claude Desktop이
   실행 전 승인을 요청하면 내용을 확인하고 승인할 것 — API 키 값을 대화
   내용에 그대로 남기고 싶지 않다면, 터미널 MCP 서버가 환경변수 파일(`.env`
   등)을 직접 못 읽게 하고 시스템 환경변수로 미리 등록해둔 뒤 "이미 설정된
   GEMINI_API_KEY로 실행해줘"라고 요청하는 편이 낫다.

4. **내 도메인 만들어서 실행**
   > "`domains/my-first-domain` 폴더 안에서
   > `python -m harness.cli run --task examples/task.hello.json`을 실행해줘."

5. **지금까지 실행한 것 한눈에 보기**
   > "`harness-mvp` 폴더에서 `python -m harness.cli status --all-domains
   > --output _workspace/overview.html`을 실행하고, 결과 파일을 열어서
   > 요약해줘."

## 자격증명 보안 관련 참고

API 키나 비밀번호를 Claude Desktop과의 대화창에 직접 타이핑하는 건 피할 것
— 대화 기록에 그대로 남는다. 가능하면:
- API 키는 시스템 환경변수로 미리 등록해두고 "이미 설정된 값으로 실행해줘"라고
  요청
- 혹은 `.env` 파일에 미리 적어두고 "`.env` 파일의 값을 읽어서 실행해줘"라고
  요청(단, 이 경우 `.env` 파일 내용을 Claude Desktop이 화면에 그대로 출력하지
  않도록 "값을 출력하지 말고 실행에만 써줘"라고 명시할 것)

## 다음에 볼 것

- CLI로도 똑같이 해보고 싶다면: `harness-getting-started-guide-ko.md`
- 전체 스펙: `docs/02_구현플랜/harness-implementation-plan-ko.md`
- 코드 구조: `harness-mvp/README.md`

## 참고 자료

이 가이드의 MCP 설정 부분은 아래 자료를 조사해 작성(2026-07-24 시점,
Claude Desktop 자체를 직접 조작해 검증하지는 못함):

- [Connect to local MCP servers – Model Context Protocol 공식 문서](https://modelcontextprotocol.io/docs/develop/connect-local-servers)
- [modelcontextprotocol/servers – 공식 레퍼런스 서버 목록](https://github.com/modelcontextprotocol/servers)
