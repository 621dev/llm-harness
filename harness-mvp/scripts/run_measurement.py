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
# 자식 프로세스의 출력을 줄 단위로 흘린다 (2026-07-29 추가). 이게 없으면 측정이
# 10분 넘게 도는 동안 `[1/3] chain 실행 중...` 같은 진행 표시가 **하나도 안 보인다** —
# 파이프로 리다이렉트되면 stdout이 블록 버퍼링되고, 측정 스크립트가 모듈 레벨에서
# `sys.stdout`을 TextIOWrapper로 감싸는 것도 같은 결과다. 진행이 안 보이면 "도는 중"과
# "멈춤"을 구분할 수 없어서, 실제로 디스크의 run 디렉터리를 세어 진행을 판정해야 했다.
# 래퍼에 `-u`를 주는 것으로는 안 된다 — 자식은 그 플래그를 물려받지 않는다.
env["PYTHONUNBUFFERED"] = "1"
command = [sys.executable, "-u", "scripts/measure_pattern_value.py", *sys.argv[1:]]
print(f"[run] {' '.join(command[1:])}", flush=True)
sys.exit(subprocess.run(command, env=env).returncode)
