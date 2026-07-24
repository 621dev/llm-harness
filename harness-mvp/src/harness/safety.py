"""Safety Evaluator: 체크리스트 기반 안전성 점검 (패턴 공통, Step 7).

harness-implementation-plan-ko.md Section 6("공통 Safety fail" 복구), Section 7 Step 7을
구현한다. 진짜 안전 분류 모델이 아니라, 비밀정보/프롬프트 인젝션/고위험 키워드를
정규식·문자열 매칭으로 스캔하는 규칙 기반 검사다. 두 패턴(fan_out_judge,
hierarchical_delegation) 모두 최종 출력에 대해 동일하게 호출된다.
"""
from __future__ import annotations

import re

from .schemas import Observation

_SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9]{16,}"),  # OpenAI 스타일 API 키
    re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS access key
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]
_INJECTION_PHRASES = (
    "ignore previous instructions",
    "이전 지시를 무시",
    "시스템 프롬프트를 무시",
)
_HIGH_RISK_KEYWORDS = ("주민등록번호", "비밀번호를 알려줘", "카드번호")


def check(content: str) -> Observation:
    """content를 스캔한다. 문제가 없으면 status="success", 있으면 "error"를 반환한다."""
    findings: list[str] = []

    for pattern in _SECRET_PATTERNS:
        if pattern.search(content):
            findings.append(f"비밀정보로 보이는 패턴 발견 ({pattern.pattern})")

    lowered = content.lower()
    for phrase in _INJECTION_PHRASES:
        if phrase.lower() in lowered:
            findings.append(f"프롬프트 인젝션 의심 문구 발견 ({phrase})")

    for keyword in _HIGH_RISK_KEYWORDS:
        if keyword in content:
            findings.append(f"고위험 키워드 발견 ({keyword})")

    if findings:
        return Observation(
            status="error",
            summary="Safety 점검 실패: " + "; ".join(findings),
            artifacts=[],
            next_actions=["ask_user"],
        )

    return Observation(
        status="success",
        summary="Safety 점검 통과 (비밀정보/인젝션/고위험 키워드 없음)",
        artifacts=[],
        next_actions=["continue"],
    )
