"""측정 실행 래퍼 — .env의 키를 프로세스 환경으로만 주입한다(화면에 절대 출력 안 함).

harness-mvp/ 에서 실행할 것. 인자는 그대로 measure_pattern_value.py로 넘어간다.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

env = dict(os.environ)
env_file = Path(".env")
loaded: list[str] = []
if env_file.is_file():
    for raw in env_file.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        env[key] = value.strip().strip('"').strip("'")
        loaded.append(key)

# 키 **이름만** 찍는다. 값은 어디에도 출력하지 않는다.
print(f"[env] .env에서 주입한 변수: {loaded or '(없음)'}", flush=True)

env["PYTHONPATH"] = "src"
env["PYTHONIOENCODING"] = "utf-8"
command = [sys.executable, "scripts/measure_pattern_value.py", *sys.argv[1:]]
print(f"[run] {' '.join(command[1:])}", flush=True)
sys.exit(subprocess.run(command, env=env).returncode)
