"""Rubric provenance and raw judge diagnostics; synthetic data and mock calls only."""
import asyncio
from contextlib import ExitStack
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch
from uuid import NAMESPACE_URL, uuid5

from benchmark.calibrate_judge import (StrictJudgeProvider, calibrate, load_pairs,
                                       load_rubric, main, score_pairs)
from monitoring.ledger import SQLiteLedger
from monitoring.quality import Quality
from routing.admission import Admission
from routing.config import Config
from tests.test_calibrate_judge import RecordingJudge, fixture


class JudgeV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.artifact, self.ratings, self.mapping = fixture()
        self.paths = {}
        for name, data in (("artifact", self.artifact), ("ratings", self.ratings),
                           ("mapping", self.mapping)):
            path = self.root / (name + ".json")
            path.write_text(json.dumps(data), encoding="utf8")
            self.paths[name] = path
        self.paths["output"] = self.root / "pairs.json"
        self.ledger = SQLiteLedger()
        self.addCleanup(self.ledger.close)
        self.config = Config()
        self.provider = RecordingJudge('  {"score":2}\n')
        self.addCleanup(patch.stopall)
        patch.dict("os.environ", LEDGER_RESPONSE_STORE=str(self.root / "journal.sqlite"),
                   ROUTER_CONFIRM_REAL_SPEND="NO").start()

    def pairs(self):
        return load_pairs(self.artifact, self.ratings, self.mapping)

    def pipeline(self, provider=None, ledger=None):
        stack = ExitStack()
        stack.enter_context(patch("benchmark.calibrate_judge.real_provider",
                                  return_value=provider or self.provider))
        stack.enter_context(patch("benchmark.calibrate_judge.PostgresLedger",
                                  return_value=ledger or self.ledger))
        stack.enter_context(patch("benchmark.calibrate_judge.shared_admission", return_value=Admission()))
        stack.enter_context(patch("benchmark.calibrate_judge.validate_calibration",
                                  return_value=dict(validated=False, reason="mock diagnostic")))
        return stack

    async def test_rubric_selection_reaches_system_message_without_metadata(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version):
                provider = RecordingJudge()
                _, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config,
                    provider, "selection", Admission(), rubric=version)
                self.assertEqual(failures, [])
                self.assertEqual(len(provider.requests), 2)
                for request in provider.requests:
                    self.assertEqual(request.messages[0]["content"], load_rubric(version).text)
                    payload = json.loads(request.messages[1]["content"])
                    self.assertEqual(set(payload), {"messages", "answer"})
                    self.assertEqual(request.max_tokens, 256)

    def test_rubric_hash_uses_exact_file_bytes(self):
        for version in ("v1", "v2"):
            path = Path("monitoring") / f"judge_prompt_{version}.md"
            rubric = load_rubric(version)
            self.assertEqual(rubric.sha256, hashlib.sha256(path.read_bytes()).hexdigest())
            self.assertEqual(rubric.text, path.read_text(encoding="utf8"))

    async def test_rubric_and_hash_recorded_in_every_output_row_and_report(self):
        for version in ("v1", "v2"):
            with self.subTest(version=version), self.pipeline():
                output = self.root / (version + ".json")
                await calibrate(**dict(self.paths, output=output), real=True, rubric=version)
                rows = json.loads(output.read_text())
                report = json.loads(output.with_name(version + "-report.json").read_text())
                for metadata in [report, *rows]:
                    for key, value in load_rubric(version).metadata.items():
                        self.assertEqual(metadata[key], value)
                self.assertEqual(len(rows), 25)
                self.assertEqual(len(self.provider.calls), 50 if version == "v1" else 100)

    async def test_default_v1_preserves_request_and_attempt_identity(self):
        await score_pairs(self.pairs()[:1], self.ledger, self.config, self.provider,
                          "legacy", Admission())
        rubric = Path("monitoring/judge_prompt_v1.md").read_text()
        self.assertEqual(self.provider.requests[0].messages[0]["content"], rubric)
        attempt = str(uuid5(NAMESPACE_URL, "legacy:source-0-A:judge-calibration-v1"))
        self.assertIn(attempt, {r["attempt_id"] for r in self.ledger.attempts()})
        self.assertIsNone(Quality(self.ledger, self.config, self.provider).rubric)
        with patch("benchmark.calibrate_judge.real_provider") as provider:
            plan = await calibrate(**self.paths)
        provider.assert_not_called()
        self.assertEqual(plan, dict(dry_run=True, pairs=25, planned_calls=50))

    async def test_cli_defaults_and_explicit_selection(self):
        for args, version, count in (([], "v1", 0), (["--rubric", "v2", "--raw-debug", "3"], "v2", 3)):
            with self.subTest(args=args), patch("benchmark.calibrate_judge.calibrate",
                    new_callable=AsyncMock, return_value=dict(dry_run=True)) as run, patch("sys.stdout", io.StringIO()):
                self.assertEqual(await asyncio.to_thread(main, args), 0)
                self.assertEqual(run.call_args.kwargs["rubric"], version)
                self.assertEqual(run.call_args.kwargs["raw_debug"], count)

    async def test_raw_debug_writes_first_n_raw_text_without_changing_scores_or_costs(self):
        with self.pipeline():
            await calibrate(**self.paths, real=True, rubric="v2", raw_debug=3)
        captured = [json.loads(line) for line in (self.root / "pairs-raw-debug.jsonl").read_text().splitlines()]
        self.assertEqual([r["call"] for r in captured], [1, 2, 3])
        self.assertEqual([r["side"] for r in captured], ["a", "b", "a"])
        self.assertTrue(all(r["raw_text"] == '  {"score":2}\n' for r in captured))
        self.assertTrue(all(r["attempt_id"] and r["blind_id"] for r in captured))
        baseline = SQLiteLedger()
        self.addCleanup(baseline.close)
        with self.subTest(baseline=True):
            output = self.root / "baseline.json"
            provider = RecordingJudge(self.provider.content)
            with self.pipeline(provider, baseline):
                await calibrate(**dict(self.paths, output=output), real=True, rubric="v2")
            self.assertEqual(json.loads(self.paths["output"].read_text()), json.loads(output.read_text()))
            for ledger in (self.ledger, baseline):
                self.assertTrue(all(a["purpose"] == "evaluation" and a["state"] == "settled"
                                    for a in ledger.attempts()))
            self.assertEqual(sum(a["cost"] for a in self.ledger.attempts()),
                             sum(a["cost"] for a in baseline.attempts()))
        self.assertEqual(len(self.provider.calls), 50)
        self.assertFalse((self.root / "baseline-raw-debug.jsonl").exists())

    async def test_raw_debug_preserves_malformed_text_and_unknown_settled_usage(self):
        stream = io.StringIO()
        provider = RecordingJudge('not json\n')
        results, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config,
            provider, "malformed", Admission(), rubric="v2", raw_debug=1, debug_file=stream)
        self.assertEqual(json.loads(stream.getvalue())["raw_text"], 'not json\n')
        self.assertEqual(len(failures), 2)
        self.assertIsNone(results[0]["judge_a"])
        self.assertIsNone(results[0]["judge_b"])
        self.assertTrue(all(a["state"] == "settled" and a["cost"] > 0 for a in self.ledger.attempts()))

    async def test_debug_write_failure_does_not_change_score_or_settlement(self):
        class BrokenStream:
            def write(self, text):
                raise OSError("synthetic disk error")

        with self.assertLogs("benchmark.calibrate_judge", level="WARNING"):
            results, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config,
                self.provider, "disk-error", Admission(), raw_debug=2, debug_file=BrokenStream())
        self.assertEqual(failures, [])
        self.assertEqual((results[0]["judge_a"], results[0]["judge_b"]), (2, 2))
        self.assertTrue(all(a["state"] == "settled" for a in self.ledger.attempts()))

    async def test_raw_debug_preserves_abnormal_stop_response_before_rejection(self):
        stream = io.StringIO()
        provider = RecordingJudge('{"score":5}', stop_reason="length")
        strict = StrictJudgeProvider(provider, raw_debug=1, debug_file=stream)
        result = await strict.complete(self.config.model("mid"), None, 30)
        self.assertEqual(json.loads(stream.getvalue())["raw_text"], '{"score":5}')
        self.assertEqual(result.content, "{}")
        self.assertEqual((result.input_tokens, result.output_tokens), (10, 5))
        self.assertIsNotNone(strict.error)

    async def test_dry_run_v2_with_debug_creates_no_files_or_provider(self):
        before = set(self.root.iterdir())
        with patch("benchmark.calibrate_judge.real_provider") as provider, patch(
                "benchmark.calibrate_judge.PostgresLedger") as ledger, patch(
                "benchmark.calibrate_judge.shared_admission") as admission:
            plan = await calibrate(**self.paths, rubric="v2", raw_debug=3)
        self.assertEqual(plan["planned_calls"], 50)
        self.assertTrue(plan["dry_run"])
        self.assertEqual(plan["rubric_sha256"], load_rubric("v2").sha256)
        self.assertEqual(set(self.root.iterdir()), before)
        provider.assert_not_called()
        ledger.assert_not_called()
        admission.assert_not_called()

    async def test_existing_debug_file_is_never_overwritten(self):
        debug = self.root / "pairs-raw-debug.jsonl"
        debug.write_text("keep")
        with patch("benchmark.calibrate_judge.real_provider") as provider:
            with self.assertRaises(FileExistsError):
                await calibrate(**self.paths, real=True, rubric="v2", raw_debug=1)
        provider.assert_not_called()
        self.assertEqual(debug.read_text(), "keep")
        self.assertFalse(self.paths["output"].exists())

    async def test_debug_creation_race_rolls_back_before_ledger_or_dispatch(self):
        debug = self.root / "pairs-raw-debug.jsonl"
        original = Path.open

        def racing_open(path, *args, **kwargs):
            if path == debug and args and args[0] == "x":
                with original(path, "w", encoding="utf8") as stream:
                    stream.write("other process")
            return original(path, *args, **kwargs)

        with patch("benchmark.calibrate_judge.real_provider", return_value=self.provider), patch(
                "benchmark.calibrate_judge.PostgresLedger") as ledger, patch.object(Path, "open", racing_open):
            with self.assertRaises(FileExistsError):
                await calibrate(**self.paths, real=True, raw_debug=1)
        ledger.assert_not_called()
        self.assertEqual(self.provider.calls, [])
        self.assertEqual(debug.read_text(), "other process")
        self.assertFalse(self.paths["output"].exists())
        self.assertFalse((self.root / "pairs-report.json").exists())

    async def test_real_spend_guard_remains_required_with_v2_and_debug(self):
        with patch("benchmark.calibrate_judge.PostgresLedger") as ledger:
            with self.assertRaisesRegex(ValueError, "ROUTER_CONFIRM_REAL_SPEND"):
                await calibrate(**self.paths, real=True, rubric="v2", raw_debug=2)
        ledger.assert_not_called()
        self.assertFalse(self.paths["output"].exists())
        self.assertFalse((self.root / "pairs-raw-debug.jsonl").exists())

    async def test_v2_cannot_reuse_cached_v1_scores_and_remains_idempotent(self):
        for version, value in (("v1", 2), ("v2", 4), ("v2", 4)):
            provider = RecordingJudge(json.dumps(dict(score=value)))
            results, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config,
                provider, "shared-run", Admission(), rubric=version)
            self.assertEqual(failures, [])
            self.assertEqual((results[0]["judge_a"], results[0]["judge_b"]), (value, value))
        self.assertEqual(len(self.ledger.attempts()), 4)
        self.assertEqual(len(provider.calls), 0)

    async def test_unresolved_rerun_rejects_different_rubric_or_hash(self):
        with self.pipeline(RecordingJudge("{}")):
            await calibrate(**self.paths, real=True, rubric="v2")
        previous = self.paths["output"]
        paths = dict(self.paths, output=self.root / "rerun.json", rerun_unresolved=previous)
        with self.assertRaisesRegex(ValueError, "rubric/hash mismatch"):
            await calibrate(**paths, rubric="v1")
        report = self.root / "pairs-report.json"
        metadata = json.loads(report.read_text())
        metadata["rubric_sha256"] = "0" * 64
        report.write_text(json.dumps(metadata))
        with self.assertRaisesRegex(ValueError, "rubric/hash mismatch"):
            await calibrate(**paths, rubric="v2")
        self.assertFalse(paths["output"].exists())

    async def test_invalid_rubric_and_negative_debug_fail_before_provider(self):
        with patch("benchmark.calibrate_judge.real_provider") as provider:
            for options in (dict(rubric="../v1"), dict(raw_debug=-1), dict(raw_debug=True)):
                with self.subTest(options=options), self.assertRaises(ValueError):
                    await calibrate(**self.paths, **options)
        provider.assert_not_called()

    def test_v2_contains_truncation_rule_constraints_and_task_examples(self):
        text = load_rubric("v2").text
        self.assertIn("Empty, off-topic, wrong, or truncated answers must score 1 or 2", text)
        self.assertIn("each stated constraint", text)
        self.assertIn("average of 12, 18, and 24", text)
        self.assertIn("cancellation", text)
        self.assertIn('Return JSON only: {"score": integer}', text)
        self.assertNotIn("2 + 2", text)
        self.assertNotIn("openai/", text)


if __name__ == "__main__":
    unittest.main()
