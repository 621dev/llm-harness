"""Deterministic Grader (Phase 2).

harness-implementation-plan-ko.md Section 8(Phase 2)을 구현한다. affaan-m/ECC의
"deterministic grader를 우선 사용하고, 주관적 품질 평가만 model judge/human review로
보완한다"는 원칙에 따라, 여기서는 규칙 기반 채점만 다룬다 (model judge/human review는
Phase 4 이후 safety/policy gate 쪽에서 다룰 영역).

채점 대상은 run_dir의 `final.md`다 — team_pattern이 fan_out_judge든
hierarchical_delegation이든 direct_call이든, 최종 산출물은 항상 final.md로
수렴하므로 grader는 패턴을 신경 쓰지 않는다("패턴 무관 run 단위 평가").
"""
from __future__ import annotations

from pathlib import Path

from harness.schemas import EvalCase, GradeResult, ObservationStatus


def grade(run_dir: Path, case: EvalCase, run_status: ObservationStatus) -> GradeResult:
    """run 하나를 채점한다.

    1. run_status가 "error"면 무조건 실패다 (min_candidates 미달, 체인 완전 실패,
       승인 반려, Safety 실패 등 — 이유는 이미 errors.json/safety.md에 있으므로 여기서
       다시 확인하지 않는다).
    2. final.md가 없으면 실패다 (예: risk_level=high라 승인 대기 중 멈춘 경우).
    3. final.md 내용에 required_phrases가 전부 있고 forbidden_phrases가 하나도 없어야
       통과다.
    """
    checked = case.required_phrases + case.forbidden_phrases

    if run_status == "error":
        return GradeResult(passed=False, reason=f"run이 실패로 종료됨 (status={run_status})", checked_phrases=checked)

    final_path = run_dir / "final.md"
    if not final_path.exists():
        return GradeResult(passed=False, reason="final.md가 없음 (승인 대기 등으로 실행이 끝까지 안 감)", checked_phrases=checked)

    content = final_path.read_text(encoding="utf-8")
    missing = [phrase for phrase in case.required_phrases if phrase not in content]
    found_forbidden = [phrase for phrase in case.forbidden_phrases if phrase in content]

    if missing or found_forbidden:
        reasons = []
        if missing:
            reasons.append(f"필수 문구 누락: {missing}")
        if found_forbidden:
            reasons.append(f"금지 문구 발견: {found_forbidden}")
        return GradeResult(passed=False, reason="; ".join(reasons), checked_phrases=checked)

    return GradeResult(passed=True, reason="run 성공 + 필수/금지 문구 조건 만족", checked_phrases=checked)
