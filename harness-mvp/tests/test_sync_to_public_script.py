"""공개 저장소 동기화 스크립트 테스트 (2026-07-29, 실제 사고로 추가).

**사고 경위**: 공개 저장소(`621dev/llm-harness`)에 `.gitignore`가 없었다. 미러에서
`pytest`를 돌려 "미러에서도 테스트가 통과하는지" 검증한 뒤 `git add -A`로 커밋했더니
`__pycache__`의 **.pyc 62개가 그대로 커밋됐다**(커밋 987656b). 비밀정보는 아니지만
(이미 공개된 소스의 바이트코드) 저장소 위생 문제이고, 검증을 계속 하려면 구조적으로
막아야 한다.

**본 저장소 `.gitignore`를 그대로 복사하지 않는다**: 그 파일 주석에는 제외 사유로
도메인 업무 관련 문구와 `domains/...` 경로가 적혀 있어, 그대로 복사하면 공개 대상이
아닌 표현을 .gitignore로 공개하게 된다. 공개 저장소는 생성물이므로 .gitignore도 생성한다.

여기서 고정하는 것:
- 동기화하면 공개 저장소에 `.gitignore`가 **항상** 생긴다
- 그 내용이 `__pycache__`를 무시한다(사고 재발 방지의 핵심)
- 본 저장소 .gitignore를 **복사한 것이 아니다**(주석에 있는 도메인 관련 표현 차단)
- 화이트리스트 밖 경로(`domains/`, `docs/03_진행상황/`)는 여전히 복사되지 않는다
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

# 이 스크립트는 **저장소 루트** `scripts/`에 있고, 공개 미러의 화이트리스트에는
# 들어 있지 않다(미러는 `harness-mvp/`만 가져간다). 그래서 공개 미러에서 테스트를
# 돌리면 import가 실패한다 — 없으면 건너뛴다. 미러에서 pytest를 돌려 검증하는
# 관행 자체가 이 사고의 발단이었으므로, 그 검증을 계속할 수 있게 하는 게 중요하다.
_SCRIPT_DIR = Path(__file__).resolve().parents[2] / "scripts"
_AVAILABLE = (_SCRIPT_DIR / "sync_to_public.py").is_file()
if _AVAILABLE:
    sys.path.insert(0, str(_SCRIPT_DIR))
    import sync_to_public


def git(*args: str, cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


@unittest.skipUnless(_AVAILABLE, "scripts/sync_to_public.py는 공개 미러에 포함되지 않는다")
class SyncToPublicTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="sync-public-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

        # 원본 저장소 흉내: 화이트리스트 안/밖 파일을 각각 커밋해둔다
        self.source = self.tmp_dir / "source"
        (self.source / "harness-mvp" / "src").mkdir(parents=True)
        (self.source / "domains" / "secret-domain").mkdir(parents=True)
        (self.source / "harness-mvp" / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
        (self.source / "CLAUDE.md").write_text("# 규칙\n", encoding="utf-8")
        (self.source / "domains" / "secret-domain" / "estimate.md").write_text(
            "공개되면 안 되는 도메인 업무 내용\n", encoding="utf-8"
        )
        git("init", "-q", cwd=self.source)
        git("config", "user.email", "t@example.com", cwd=self.source)
        git("config", "user.name", "t", cwd=self.source)
        git("add", "-A", cwd=self.source)
        git("commit", "-qm", "init", cwd=self.source)

        self.dest = self.tmp_dir / "dest"
        self.dest.mkdir()
        git("init", "-q", cwd=self.dest)

    def sync(self) -> list[Path]:
        return sync_to_public.sync(self.dest, repo_root=self.source)

    def test_gitignore_is_created(self) -> None:
        """공개 저장소에 .gitignore가 없어서 .pyc가 커밋된 사고의 직접 재발 방지."""
        self.sync()

        self.assertTrue((self.dest / ".gitignore").is_file())

    def test_gitignore_ignores_pycache(self) -> None:
        """미러에서 pytest를 돌려도 .pyc가 커밋 대상에 안 잡혀야 한다."""
        self.sync()
        # 실제로 git이 무시하는지까지 확인한다 — 파일 내용 문자열 검사만으로는
        # "규칙이 실제로 먹히는가"를 보증하지 못한다.
        cache_dir = self.dest / "harness-mvp" / "src" / "__pycache__"
        cache_dir.mkdir(parents=True)
        (cache_dir / "app.cpython-312.pyc").write_bytes(b"\x00fake bytecode")

        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=self.dest,
            capture_output=True,
            text=True,
            check=True,
        )

        self.assertNotIn("pyc", result.stdout)

    def test_generated_gitignore_is_not_a_copy_of_the_source(self) -> None:
        """본 저장소 .gitignore를 복사하면 그 주석의 도메인 관련 표현까지 공개된다.

        리터럴 단어를 여기 적어두면 이 테스트 파일 자체가 공개 저장소로 나가면서
        같은 문제를 만든다(실제로 한 번 그랬다). 그래서 "복사본이 아니다 +
        도메인 경로가 없다"는 구조적 불변식으로 검사한다.
        """
        source_gitignore = self.source / ".gitignore"
        source_gitignore.write_text(
            "__pycache__/\n# 제외 사유: <도메인 고유 표현>\ndomains/x/references/\n",
            encoding="utf-8",
        )
        self.sync()

        text = (self.dest / ".gitignore").read_text(encoding="utf-8")

        self.assertNotEqual(text, source_gitignore.read_text(encoding="utf-8"))
        self.assertNotIn("domains/", text)

    def test_whitelist_still_excludes_domains(self) -> None:
        """.gitignore 추가와 무관하게 기존 제외 규칙이 유지되는지(회귀 방지)."""
        self.sync()

        self.assertTrue((self.dest / "harness-mvp" / "src" / "app.py").is_file())
        self.assertFalse((self.dest / "domains").exists())

    def test_gitignore_survives_repeated_sync(self) -> None:
        """sync는 .git 외 전부를 지우고 다시 쓴다 — 두 번 돌려도 남아야 한다."""
        self.sync()
        (self.dest / ".gitignore").write_text("손으로 망가뜨린 내용\n", encoding="utf-8")

        self.sync()

        self.assertIn("__pycache__/", (self.dest / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
