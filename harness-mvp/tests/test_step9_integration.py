"""Step 9 테스트: orchestrator 통합 테스트 (두 패턴 모두 + 보완 장치).

harness-implementation-plan-ko.md Section 7 Step 9, Section 11(DoD)을 검증한다.
Step 0의 낡은 orchestrator API(수동으로 Plan을 넘기던 방식)를 대체하는 최종
통합 테스트로, 실제 CLI가 쓰는 것과 같은 provider 구성으로 orchestrator.run()/resume()을
끝까지 실행해서 DoD에 명시된 파일/재현성/복구 전략/보완 장치를 검증한다.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import orchestrator, run_store  # noqa: E402
from harness.schemas import ProviderConfig, TaskInput  # noqa: E402
from providers.fallback_provider import QuotaFallbackProvider  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def make_task(task_id: str, prompt: str, constraints: list[str] | None = None) -> TaskInput:
    return TaskInput(task_id=task_id, prompt=prompt, constraints=constraints or [])


def fan_out_providers(*, fail_times: dict[str, int] | None = None) -> dict[str, MockProvider]:
    fail_times = fail_times or {}
    specs = [("model-a", "concise"), ("model-b", "detailed"), ("model-c", "creative")]
    providers = {
        provider_id: MockProvider(
            ProviderConfig(provider_id=provider_id, model_id=provider_id),
            profile=profile,
            fail_times=fail_times.get(provider_id, 0),
        )
        for provider_id, profile in specs
    }
    # ADR 0004: fan_out_judge는 judge용 provider도 필요하다.
    providers[orchestrator.JUDGE_PROVIDER_KEY] = MockProvider(
        ProviderConfig(provider_id="judge", model_id="judge-mock"), profile="judge"
    )
    return providers


def mixed_auth_fan_out_providers() -> dict[str, MockProvider]:
    """구독(cli_subscription) provider 2개 + api_key provider 1개 — 구독 한도
    보호 로직(orchestrator._limit_subscription_candidates, Section 9) 검증용."""
    providers = {
        "model-a": MockProvider(
            ProviderConfig(provider_id="model-a", model_id="model-a", auth_mode="cli_subscription"),
            profile="concise",
        ),
        "model-b": MockProvider(
            ProviderConfig(provider_id="model-b", model_id="model-b", auth_mode="cli_subscription"),
            profile="detailed",
        ),
        "model-c": MockProvider(
            ProviderConfig(provider_id="model-c", model_id="model-c", auth_mode="api_key"), profile="creative"
        ),
    }
    providers[orchestrator.JUDGE_PROVIDER_KEY] = MockProvider(
        ProviderConfig(provider_id="judge", model_id="judge-mock"), profile="judge"
    )
    return providers


def all_subscription_fan_out_providers() -> dict[str, MockProvider]:
    """구독 provider만 2개 — 한도 보호를 그대로 적용하면 1개만 남아 MIN_CANDIDATES(2)
    미만이 되는 경우, 보호 로직이 스스로 물러서서 원래대로 2개 다 쓰는지 확인용."""
    providers = {
        "model-a": MockProvider(
            ProviderConfig(provider_id="model-a", model_id="model-a", auth_mode="cli_subscription"),
            profile="concise",
        ),
        "model-b": MockProvider(
            ProviderConfig(provider_id="model-b", model_id="model-b", auth_mode="cli_subscription"),
            profile="detailed",
        ),
    }
    providers[orchestrator.JUDGE_PROVIDER_KEY] = MockProvider(
        ProviderConfig(provider_id="judge", model_id="judge-mock"), profile="judge"
    )
    return providers


def delegation_providers(
    *, fail_times: dict[str, int] | None = None, workers: int = 2
) -> dict[str, MockProvider]:
    """매니저-워커 위임용 provider 묶음 (ADR 0014로 역할 provider가 워커로 바뀌었다).

    워커는 `__worker__:` 접두사로 **명시 등록해야** 한다 — orchestrator가 그 접두사만
    보고 워커를 고른다(후보 목록의 claude가 워커로 새어 들어오는 걸 막기 위함).
    """
    fail_times = fail_times or {}
    providers: dict[str, MockProvider] = {
        orchestrator.MANAGER_PROVIDER_KEY: MockProvider(
            ProviderConfig(provider_id="manager", model_id="manager-mock"),
            profile="manager",
            fail_times=fail_times.get("manager", 0),
        )
    }
    for index in range(1, workers + 1):
        name = f"worker{index}"
        key = f"{orchestrator.WORKER_PROVIDER_PREFIX}:{name}"
        providers[key] = MockProvider(
            ProviderConfig(provider_id=key, model_id=f"{name}-mock"),
            profile="detailed",
            fail_times=fail_times.get(name, 0),
        )
    return providers


class FanOutJudgeIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_full_run_creates_all_dod_files(self) -> None:
        task = make_task("fan-out-demo", "이 설계안을 검토해줘: 마이크로서비스로 갈지 모놀리식으로 갈지 비교해줘")

        observation = orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-fan-out-demo"
        self.assertEqual(observation.status, "success")
        # run_meta.json(2026-07-16, live_status.py)은 live 진행상황 조회용 pid/시작
        # 시각 기록 — 모든 run에 항상 있어야 한다.
        for name in ("input.json", "plan.md", "plan.json", "fitness_check.json", "judging.json", "final.md",
                     "safety.md", "metrics.json", "errors.json", "run_meta.json"):
            self.assertTrue((run_dir / name).exists(), f"{name} 이 없음")
        for model_id in ("model-a", "model-b", "model-c"):
            self.assertTrue((run_dir / "artifacts" / "candidates" / f"{model_id}.md").exists())
        self.assertFalse((run_dir / "approval.json").exists())  # risk_level=high가 아니므로 생성 안 됨
        self.assertEqual(run_store.read_json(run_dir, "errors.json"), [])
        self.assertTrue(run_store.read_json(run_dir, "fitness_check.json")["passed"])

    def test_one_candidate_failure_still_succeeds_and_is_logged(self) -> None:
        task = make_task("fan-out-partial-fail", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘")
        providers = fan_out_providers(fail_times={"model-b": 2})  # 재시도까지 다 실패(영구 실패)

        observation = orchestrator.run(task, providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-fan-out-partial-fail"
        self.assertEqual(observation.status, "warning")  # 성공했지만 경고가 있음
        self.assertTrue((run_dir / "judging.json").exists())
        self.assertTrue((run_dir / "final.md").exists())
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertEqual(len(errors), 1)
        self.assertIn("model-b", errors[0]["message"] + errors[0]["stage"])

    def test_cap_sees_subscription_through_the_quota_fallback_wrapper(self) -> None:
        """**구독 상한이 통째로 무효였던 버그의 회귀 방지** (2026-07-29).

        `_limit_subscription_candidates`가 `p.config.auth_mode`를 읽고 있었다.
        `QuotaFallbackProvider`로 감싸면 wrapper의 config는 호출부가 만든 것이고
        `auth_mode` 기본값이 `"api_key"`라 — **구독 provider가 하나도 안 보여서 상한이
        아예 발동하지 않았다.** wrapper는 실제로 답한 쪽을 따라가는 동적 `auth_mode`
        속성을 갖고 있는데(2026-07-28 `subscription_calls` 누락 수정 때 추가) 여기서
        그 속성을 우회하고 있었다 — **같은 버그를 두 번 다른 자리에서 만든 것**이다.

        기존 상한 테스트들은 **wrapper를 안 쓴 MockProvider**만 써서 이걸 못 잡았다
        (감싸지 않으면 `config.auth_mode`와 `auth_mode`가 같다). 실사용은 전부
        감싸져 있으므로(`config.json`의 폴백 설정) 여기서 감싼 경우를 고정한다.
        """
        def wrapped(name: str, auth_mode: str) -> QuotaFallbackProvider:
            primary = MockProvider(
                ProviderConfig(provider_id=name, model_id=name, auth_mode=auth_mode), profile="concise"
            )
            fallback = MockProvider(
                ProviderConfig(provider_id=f"{name}-fb", model_id=f"{name}-fb", auth_mode="api_key"),
                profile="concise",
            )
            # wrapper config는 auth_mode를 안 준다 — 실사용(cli._wrap_with_quota_fallback)과 같다
            return QuotaFallbackProvider(
                primary=primary, fallback=fallback,
                config=ProviderConfig(provider_id=name, model_id=name),
            )

        candidates = [
            wrapped("sub-a", "cli_subscription"),
            wrapped("sub-b", "cli_subscription"),
            wrapped("paid-c", "api_key"),
        ]
        saved = orchestrator.MAX_SUBSCRIPTION_CANDIDATES
        orchestrator.MAX_SUBSCRIPTION_CANDIDATES = 1
        self.addCleanup(setattr, orchestrator, "MAX_SUBSCRIPTION_CANDIDATES", saved)

        limited = orchestrator._limit_subscription_candidates(candidates)

        # 감싼 상태에서도 구독 2개를 알아보고 1개로 줄여야 한다(+ api_key는 유지)
        self.assertEqual([p.provider_id for p in limited], ["sub-a", "paid-c"])

    def test_chain_role_providers_are_not_fan_out_candidates(self) -> None:
        """체인 역할 provider가 후보 자리에 새면 안 된다 (2026-07-29).

        `_candidate_providers`가 judge/agent만 제외해서, `fan_out_judge`가
        **`research-mock`을 3번째 후보로 쓰고 있었다**(`num_candidates` 기본값 3 =
        claude, codex, research-mock). 역할 provider는 `hierarchical_delegation`이
        `providers[step.provider_id]`로 직접 찾는 것이다.

        지금까지 무해했던 건 역할 모델이 후보 모델과 같아서였고, 역할별 모델을 다르게
        두면(2026-07-29 재편) 후보 구성이 조용히 오염된다.
        """
        from harness import planner

        providers = {
            "claude": MockProvider(ProviderConfig(provider_id="claude", model_id="claude"), profile="concise"),
            orchestrator.JUDGE_PROVIDER_KEY: MockProvider(
                ProviderConfig(provider_id="judge", model_id="judge"), profile="judge"
            ),
            orchestrator.AGENT_PROVIDER_KEY: MockProvider(
                ProviderConfig(provider_id="agent", model_id="agent"), profile="concise"
            ),
        }
        for role in planner.KNOWN_DELEGATION_ROLES:
            providers[f"{role}-mock"] = MockProvider(
                ProviderConfig(provider_id=f"{role}-mock", model_id=f"{role}-mock"), profile="concise"
            )

        candidates = orchestrator._candidate_providers(providers)

        self.assertEqual(list(candidates), ["claude"])

    def test_subscription_candidates_capped_to_protect_quota(self) -> None:
        """구독 provider가 2개(model-a, model-b) 있어도 MAX_SUBSCRIPTION_CANDIDATES=1이라
        먼저 오는 구독 provider 1개(model-a) + api_key provider(model-c)만 호출되고,
        나머지 구독 provider(model-b)는 아예 호출조차 안 되는지 확인한다 (Section 9)."""
        task = make_task("fan-out-quota", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘")

        observation = orchestrator.run(task, mixed_auth_fan_out_providers(), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-fan-out-quota"
        self.assertEqual(observation.status, "success")
        candidates_dir = run_dir / "artifacts" / "candidates"
        called_model_ids = sorted(p.stem for p in candidates_dir.glob("*.md"))
        self.assertEqual(called_model_ids, ["model-a", "model-c"])  # model-b(2번째 구독)는 호출조차 안 됨

    def test_subscription_cap_skipped_when_it_would_break_min_candidates(self) -> None:
        """구독 provider만 2개뿐이면, 한도 보호를 그대로 적용하면 1개(<MIN_CANDIDATES)만
        남아 run 자체가 실패한다 — 그럴 땐 한도 보호를 포기하고 둘 다 쓴다."""
        task = make_task("fan-out-quota-fallback", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘")

        observation = orchestrator.run(task, all_subscription_fan_out_providers(), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-fan-out-quota-fallback"
        self.assertEqual(observation.status, "success")
        candidates_dir = run_dir / "artifacts" / "candidates"
        called_model_ids = sorted(p.stem for p in candidates_dir.glob("*.md"))
        self.assertEqual(called_model_ids, ["model-a", "model-b"])  # 둘 다 호출됨 (보호 로직 포기)

    def test_below_min_candidates_fails_without_final_output(self) -> None:
        task = make_task("fan-out-below-min", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘")
        providers = fan_out_providers(fail_times={"model-a": 2, "model-b": 2})  # 2개 영구 실패, 1개만 성공

        observation = orchestrator.run(task, providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-fan-out-below-min"
        self.assertEqual(observation.status, "error")
        self.assertFalse((run_dir / "judging.json").exists())
        self.assertFalse((run_dir / "final.md").exists())
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertGreaterEqual(len(errors), 1)


class HierarchicalDelegationIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_full_run_creates_all_dod_files(self) -> None:
        # ADR 0009(2026-07-29)로 체인은 opt-in 전용 — 키워드만으로는 fan_out_judge로 간다.
        task = make_task("delegation-demo", "경쟁사 A/B/C의 가격 정책을 리서치해줘", ["team_pattern:hierarchical_delegation"])

        observation = orchestrator.run(task, delegation_providers(), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-delegation-demo"
        self.assertEqual(observation.status, "success")
        for name in ("input.json", "plan.md", "plan.json", "fitness_check.json", "final.md", "safety.md",
                     "metrics.json", "errors.json", "run_meta.json"):
            self.assertTrue((run_dir / name).exists(), f"{name} 이 없음")
        self.assertFalse((run_dir / "judging.json").exists())  # 이 패턴엔 Judge 자체가 없음
        # 매니저가 지시한 것과 돌아온 것 — 이 패턴의 유일한 감사 기록 (본문은 조각 파일에)
        self.assertTrue((run_dir / "delegation.json").exists())
        parts_dir = run_dir / "artifacts" / "parts"
        self.assertTrue(parts_dir.is_dir())
        self.assertGreaterEqual(len(list(parts_dir.glob("*.md"))), 2)

    def test_manager_never_receives_part_bodies_in_concat_mode(self) -> None:
        """**이 패턴의 존재 이유**: 기본 조립에서 매니저는 조각 본문을 보지 않는다.

        매니저 호출은 분해 1회뿐이어야 한다. 조립까지 매니저가 하면 조각 총량이 매니저
        입력으로 들어가 아끼려던 걸 그대로 쓴다(ADR 0014).
        """
        task = make_task(
            "delegation-concat", "경쟁사 A/B/C의 가격 정책을 리서치해줘", ["team_pattern:hierarchical_delegation"]
        )
        providers = delegation_providers()

        orchestrator.run(task, providers, root=self.tmp_dir)

        manager = providers[orchestrator.MANAGER_PROVIDER_KEY]
        self.assertEqual(manager.call_count, 1)  # 분해만 — 조립은 LLM 호출 0회

    def test_worker_failure_keeps_the_other_parts_as_partial(self) -> None:
        """조각 하나가 실패해도 나머지는 살린다 — 체인과 달리 조각은 서로 의존하지 않는다.

        체인에서는 중간 실패가 뒤 단계를 통째로 막았다(앞 결과가 입력이므로). 조각은
        독립이라 실패한 것만 빠지고 나머지가 그대로 문서가 된다.
        """
        task = make_task(
            "delegation-part-fail", "경쟁사 A/B/C의 가격 정책을 리서치해줘", ["team_pattern:hierarchical_delegation"]
        )
        providers = delegation_providers(fail_times={"worker2": 2})  # 재시도까지 실패

        observation = orchestrator.run(task, providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-delegation-part-fail"
        self.assertEqual(observation.status, "warning")
        final_content = run_store.read_markdown(run_dir, "final.md")
        self.assertTrue(final_content.startswith("(partial)"))
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any("delegation worker" in e["stage"] for e in errors))

    def test_partial_promotion_still_runs_safety_check(self) -> None:
        """회귀 테스트: partial로 승격되는 내용도 Safety 체크를 반드시 거쳐야 한다
        (한때 _finalize_partial_chain이 safety.check() 호출 없이 바로 final.md를 썼던
        버그가 있었음 — Section 12.1 "Safety는 어떤 경로에서도 생략하지 않는다" 위반).

        Phase 4(Safety Release Gate) 도입 이후로는 안전하지 않은 내용을 즉시 차단하는
        대신 "검토 대기"로 멈춘다 — 자세한 pending/release/block 동작은
        test_phase4_safety_gate.py에서 검증하고, 여기서는 "Safety 체크 자체가 반드시
        실행됐는가"만 확인한다."""
        task = make_task(
            "delegation-partial-unsafe",
            "설계 리뷰 결과를 반영해서 순차 검토를 진행해줘. 주민등록번호 예시를 포함해서 검토해줘.",
            ["team_pattern:hierarchical_delegation"],  # ADR 0009: 진입은 여전히 opt-in
        )
        providers = delegation_providers(fail_times={"worker2": 2})

        observation = orchestrator.run(task, providers, root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-delegation-partial-unsafe"
        self.assertEqual(observation.status, "warning")  # 검토 대기 중
        self.assertFalse((run_dir / "final.md").exists())  # 검토 전까지 공개되지 않음
        self.assertTrue((run_dir / "safety.md").exists())
        self.assertTrue((run_dir / "safety_review.json").exists())
        errors = run_store.read_json(run_dir, "errors.json")
        self.assertTrue(any(e["stage"] == "safety" for e in errors))


class ReproducibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_rerun_same_task_reproduces_same_file_structure(self) -> None:
        task = make_task("repeat-demo", "이 설계안을 검토해줘: 아키텍처를 비교 분석해줘")

        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)
        run_dir = self.tmp_dir / "run-repeat-demo"
        first_files = sorted(p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_file())

        shutil.rmtree(run_dir)
        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)  # 새 provider 인스턴스로 재실행
        second_files = sorted(p.relative_to(run_dir) for p in run_dir.rglob("*") if p.is_file())

        self.assertEqual(first_files, second_files)


class FitnessGateIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_trivial_task_uses_direct_call_and_skips_pattern_dispatch(self) -> None:
        task = make_task("trivial-demo", "지금 몇 시야?")

        observation = orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)

        run_dir = self.tmp_dir / "run-trivial-demo"
        self.assertEqual(observation.status, "success")
        self.assertFalse(run_store.read_json(run_dir, "fitness_check.json")["passed"])
        self.assertFalse((run_dir / "plan.md").exists())  # Planner 자체를 건너뜀
        self.assertFalse((run_dir / "judging.json").exists())
        self.assertEqual(list((run_dir / "artifacts" / "candidates").iterdir()), [])
        self.assertTrue((run_dir / "final.md").exists())


class ApprovalCheckpointIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)

    def test_high_risk_task_blocks_until_approved_then_completes(self) -> None:
        task = make_task(
            "high-risk-demo",
            "프로덕션 결제 시스템에 배포할 변경사항을 검토해줘",
            constraints=["risk_level:high"],
        )

        pending = orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)
        run_dir = self.tmp_dir / "run-high-risk-demo"

        self.assertEqual(pending.status, "warning")
        self.assertEqual(run_store.read_json(run_dir, "approval.json")["status"], "pending")
        self.assertFalse((run_dir / "final.md").exists())
        self.assertEqual(list((run_dir / "artifacts" / "candidates").iterdir()), [])

        approved = orchestrator.resume("run-high-risk-demo", "approved", fan_out_providers(), root=self.tmp_dir)

        self.assertEqual(approved.status, "success")
        self.assertEqual(run_store.read_json(run_dir, "approval.json")["status"], "approved")
        self.assertTrue((run_dir / "final.md").exists())

    def test_resume_refreshes_run_meta_pid(self) -> None:
        """resume()은 별도 프로세스(예: 다른 CLI 호출)에서 실행될 수 있으므로,
        run_meta.json의 pid를 지금 실제로 패턴을 실행하는 프로세스로 갱신해야
        live_status.describe_run()이 "interrupted"로 오판하지 않는다."""
        task = make_task(
            "high-risk-run-meta", "프로덕션 결제 시스템에 배포할 변경사항을 검토해줘",
            constraints=["risk_level:high"],
        )

        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)
        run_dir = self.tmp_dir / "run-high-risk-run-meta"
        meta_after_run = run_store.read_json(run_dir, "run_meta.json")
        self.assertEqual(meta_after_run["pid"], os.getpid())

        orchestrator.resume("run-high-risk-run-meta", "approved", fan_out_providers(), root=self.tmp_dir)
        meta_after_resume = run_store.read_json(run_dir, "run_meta.json")
        self.assertEqual(meta_after_resume["pid"], os.getpid())
        self.assertGreaterEqual(meta_after_resume["started_at"], meta_after_run["started_at"])

    def test_high_risk_task_rejected_never_executes(self) -> None:
        task = make_task(
            "high-risk-reject-demo",
            "프로덕션 결제 시스템에 배포할 변경사항을 검토해줘",
            constraints=["risk_level:high"],
        )

        orchestrator.run(task, fan_out_providers(), root=self.tmp_dir)
        run_dir = self.tmp_dir / "run-high-risk-reject-demo"

        rejected = orchestrator.resume(
            "run-high-risk-reject-demo", "rejected", fan_out_providers(), root=self.tmp_dir
        )

        self.assertEqual(rejected.status, "error")
        self.assertEqual(run_store.read_json(run_dir, "approval.json")["status"], "rejected")
        self.assertFalse((run_dir / "final.md").exists())
        self.assertEqual(list((run_dir / "artifacts" / "candidates").iterdir()), [])


if __name__ == "__main__":
    unittest.main()
