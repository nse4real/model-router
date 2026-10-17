"""Offline calibration joins and guarded judging; all provider calls are mocks."""
import asyncio
from copy import deepcopy
import gc
from decimal import Decimal
import io
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import warnings
import unittest
from unittest.mock import patch

from api.contracts import Request
from benchmark.calibrate_judge import calibrate, load_pairs, main, parse_score, score_pairs
from benchmark.dataset import load_dataset
from benchmark.recover import recover
from fallback.providers import LiteLLMProvider, MockJudgeProvider, Output
from monitoring.ledger import SQLiteLedger
from monitoring.quality import Quality, validate_calibration
from routing.admission import Admission
from routing.config import Config


class RecordingJudge(MockJudgeProvider):
    def __init__(self, content='{"score":3}', stop_reason="stop"):
        super().__init__()
        self.content, self.stop_reason = content, stop_reason
        self.requests = []
        self.timeouts = []

    async def complete(self, model, request, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        self.calls.append(model["id"])
        return Output(self.content, 10, 5, self.stop_reason, model["id"])


def fixture():
    rows, manifest = load_dataset()
    rows = [row for row in rows if row["split"] == "calibration"][:25]
    artifact = dict(storage="postgres", data_source="real-unverified", split="calibration",
                    run_id="synthetic-calibration", dataset_checksum=manifest["sha256"], records=[])
    ratings, mapping = [], []
    for index, row in enumerate(rows):
        blind = f"blind-{index}"
        answers = {"A": f"First synthetic answer {index}", "B": f"Second synthetic answer {index}"}
        left, right = ("B", "A") if index % 2 == 0 else ("A", "B")
        ratings.append(dict(blind_id=blind, prompt=row["prompt"], left=answers[left], right=answers[right],
                            left_rating=2, right_rating=5))
        mapping.append(dict(blind_id=blind, prompt_id=row["id"], left_condition=left,
                            tier=row["tier"], task_type=row["task_type"],
                            left_completed=True, right_completed=True))
        for condition in ("A", "B", "C-cold", "C-warm"):
            artifact["records"].append(dict(prompt_id=row["id"], condition=condition,
                content=answers.get(condition, "UNRATED ANSWER MUST NEVER REACH JUDGE"),
                completed=True, status=200, request_id=f"source-{index}-{condition}"))
    # Join by ID, not by list position.
    return artifact, ratings, list(reversed(mapping))


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact, self.ratings, self.mapping = fixture()
        self.paths = {}
        for name, value in (("artifact", self.artifact), ("ratings", self.ratings), ("mapping", self.mapping)):
            path = self.root / (name + ".json")
            path.write_text(json.dumps(value), encoding="utf8")
            self.paths[name] = path
        self.paths["output"] = self.root / "pairs.json"
        self.ledger = SQLiteLedger()
        self.addCleanup(self.ledger.close)
        self.provider = RecordingJudge()
        self.config = Config()

    def pairs(self):
        return load_pairs(self.artifact, self.ratings, self.mapping)

    def test_calibration_timeout_defaults_to_30(self):
        self.assertEqual(self.config.judge_calibration_timeout_s, 30)
        data = deepcopy(self.config.data)
        data.pop("judge_calibration_timeout_s", None)
        self.assertEqual(Config(data=data).judge_calibration_timeout_s, 30)

    def test_calibration_timeout_rejects_out_of_range_and_non_integer_values(self):
        for value in (0, -1, 61, 30.0, "30", True, False, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "judge_calibration_timeout_s"):
                Config(data=dict(self.config.data, judge_calibration_timeout_s=value))
        for value in (1, 60):
            with self.subTest(value=value):
                self.assertEqual(Config(data=dict(self.config.data,
                    judge_calibration_timeout_s=value)).judge_calibration_timeout_s, value)

    async def test_default_calibration_timeout_reaches_judge_call(self):
        _, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config, self.provider,
                                       "default-timeout", Admission())
        self.assertEqual(failures, [])
        self.assertEqual(self.provider.timeouts, [30, 30])

    async def test_configured_calibration_timeout_reaches_provider_and_outer_wait(self):
        config = Config(data=dict(self.config.data, judge_calibration_timeout_s=47))
        with patch("monitoring.quality.asyncio.wait_for", wraps=asyncio.wait_for) as wait:
            _, failures = await score_pairs(self.pairs()[:1], self.ledger, config, self.provider,
                                           "configured-timeout", Admission())
        self.assertEqual(failures, [])
        self.assertEqual(self.provider.timeouts, [47, 47])
        self.assertEqual([call.kwargs["timeout"] for call in wait.call_args_list], [47, 47])

    async def test_live_and_benchmark_judge_timeouts_remain_5_seconds(self):
        config = Config(data=dict(self.config.data, judge_calibration_timeout_s=47))
        request = Request.parse(dict(messages=[dict(role="user", content="Synthetic prompt")]))
        for benchmark in (False, True):
            with self.subTest(benchmark=benchmark):
                quality = Quality(self.ledger, config, self.provider, benchmark=benchmark)
                parent, attempt = f"parent-{benchmark}", f"judge-{benchmark}"
                self.ledger.begin(parent, "unchanged-timeouts")
                self.ledger.job(attempt, parent)
                await quality.process((attempt, parent, "simple", request, Output("Answer", 0, 0)))
                self.assertEqual(quality.timeout, 5)
        self.assertEqual(self.provider.timeouts, [5, 5])

    def test_left_b_maps_human_ratings_by_condition_and_blind_id(self):
        pairs = self.pairs()
        self.assertEqual((pairs[0]["human_a"], pairs[0]["human_b"]), (5, 2))
        self.assertEqual((pairs[1]["human_a"], pairs[1]["human_b"]), (2, 5))
        self.assertEqual(pairs[0]["records"]["a"]["content"], self.ratings[0]["right"])
        mapping = deepcopy(self.mapping)
        for row in mapping:
            row["right_condition"] = "B" if row["left_condition"] == "A" else "A"
        self.assertEqual(load_pairs(self.artifact, self.ratings, mapping), pairs)

    async def test_only_50_rated_outputs_and_no_condition_or_human_metadata_reach_judge(self):
        results, failures = await score_pairs(self.pairs(), self.ledger, self.config, self.provider,
                                              self.artifact["run_id"], Admission())
        self.assertEqual(failures, [])
        self.assertEqual(len(self.provider.requests), 50)
        self.assertEqual(len(results), 25)
        self.assertTrue(all(type(p["judge_a"]) is int and p["judge_a"] == 3 for p in results))
        rubric = Path("monitoring/judge_prompt_v1.md").read_text()
        for index, request in enumerate(self.provider.requests):
            self.assertEqual(request.messages[0], dict(role="system", content=rubric))
            payload = json.loads(request.messages[1]["content"])
            self.assertEqual(set(payload), {"messages", "answer"})
            self.assertEqual(payload["messages"], [
                dict(role="system", content="Answer accurately and concisely."),
                dict(role="user", content=self.ratings[index // 2]["prompt"])])
            self.assertEqual(payload["answer"], self.artifact["records"][(index // 2) * 4 + index % 2]["content"])
            self.assertEqual(request.max_tokens, 256)
        attempts = self.ledger.attempts()
        self.assertEqual(len(attempts), 50)
        self.assertTrue(all(a["purpose"] == "evaluation" and a["state"] == "settled" for a in attempts))
        self.assertEqual(set(self.provider.calls), {self.config.model("mid")["id"]})

    def test_strict_json_rejects_malformed_missing_and_coercible_scores(self):
        for content in ('', 'not json', '```json\n{"score":3}\n```', '{}', 'null', '[]',
                        '{"score":"3"}', '{"score":3.0}', '{"score":true}', '{"score":null}',
                        '{"score":0}', '{"score":6}', '{"score":NaN}', '{"score":3,"extra":1}',
                        '{"score":2,"score":3}'):
            with self.subTest(content=content), self.assertRaises((ValueError, TypeError)):
                parse_score(content)
        self.assertEqual(parse_score(' {"score": 5} \n'), 5)

    async def test_malformed_response_is_unknown_and_usage_remains_charged(self):
        for content, stop in (('{"score":"3"}', "stop"), ('{"score":3,"extra":1}', "stop"),
                              ('{"score":3}', "length")):
            with self.subTest(content=content, stop=stop):
                provider = RecordingJudge(content, stop)
                ledger = SQLiteLedger()
                self.addCleanup(ledger.close)
                results, failures = await score_pairs(self.pairs()[:1], ledger, self.config, provider,
                                                      "malformed", Admission())
                self.assertIsNone(results[0]["judge_a"])
                self.assertIsNone(results[0]["judge_b"])
                self.assertEqual(len(failures), 2)
                self.assertEqual(failures[0]["blind_id"], "blind-0")
                self.assertEqual(failures[0]["reason"], "Malformed or incomplete judge response")
                for attempt in ledger.attempts():
                    self.assertEqual(attempt["state"], "settled")
                    self.assertEqual(Decimal(str(attempt["cost"])), self.config.cost("mid", 10, 5))
                with ledger.transaction() as db:
                    jobs = db.execute("SELECT state,score FROM judge_jobs").fetchall()
                self.assertTrue(all(j["state"] == "quality_unknown" and j["score"] is None for j in jobs))

    async def test_evaluation_cap_blocks_dispatch_with_existing_evaluation_spend(self):
        self.ledger.begin("earlier")
        self.ledger.reserve("prior", "earlier", "model", "mock", "evaluation", "v", "3")
        self.ledger.dispatch("prior")
        self.ledger.settle("prior", Decimal("3"), 1, 1)
        results, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config, self.provider,
                                              "capped", Admission())
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(len(failures), 2)
        self.assertIsNone(results[0]["judge_a"])
        self.assertEqual(len(self.ledger.attempts()), 1)

    async def test_global_cap_blocks_dispatch(self):
        ledger = SQLiteLedger(cap="0")
        self.addCleanup(ledger.close)
        _, failures = await score_pairs(self.pairs()[:1], ledger, self.config, self.provider,
                                        "global-cap", Admission())
        self.assertEqual(len(failures), 2)
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(ledger.attempts(), [])

    async def test_dry_run_makes_zero_calls_and_creates_no_outputs(self):
        with patch("benchmark.calibrate_judge.real_provider") as provider, patch(
                "benchmark.calibrate_judge.PostgresLedger") as ledger, patch(
                "benchmark.calibrate_judge.shared_admission") as admission:
            result = await calibrate(**self.paths)
        self.assertEqual(result, dict(dry_run=True, pairs=25, planned_calls=50))
        provider.assert_not_called()
        ledger.assert_not_called()
        admission.assert_not_called()
        self.assertFalse(self.paths["output"].exists())
        self.assertFalse((self.root / "pairs-report.json").exists())

    async def test_dry_run_with_existing_outputs_leaves_them_untouched(self):
        self.paths["output"].write_text("original pairs")
        report = self.root / "pairs-report.json"
        report.write_text("original report")
        with patch("benchmark.calibrate_judge.real_provider") as provider, patch(
                "benchmark.calibrate_judge.PostgresLedger") as ledger:
            plan = await calibrate(**self.paths)
        self.assertEqual(plan["planned_calls"], 50)
        self.assertEqual(self.paths["output"].read_text(), "original pairs")
        self.assertEqual(report.read_text(), "original report")
        provider.assert_not_called()
        ledger.assert_not_called()

    async def test_storage_outage_still_writes_reason_without_dispatch(self):
        self.ledger.available = False
        try:
            with self.mocked_pipeline():
                result = await calibrate(**self.paths, real=True)
        finally:
            self.ledger.available = True
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(len(result["unknown_outputs"]), 50)
        self.assertTrue(all(f["reason"].startswith("StorageUnavailable") for f in result["unknown_outputs"]))
        report = json.loads((self.root / "pairs-report.json").read_text())
        self.assertEqual(report["reason"], "judge incomplete (50 calls unresolved)")

    async def test_existing_output_or_report_is_refused_before_provider_setup(self):
        for name in ("pairs.json", "pairs-report.json"):
            path = self.root / name
            path.write_text("keep", encoding="utf8")
            with patch("benchmark.calibrate_judge.real_provider") as provider:
                with self.assertRaises(FileExistsError):
                    await calibrate(**self.paths, real=True)
            provider.assert_not_called()
            self.assertEqual(path.read_text(), "keep")
            path.unlink()

    async def test_output_creation_race_refuses_before_calls_and_removes_partial_claim(self):
        original_open = Path.open
        report = self.root / "pairs-report.json"

        def racing_open(path, *args, **kwargs):
            if path == report and args and args[0] == "x":
                with original_open(path, "w", encoding="utf8") as stream:
                    stream.write("other process")
            return original_open(path, *args, **kwargs)

        with patch("benchmark.calibrate_judge.real_provider", return_value=self.provider), patch(
                "benchmark.calibrate_judge.PostgresLedger") as ledger, patch.object(Path, "open", racing_open):
            with self.assertRaises(FileExistsError):
                await calibrate(**self.paths, real=True)
        ledger.assert_not_called()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(report.read_text(), "other process")
        self.assertFalse(self.paths["output"].exists())

    async def test_real_guard_requires_confirmation_before_storage_or_calls(self):
        with patch.dict("os.environ", {"ROUTER_CONFIRM_REAL_SPEND": "NO"}), patch(
                "benchmark.calibrate_judge.PostgresLedger") as ledger:
            with self.assertRaisesRegex(ValueError, "ROUTER_CONFIRM_REAL_SPEND"):
                await calibrate(**self.paths, real=True)
        ledger.assert_not_called()
        self.assertFalse(self.paths["output"].exists())

    async def test_mocked_pipeline_writes_pairs_and_validator_report_using_configured_caps(self):
        with patch("benchmark.calibrate_judge.real_provider", return_value=self.provider) as factory, patch(
                "benchmark.calibrate_judge.PostgresLedger", return_value=self.ledger) as ledger, patch(
                "benchmark.calibrate_judge.shared_admission", return_value=Admission()), patch(
                "benchmark.calibrate_judge.validate_calibration", return_value=dict(validated=True)) as validate:
            result = await calibrate(**self.paths, real=True)
        factory.assert_called_once_with(True)
        self.assertEqual(ledger.call_args.kwargs, dict(cap=self.config.data["global_cap_usd"],
            evaluation_cap=self.config.data["evaluation_cap_usd"]))
        pairs = json.loads(self.paths["output"].read_text())
        validate.assert_called_once_with(pairs)
        self.assertEqual(len(pairs), 25)
        report = json.loads((self.root / "pairs-report.json").read_text())
        self.assertTrue(result["validated"])
        self.assertEqual(report["unknown_outputs"], [])
        self.assertEqual(report["source_run_id"], self.artifact["run_id"])
        # Completed durable jobs are reused, never replayed, even with a fresh output.
        await score_pairs(self.pairs(), self.ledger, self.config, self.provider,
                          self.artifact["run_id"], Admission())
        self.assertEqual(len(self.provider.calls), 50)

    async def test_unknown_pair_writes_failed_report_with_pair_identity(self):
        provider = RecordingJudge('{}')
        with patch("benchmark.calibrate_judge.real_provider", return_value=provider), patch(
                "benchmark.calibrate_judge.PostgresLedger", return_value=self.ledger), patch(
                "benchmark.calibrate_judge.shared_admission", return_value=Admission()):
            result = await calibrate(**self.paths, real=True)
        self.assertFalse(result["validated"])
        self.assertEqual(len(result["unknown_outputs"]), 50)
        report = json.loads((self.root / "pairs-report.json").read_text())
        self.assertFalse(report["validated"])
        self.assertEqual(report["reason"], "judge incomplete (50 calls unresolved)")
        self.assertEqual(report["unknown_outputs"][0]["blind_id"], "blind-0")

    def mocked_pipeline(self, provider=None):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(patch("benchmark.calibrate_judge.real_provider", return_value=provider or self.provider))
        stack.enter_context(patch("benchmark.calibrate_judge.PostgresLedger", return_value=self.ledger))
        stack.enter_context(patch("benchmark.calibrate_judge.shared_admission", return_value=Admission()))
        return stack

    async def test_litellm_executor_handoff_is_drained_even_on_repeated_cancellation(self):
        started, release = asyncio.Event(), threading.Event()
        loop = asyncio.get_running_loop()
        completed = []
        created = []

        async def provider_coroutine():
            try:
                await asyncio.sleep(0)
                completed.append("completed")
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='{"score":3}'),
                                        finish_reason="stop")],
                    usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5), model="fixture")
            except asyncio.CancelledError:
                completed.append("cancelled")
                raise

        def factory():
            loop.call_soon_threadsafe(started.set)
            if not release.wait(5):
                raise RuntimeError("fixture handoff was not released")
            call = provider_coroutine()
            created.append(call)
            return call

        parameters = []

        async def sdk_completion(**kwargs):
            parameters.append(kwargs)
            call = await loop.run_in_executor(None, factory)
            return await call

        with warnings.catch_warnings(record=True) as caught, patch.dict("sys.modules", {
                "litellm": SimpleNamespace(acompletion=sdk_completion)}), patch.dict(
                "os.environ", ROUTER_CONFIRM_REAL_SPEND="YES"):
            warnings.simplefilter("always", RuntimeWarning)
            provider = LiteLLMProvider(True)
            request = Request.parse(
                dict(messages=[dict(role="user", content="synthetic")]))
            call = asyncio.create_task(provider.complete(self.config.model("mid"), request, .01))
            try:
                await asyncio.wait_for(started.wait(), 2)
                call.cancel()
                await asyncio.sleep(0)
                call.cancel()
                await asyncio.sleep(0)
                self.assertFalse(call.done())
            finally:
                release.set()
            with self.assertRaises(asyncio.CancelledError):
                await call
            self.assertEqual(completed, ["completed"])
            self.assertTrue(all(c.cr_frame is None for c in created))
            self.assertEqual(parameters[0]["num_retries"], 0)
            self.assertEqual(parameters[0]["timeout"], .01)
            gc.collect()
            self.assertFalse([w for w in caught if "never awaited" in str(w.message)])

    async def test_timeout_drains_every_judge_call_and_reports_persisted_reason(self):
        completed, cancelled = [], []

        class SlowJudge(RecordingJudge):
            async def complete(inner, model, request, timeout):
                inner.calls.append(model["id"])
                try:
                    await asyncio.sleep(1)
                    completed.append(model["id"])
                    return Output('{"score":3}', 10, 5)
                except asyncio.CancelledError:
                    cancelled.append(model["id"])
                    raise

        def bounded_quality(*args, **kwargs):
            kwargs["timeout"] = .001
            return Quality(*args, **kwargs)

        provider = SlowJudge()
        with self.mocked_pipeline(provider), patch("benchmark.calibrate_judge.Quality", side_effect=bounded_quality):
            result = await calibrate(**self.paths, real=True)
        self.assertEqual(len(provider.calls), len(completed) + len(cancelled))
        self.assertGreater(len(cancelled), 0)
        self.assertEqual(len(cancelled), len(provider.calls))
        self.assertTrue(all(f["reason"] == "TimeoutError" for f in result["unknown_outputs"]))
        report = json.loads((self.root / "pairs-report.json").read_text())
        self.assertEqual(report["reason"], "judge incomplete (50 calls unresolved)")
        with self.ledger.transaction() as db:
            jobs = db.execute("SELECT j.state,d.reason FROM judge_jobs j JOIN judge_job_reasons d "
                              "ON d.attempt_id=j.attempt_id").fetchall()
        self.assertTrue(all(j["reason"] == "TimeoutError" and j["state"] == "quality_unknown" for j in jobs))
        self.assertTrue(all(a["cost"] == a["reserved"] for a in self.ledger.attempts()))
        self.assertFalse([t for t in asyncio.all_tasks() if t is not asyncio.current_task() and not t.done()])

    async def test_probe_cancelled_before_awaiting_provider_leaves_no_coroutine(self):
        tasks = []
        create_task = asyncio.create_task

        def owned_task(coro, **kwargs):
            task = create_task(coro, **kwargs)
            tasks.append(task)
            return task

        async def cancelled_probe(health, model, permit, call, **kwargs):
            raise asyncio.CancelledError

        with warnings.catch_warnings(record=True) as caught, patch(
                "routing.latency.Health.run_probe", new=cancelled_probe), patch(
                "monitoring.quality.asyncio.create_task", side_effect=owned_task):
            warnings.simplefilter("always", RuntimeWarning)
            with self.assertRaises(asyncio.CancelledError):
                await score_pairs(self.pairs()[:1], self.ledger, self.config, self.provider,
                                  "cancel-before-probe", Admission())
            self.assertTrue(tasks)
            self.assertTrue(all(t.done() for t in tasks))
            self.assertTrue(all(t.get_coro().cr_frame is None for t in tasks))
            gc.collect()
            self.assertFalse([w for w in caught if "never awaited" in str(w.message)])

    async def test_cancelled_calibration_drains_call_and_writes_partial_report(self):
        started = asyncio.Event()
        cancelled = []

        class CancelJudge(RecordingJudge):
            async def complete(inner, model, request, timeout):
                inner.calls.append(model["id"])
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.append(model["id"])

        provider = CancelJudge()
        with self.mocked_pipeline(provider):
            call = asyncio.create_task(calibrate(**self.paths, real=True))
            await asyncio.wait_for(started.wait(), 2)
            call.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await call
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(len(cancelled), 1)
        report = json.loads((self.root / "pairs-report.json").read_text())
        self.assertEqual(report["reason"], "judge incomplete (50 calls unresolved)")
        self.assertTrue(all(f["reason"].startswith("CancelledError") for f in report["unknown_outputs"]))
        with self.ledger.transaction() as db:
            jobs = db.execute("SELECT j.state,d.reason FROM judge_jobs j JOIN judge_job_reasons d "
                              "ON d.attempt_id=j.attempt_id").fetchall()
        self.assertEqual(len(jobs), 50)
        self.assertTrue(all(j["reason"].startswith("CancelledError") for j in jobs))

    async def test_blank_exception_has_class_in_job_and_report(self):
        class FailedJudge(RecordingJudge):
            async def complete(inner, *args):
                raise RuntimeError

        with self.mocked_pipeline(FailedJudge()):
            result = await calibrate(**self.paths, real=True)
        self.assertTrue(all(f["reason"] == "RuntimeError" for f in result["unknown_outputs"]))
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute("SELECT reason FROM judge_job_reasons LIMIT 1").fetchone()["reason"],
                             "RuntimeError")

    async def test_rerun_only_unresolved_requires_recovery_and_keeps_reserved_charge(self):
        class OneFailure(RecordingJudge):
            async def complete(inner, *args):
                if not inner.calls:
                    inner.calls.append("failed")
                    raise TimeoutError
                return await super().complete(*args)

        # Synthetic timestamps only: recovery is never run against the real ledger.
        self.ledger.clock = lambda: 0
        with self.mocked_pipeline(OneFailure()):
            first = await calibrate(**self.paths, real=True)
        self.assertEqual(len(first["unknown_outputs"]), 1)
        previous = self.paths["output"]
        previous_report = self.root / "pairs-report.json"
        before_files = previous.read_bytes(), previous_report.read_bytes()
        before = json.loads(previous.read_text())
        stuck_id = first["unknown_outputs"][0]["attempt_id"]
        stuck = next(a for a in self.ledger.attempts() if a["attempt_id"] == stuck_id)
        self.assertEqual(stuck["state"], "dispatched")
        rerun_paths = dict(self.paths, output=self.root / "rerun.json", rerun_unresolved=previous)
        with self.mocked_pipeline():
            with self.assertRaisesRegex(ValueError, "benchmark.recover"):
                await calibrate(**rerun_paths, real=True)
        self.assertEqual(self.provider.calls, [])
        self.assertFalse(rerun_paths["output"].exists())
        self.assertEqual(next(a for a in self.ledger.attempts() if a["attempt_id"] == stuck_id), stuck)
        recovery_path = self.root / "synthetic-recovery.json"
        recovery_path.write_text(json.dumps(self.artifact))
        import time
        self.ledger.clock = time.time
        with patch("benchmark.recover.PostgresLedger", return_value=self.ledger), patch.object(
                self.ledger, "close"), patch.dict("os.environ", LEDGER_RESPONSE_STORE=str(self.root / "journal.sqlite")):
            recover(recovery_path)
        recovered = next(a for a in self.ledger.attempts() if a["attempt_id"] == stuck_id)
        self.assertEqual((recovered["state"], recovered["cost_status"]), ("unknown", "unknown"))
        self.assertEqual(recovered["cost"], stuck["reserved"])
        self.assertEqual(recovered["cost"], stuck["cost"])
        with self.mocked_pipeline():
            result = await calibrate(**rerun_paths, real=True)
        self.assertEqual(result["planned_calls"], 1)
        self.assertEqual(len(self.provider.calls), 1)
        after = json.loads(rerun_paths["output"].read_text())
        self.assertNotEqual(after[0]["attempt_a"], stuck_id)
        self.assertEqual(after[0]["judge_a"], 3)
        self.assertEqual(after[0]["attempt_b"], before[0]["attempt_b"])
        self.assertEqual(after[1:], before[1:])
        self.assertEqual((previous.read_bytes(), previous_report.read_bytes()), before_files)
        self.assertEqual(next(a for a in self.ledger.attempts() if a["attempt_id"] == stuck_id), recovered)
        self.assertEqual(len(self.ledger.attempts()), 51)
        total = sum(Decimal(str(a["cost"])) for a in self.ledger.attempts())
        self.assertEqual(total, Decimal(str(stuck["reserved"])) + 50 * self.config.cost("mid", 10, 5))

    async def test_rerun_dry_plan_accepts_legacy_outputs_and_refuses_overwrite(self):
        with self.mocked_pipeline(RecordingJudge('{}')):
            await calibrate(**self.paths, real=True)
        previous = self.paths["output"]
        old = json.loads(previous.read_text())
        for row in old:
            row.pop("attempt_a")
            row.pop("attempt_b")
        previous.write_text(json.dumps(old))
        paths = dict(self.paths, output=self.root / "new.json", rerun_unresolved=previous)
        with patch("benchmark.calibrate_judge.real_provider") as provider:
            plan = await calibrate(**paths)
            with self.assertRaises(ValueError):
                await calibrate(**dict(paths, output=previous))
        self.assertEqual(plan["planned_calls"], 50)
        provider.assert_not_called()
        self.assertFalse(paths["output"].exists())

    def test_genuinely_invalid_ratings_keep_validator_behavior(self):
        pairs = self.pairs()
        for pair in pairs:
            pair.update(judge_a=3, judge_b=3)
        pairs[0]["judge_a"] = "3"
        self.assertEqual(validate_calibration(pairs), dict(validated=False, reason="Invalid or missing ratings"))

    def test_invalid_joins_ratings_and_changed_content_are_rejected(self):
        for change in ("duplicate", "missing", "bad-rating", "stale-answer", "incomplete", "wrong-right"):
            artifact, ratings, mapping = fixture()
            if change == "duplicate":
                ratings[-1] = deepcopy(ratings[0])
            elif change == "missing":
                mapping.pop()
            elif change == "bad-rating":
                ratings[0]["left_rating"] = True
            elif change == "stale-answer":
                ratings[0]["left"] = "changed"
            elif change == "incomplete":
                artifact["records"][0]["completed"] = False
            else:
                mapping[0]["right_condition"] = mapping[0]["left_condition"]
            with self.subTest(change=change), self.assertRaises(ValueError):
                load_pairs(artifact, ratings, mapping)


class CalibrationCLITests(unittest.TestCase):
    def test_cli_dry_run_and_failed_validation_exit_codes(self):
        for real, result, expected in ((False, dict(dry_run=True, pairs=25, planned_calls=50), 0),
                                       (True, dict(validated=False, unknown_outputs=[dict(blind_id="pair")]), 1)):
            with self.subTest(real=real), patch("benchmark.calibrate_judge.calibrate", return_value=result), patch(
                    "sys.stdout", new_callable=io.StringIO) as stdout:
                self.assertEqual(main(["--real"] if real else []), expected)
                self.assertEqual(json.loads(stdout.getvalue()), result)
