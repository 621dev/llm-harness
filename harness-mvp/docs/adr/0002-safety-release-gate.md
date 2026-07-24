# ADR 0002: Safety 실패는 즉시 차단이 아니라 사람 검토 대기로 승격한다

- 상태: 확정
- 관련 플랜: `harness-implementation-plan-ko.md` Section 6, Section 12.2, Phase 4

## 배경

Phase 1~3까지 `safety.check()`가 실패하면 run은 그 자리에서 `status="error"`로
끝났다 — final.md에는 "(보류) Safety 점검 실패로 최종 출력을 보류함" 같은 placeholder만
남고, 실제로 생성됐던 내용은 어디에도 보존되지 않았다. Phase 4("safety.py를 release
gate로 승격, human review 큐 연결")에서 이 방식을 다시 검토했다.

## 결정

Safety 체크 실패를 최종 결정이 아니라 "사람 검토가 필요하다"는 신호로 바꾼다.

- 실패한 내용은 `pending_review_content.md`에 그대로 보관하고, `safety_review.json`을
  `"pending"` 상태로 써서 run을 멈춘다(`final.md`는 이 시점에 생성되지 않는다).
- 이미 있던 승인 체크포인트(`Approval` 스키마, `pending`/`approved`/`rejected` 상태)를
  그대로 재사용한다 — 새 스키마를 만들지 않는다. `approved` = 사람이 오탐으로 판단해
  원래 내용을 공개(release), `rejected` = 위험하다고 확정해 계속 보류(block).
- `orchestrator.resolve_safety_review(run_id, decision)`이 검토 결과를 받아 이어가고,
  `orchestrator.list_safety_review_queue()`가 검토 대기 중인 run 목록을 보여준다.
  `cli.py`의 `safety-queue`/`safety-approve`/`safety-reject` 명령이 이걸 다룬다.

## 이유

- 규칙 기반 Safety 스캐너는 오탐이 있을 수밖에 없다(예: "주민등록번호"라는 단어가
  나왔다고 실제로 개인정보가 유출된 건 아닐 수 있다). 오탐을 즉시 영구 차단해버리면
  복구할 방법이 없다 — 원본 내용 자체가 안 남기 때문이다.
- jikime/harness-lab에서 가져온 "청사진 제시 → 사용자 승인" 원칙(Section 12.2)과
  같은 결의 문제다: 기계 판정이 의심스러울 때는 사람이 최종 결정권을 갖는 게 안전하다.
- 이미 구현해둔 승인 체크포인트와 상태 모델(pending/approved/rejected)이 정확히
  같은 모양이라, 새 스키마를 만드는 대신 재사용해서 일관성을 유지하고 코드량도 줄였다.

## 영향

- `schemas.py`: 스키마 변경 없음(`Approval` 재사용)
- `orchestrator.py`: `_finalize()`가 Safety 실패 시 `_enter_safety_review()`로 위임,
  `_finalize_partial_chain()`은 자체 Safety 처리를 제거하고 `_finalize()`에 위임(중복
  제거). `resolve_safety_review()`/`list_safety_review_queue()` 추가
- Run Store: `safety_review.json`, `pending_review_content.md` 파일 추가
- `cli.py`: `safety-queue`/`safety-approve`/`safety-reject` 명령 추가
- 기존 테스트 영향: `test_step9_integration.py`의
  `test_partial_promotion_still_runs_safety_check`가 "즉시 차단됨"을 검증하던 걸
  "검토 대기 상태로 멈춤"을 검증하도록 갱신됨(Safety 체크가 실행된다는 핵심 취지는
  그대로 유지)
