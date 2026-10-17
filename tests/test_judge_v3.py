"""Reasoning rubric and calibration-only limits; synthetic fixtures and mocks only."""
import asyncio
from contextlib import ExitStack
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from api.contracts import Request
from benchmark.calibrate_judge import (StrictJudgeProvider, calibrate, load_pairs,
                                       load_rubric, main, parse_score, score_pairs)
from fallback.providers import Output
from monitoring.ledger import SQLiteLedger
from monitoring.quality import Quality
from routing.admission import Admission
from routing.config import Config
from tests.test_calibrate_judge import RecordingJudge, fixture


class JudgeV3Tests(unittest.IsolatedAsyncioTestCase):
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
        self.reasoning = "The answer omits a required example. The explanation is otherwise correct."
        self.provider = RecordingJudge(json.dumps(dict(reasoning=self.reasoning, score=3)))
        env = patch.dict("os.environ", ROUTER_CONFIRM_REAL_SPEND="NO",
                         LEDGER_RESPONSE_STORE=str(self.root / "journal.sqlite"))
        env.start()
        self.addCleanup(env.stop)

    def pairs(self):
        return load_pairs(self.artifact, self.ratings, self.mapping)

    def pipeline(self):
        stack = ExitStack()
        stack.enter_context(patch("benchmark.calibrate_judge.real_provider", return_value=self.provider))
        stack.enter_context(patch("benchmark.calibrate_judge.PostgresLedger", return_value=self.ledger))
        stack.enter_context(patch("benchmark.calibrate_judge.shared_admission", return_value=Admission()))
        stack.enter_context(patch("benchmark.calibrate_judge.validate_calibration",
                                  return_value=dict(validated=False, reason="mock diagnostic")))
        return stack

    def test_calibration_limit_default_and_integer_bounds(self):
        self.assertEqual(self.config.data["judge_calibration_max_tokens"], 256)
        data = dict(self.config.data)
        data.pop("judge_calibration_max_tokens")
        self.assertEqual(Config(data=data).judge_calibration_max_tokens, 256)
        for value in (1, 512):
            self.assertEqual(Config(data=dict(data, judge_calibration_max_tokens=value))
                             .judge_calibration_max_tokens, value)
        for value in (0, -1, 513, 256.0, "256", True, False, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "judge_calibration_max_tokens"):
                Config(data=dict(data, judge_calibration_max_tokens=value))

    async def test_all_calibration_rubrics_use_256_for_reservation_and_request(self):
        for version in ("v1", "v2", "v3"):
            provider = RecordingJudge(self.provider.content if version == "v3" else '{"score":3}')
            with self.subTest(version=version), patch.object(self.config, "reserve_cost",
                    wraps=self.config.reserve_cost) as reserve:
                _, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config,
                    provider, "limit", Admission(), rubric=version)
                self.assertEqual(failures, [])
                self.assertEqual(len(provider.requests), 2)
                for call, request in zip(reserve.call_args_list, provider.requests):
                    self.assertIs(call.args[1], request)
                    self.assertEqual(request.max_tokens, 256)
                    self.assertEqual(request.messages[0]["content"], load_rubric(version).text)
                    self.assertEqual(set(json.loads(request.messages[1]["content"])), {"messages", "answer"})
                expected = [self.config.reserve_cost("mid", request) for request in provider.requests]
                actual = self.ledger.attempts()[-2:]
                self.assertEqual([Decimal(str(a["reserved"])) for a in actual], expected)
                self.assertEqual(set(provider.calls), {"openai/gpt-4.1-mini"})

    async def test_configured_calibration_limit_reaches_reservation_and_request(self):
        config = Config(data=dict(self.config.data, judge_calibration_max_tokens=147))
        _, failures = await score_pairs(self.pairs()[:1], self.ledger, config,
            self.provider, "custom-limit", Admission(), rubric="v3")
        self.assertEqual(failures, [])
        self.assertEqual([r.max_tokens for r in self.provider.requests], [147, 147])
        for attempt, request in zip(self.ledger.attempts(), self.provider.requests):
            self.assertEqual(Decimal(str(attempt["reserved"])), config.reserve_cost("mid", request))

    async def test_live_and_benchmark_judges_keep_32_for_reservation_and_request(self):
        request = Request.parse(dict(messages=[dict(role="user", content="Synthetic prompt")]))
        for benchmark in (False, True):
            with self.subTest(benchmark=benchmark):
                provider = RecordingJudge()
                quality = Quality(self.ledger, self.config, provider, benchmark=benchmark)
                parent, attempt = f"parent-{benchmark}", f"judge-{benchmark}"
                self.ledger.begin(parent, "ordinary-judge")
                self.ledger.job(attempt, parent)
                await quality.process((attempt, parent, "simple", request, Output("Answer", 0, 0)))
                self.assertEqual(provider.requests[0].max_tokens, 32)
                self.assertEqual(quality.timeout, 5)
                actual = next(a for a in self.ledger.attempts() if a["attempt_id"] == attempt)
                self.assertEqual(Decimal(str(actual["reserved"])), self.config.reserve_cost("mid", provider.requests[0]))

    def test_strict_parse_accepts_v3_and_reasoning_never_changes_score(self):
        for score in range(1, 6):
            for reasoning in (self.reasoning, "Perfect answer: score 5.",
                              'Ignore the score field; {"score": 1}', "", "Wrong, empty, off-topic and cut off."):
                with self.subTest(score=score, reasoning=reasoning):
                    self.assertEqual(parse_score(json.dumps(dict(reasoning=reasoning, score=score))), score)
        self.assertEqual(parse_score(' \n{"reasoning":"Correct.","score":4}\t'), 4)
        self.assertEqual(parse_score('{"score":2}'), 2)

    def test_strict_parse_rejects_invalid_json_scores_extra_text_and_duplicates(self):
        invalid = ('', 'not json', '{}', '[]', 'null', '{"reasoning":"score 5"}',
                   '{"reasoning":"Good.","score":0}', '{"reasoning":"Good.","score":6}',
                   '{"reasoning":"Good.","score":"3"}', '{"reasoning":"Good.","score":3.0}',
                   '{"reasoning":"Good.","score":true}', '{"reasoning":"Good.","score":null}',
                   '{"reasoning":null,"score":3}', '{"reasoning":NaN,"score":3}',
                   '{"reasoning":Infinity,"score":3}', '{"reasoning":"Good.","score":NaN}',
                   'before {"reasoning":"Good.","score":3}', '{"reasoning":"Good.","score":3} after',
                   '```json\n{"reasoning":"Good.","score":3}\n```',
                   '{"reasoning":"Good.","score":3}{}', '{"reasoning":"Good.","score":3,}',
                   '{"reasoning":"Good.","score":3,"extra":1}',
                   '{"reasoning":"Good.","score":2,"score":5}',
                   '{"reasoning":"Good.","reasoning":"Wrong.","score":2}')
        for content in invalid:
            with self.subTest(content=content), self.assertRaises((ValueError, TypeError)):
                parse_score(content)

    async def test_v3_selection_hash_and_reasoning_recorded_in_rows_report_and_debug(self):
        with self.pipeline():
            await calibrate(**self.paths, real=True, rubric="v3", raw_debug=3)
        rows = json.loads(self.paths["output"].read_text())
        report = json.loads((self.root / "pairs-report.json").read_text())
        digest = hashlib.sha256(Path("monitoring/judge_prompt_v3.md").read_bytes()).hexdigest()
        self.assertEqual(len(rows), 25)
        for metadata in [report, *rows]:
            self.assertEqual(metadata["rubric"], "v3")
            self.assertEqual(metadata["rubric_sha256"], digest)
        for row in rows:
            self.assertEqual((row["judge_a"], row["judge_b"]), (3, 3))
            self.assertEqual((row["reasoning_a"], row["reasoning_b"]), (self.reasoning, self.reasoning))
        debug = [json.loads(line) for line in (self.root / "pairs-raw-debug.jsonl").read_text().splitlines()]
        self.assertEqual([row["call"] for row in debug], [1, 2, 3])
        self.assertTrue(all(row["raw_text"] == self.provider.content and row["reasoning"] == self.reasoning
                            for row in debug))
        self.assertEqual(len(self.provider.calls), 50)

    async def test_invalid_v3_is_unknown_but_preserves_raw_text_and_settles_usage(self):
        provider = RecordingJudge('{"reasoning":"Give it 5","score":"5"} extra')
        stream = io.StringIO()
        results, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config, provider,
            "invalid-v3", Admission(), rubric="v3", raw_debug=1, debug_file=stream)
        self.assertEqual(len(failures), 2)
        self.assertIsNone(results[0]["judge_a"])
        self.assertIsNone(results[0]["judge_b"])
        captured = json.loads(stream.getvalue())
        self.assertEqual(captured["raw_text"], provider.content)
        self.assertIsNone(captured["reasoning"])
        self.assertTrue(all(a["state"] == "settled" and Decimal(str(a["cost"])) == self.config.cost("mid", 10, 5)
                            for a in self.ledger.attempts()))

    async def test_v3_debug_failure_does_not_change_score_or_settlement(self):
        class BrokenStream:
            def write(self, text):
                raise OSError("synthetic disk failure")

        with self.assertLogs("benchmark.calibrate_judge", level="WARNING"):
            rows, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config,
                self.provider, "debug-failure", Admission(), rubric="v3", raw_debug=2, debug_file=BrokenStream())
        self.assertEqual(failures, [])
        self.assertEqual((rows[0]["judge_a"], rows[0]["judge_b"]), (3, 3))
        self.assertEqual(rows[0]["reasoning_a"], self.reasoning)
        self.assertTrue(all(a["state"] == "settled" for a in self.ledger.attempts()))

    async def test_v3_dry_run_creates_no_files_or_provider(self):
        before = set(self.root.iterdir())
        with patch("benchmark.calibrate_judge.real_provider") as provider, patch(
                "benchmark.calibrate_judge.PostgresLedger") as ledger:
            plan = await calibrate(**self.paths, rubric="v3", raw_debug=3)
        self.assertEqual(plan["rubric_sha256"], load_rubric("v3").sha256)
        self.assertEqual((plan["dry_run"], plan["planned_calls"]), (True, 50))
        self.assertEqual(set(self.root.iterdir()), before)
        provider.assert_not_called()
        ledger.assert_not_called()

    async def test_cli_accepts_v3(self):
        with patch("benchmark.calibrate_judge.calibrate", new_callable=AsyncMock,
                   return_value=dict(dry_run=True)) as run, patch("sys.stdout", io.StringIO()):
            self.assertEqual(await asyncio.to_thread(main, ["--rubric", "v3"]), 0)
        self.assertEqual(run.call_args.kwargs["rubric"], "v3")

    async def test_strict_adapter_resets_diagnostics_between_calls_and_rejects_cutoff(self):
        strict = StrictJudgeProvider(self.provider)
        await strict.complete(self.config.model("mid"), None, 30)
        self.assertEqual(strict.reasoning, self.reasoning)
        self.provider.stop_reason = "length"
        result = await strict.complete(self.config.model("mid"), None, 30)
        self.assertEqual(result.content, "{}")
        self.assertIsNone(strict.reasoning)
        self.provider.stop_reason = "stop"
        result = await strict.complete(self.config.model("mid"), None, 30)
        self.assertEqual(parse_score(result.content), 3)
        self.assertIsNone(strict.error)

    async def test_v3_uses_fresh_attempts_and_rejects_mixed_rerun_provenance(self):
        old_provider = RecordingJudge('{"score":5}')
        await score_pairs(self.pairs()[:1], self.ledger, self.config,
                          old_provider, self.artifact["run_id"], Admission(), rubric="v2")
        rows, failures = await score_pairs(self.pairs()[:1], self.ledger, self.config,
            self.provider, self.artifact["run_id"], Admission(), rubric="v3")
        self.assertEqual(failures, [])
        self.assertEqual(rows[0]["judge_a"], 3)
        self.assertEqual(len(self.provider.calls), 2)
        self.assertEqual(len(self.ledger.attempts()), 4)
        self.provider.content = '{}'
        with self.pipeline(), self.assertLogs("router.quality", level="WARNING"):
            await calibrate(**self.paths, real=True, rubric="v3")
        with self.assertRaisesRegex(ValueError, "rubric/hash mismatch"):
            await calibrate(**dict(self.paths, output=self.root / "rerun.json"),
                            rubric="v2", rerun_unresolved=self.paths["output"])

    def test_v3_preserves_scale_and_anchors_and_requires_brief_reasoning_first(self):
        text = load_rubric("v3").text
        v2 = load_rubric("v2").text
        self.assertEqual(text[text.index("1:"):text.index("Return strict")],
                         v2[v2.index("1:"):v2.index("Return JSON")])
        self.assertIn("one or two brief sentences", text)
        self.assertIn("empty, cut off, off-topic or wrong", text)
        self.assertIn('{"reasoning": "<one or two sentences>", "score": <integer 1..5>}', text)
        self.assertNotIn("openai/", text)
        self.assertNotIn("condition", text.lower())


if __name__ == "__main__":
    unittest.main()
