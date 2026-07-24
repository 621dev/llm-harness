"""Step 2 테스트: mock provider + model_runner (stdlib unittest, 외부 패키지 불필요).

harness-implementation-plan-ko.md Section 7 Step 2 체크리스트를 검증한다.
- 3개 mock provider가 서로 다른 프로필로 결정적 응답을 내는가
- model_runner.run_all이 provider마다 artifacts/candidates/<model_id>.md를 저장하는가
- 실패 주입: 1회 실패 후 재시도로 복구되는가, 재시도까지 다 실패하면 error 후보로
  기록되고 나머지 provider는 계속 진행되는가 (Section 6 복구 전략)
- 동일 prompt로 재실행 시 동일 결과가 나오는가 (재현성)

실행: python -m unittest tests/test_step2_model_runner.py -v (harness-mvp 디렉토리에서)
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from harness import model_runner, run_store  # noqa: E402
from harness.schemas import ProviderConfig  # noqa: E402
from providers.mock import MockProvider  # noqa: E402


def make_providers(*, fail_times: dict[str, int] | None = None) -> list[MockProvider]:
    fail_times = fail_times or {}
    specs = [
        ("model-a", "concise"),
        ("model-b", "detailed"),
        ("model-c", "creative"),
    ]
    return [
        MockProvider(
            ProviderConfig(provider_id=provider_id, model_id=provider_id),
            profile=profile,
            fail_times=fail_times.get(provider_id, 0),
        )
        for provider_id, profile in specs
    ]


class ModelRunnerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = Path(tempfile.mkdtemp(prefix="harness-test-"))
        self.addCleanup(shutil.rmtree, self.tmp_dir, ignore_errors=True)
        self.run_dir = run_store.create_run(run_id="run-model-runner-test", root=self.tmp_dir)

    def test_run_all_generates_one_candidate_per_provider_with_distinct_profiles(self) -> None:
        providers = make_providers()

        candidates = model_runner.run_all("설계안을 검토해줘", providers, self.run_dir)

        self.assertEqual(len(candidates), 3)
        self.assertEqual([c.status for c in candidates], ["success", "success", "success"])
        self.assertTrue(all("설계안을 검토해줘" in c.content for c in candidates))
        # 세 후보의 내용이 서로 달라야 한다 (모델별 다른 강점 시뮬레이션)
        contents = {c.content for c in candidates}
        self.assertEqual(len(contents), 3)

        for candidate in candidates:
            candidate_path = self.run_dir / "artifacts" / "candidates" / f"{candidate.model_id}.md"
            self.assertTrue(candidate_path.exists())
            self.assertIn(candidate.content, candidate_path.read_text(encoding="utf-8"))

    def test_provider_recovers_after_one_retry(self) -> None:
        providers = make_providers(fail_times={"model-a": 1})  # 1회 실패 후 재시도로 성공

        candidates = model_runner.run_all("리서치해줘", providers, self.run_dir)

        candidate_a = next(c for c in candidates if c.model_id == "model-a")
        self.assertEqual(candidate_a.status, "success")
        self.assertEqual(providers[0].call_count, 2)  # 최초 호출 + 재시도 1회

    def test_provider_exhausts_retry_records_error_and_others_continue(self) -> None:
        # model-b는 재시도(1회)까지 다 실패하는 상황을 주입한다 (MAX_RETRIES=1 이므로 총 2번 호출).
        providers = make_providers(fail_times={"model-b": 2})

        candidates = model_runner.run_all("리서치해줘", providers, self.run_dir)

        statuses = {c.model_id: c.status for c in candidates}
        self.assertEqual(statuses["model-a"], "success")
        self.assertEqual(statuses["model-b"], "error")
        self.assertEqual(statuses["model-c"], "success")  # 한 provider의 실패가 나머지를 막지 않음
        self.assertEqual(providers[1].call_count, 2)

        error_candidate = next(c for c in candidates if c.model_id == "model-b")
        error_path = self.run_dir / "artifacts" / "candidates" / "model-b.md"
        self.assertIn("error", error_path.read_text(encoding="utf-8"))
        self.assertIsNone(error_candidate.tokens)
        self.assertIsNone(error_candidate.cost_usd)

    def test_rerun_same_prompt_is_reproducible(self) -> None:
        providers_first = make_providers()
        first = model_runner.run_all("동일 프롬프트", providers_first, self.run_dir)

        providers_second = make_providers()
        second = model_runner.run_all("동일 프롬프트", providers_second, self.run_dir)

        self.assertEqual([c.content for c in first], [c.content for c in second])

    def test_cost_usd_only_filled_for_api_key_auth_mode(self) -> None:
        cli_provider = MockProvider(
            ProviderConfig(provider_id="model-a", model_id="model-a", auth_mode="cli_subscription"),
            profile="concise",
        )
        api_provider = MockProvider(
            ProviderConfig(provider_id="model-b", model_id="model-b", auth_mode="api_key"),
            profile="concise",
        )

        candidates = model_runner.run_all("비용 필드 확인", [cli_provider, api_provider], self.run_dir)

        self.assertIsNone(candidates[0].cost_usd)
        self.assertIsNotNone(candidates[1].cost_usd)


if __name__ == "__main__":
    unittest.main()
