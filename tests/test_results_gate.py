"""Offline source-gate regressions using synthetic scores and mocked execution."""
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from benchmark.results import CONDITIONS, analyse
from benchmark.run import build_parser, main


def records(n=60):
    return [dict(prompt_id=f"fixture-{i:02}", condition=c, cost="2",
                 completed=True, quality=5) for i in range(n) for c in CONDITIONS]


class ResultsGateTests(unittest.TestCase):
    def analyse(self, rows=None, n=60, **kwargs):
        return analyse(records(n) if rows is None else rows, n, resamples=20,
                       data_source="real", **kwargs)

    def assert_withheld(self, report, reason):
        for claim in report["quality"].values():
            self.assertFalse(claim["supported"])
            self.assertIn(reason, claim["reason"])
            self.assertNotIn("mean_difference", claim)
        for condition in report["conditions"].values():
            self.assertIsNone(condition["accepted"])
            self.assertIsNone(condition["cost_per_accepted_response"])
            self.assertIn(reason, condition["accepted_reason"])

    def test_caller_label_cannot_validate_judge_even_with_60_pairs(self):
        report = self.analyse(quality_source="validated_judge", judge_validated=False)
        self.assert_withheld(report, "judge withheld: not validated")
        self.assertEqual(report["quality_source"], "judge withheld: not validated")

    def test_diagnostic_judge_scores_cannot_populate_acceptance_or_claims(self):
        for source in ("diagnostic_real_judge", "diagnostic_mock_judge"):
            with self.subTest(source=source):
                report = self.analyse(quality_source=source)
                self.assert_withheld(report, "judge withheld: not validated")
                self.assertEqual(report["quality_source"], "judge withheld: not validated")
                self.assertEqual(report["conditions"]["A"]["total_cost"], 120)
                self.assertEqual(report["savings_measured"]["B"]["ratio"], 0)

    def test_validation_requires_boolean_true(self):
        for flag in (None, 0, 1, "False", "True"):
            with self.subTest(flag=flag):
                self.assert_withheld(self.analyse(quality_source="validated_judge",
                                                 judge_validated=flag), "not validated")

    def test_validated_judge_uses_all_prompts_in_every_condition(self):
        report = self.analyse(n=4, quality_source="validated_judge", judge_validated=True)
        for claim in report["quality"].values():
            self.assertTrue(claim["supported"])
            self.assertEqual(claim["n"], 4)
        for condition in report["conditions"].values():
            self.assertEqual(condition["accepted"], 4)
            self.assertEqual(condition["cost_per_accepted_response"], 2)
            self.assertIsNone(condition["accepted_reason"])

    def test_validated_judge_still_requires_complete_scores(self):
        rows = records(4)
        rows[1]["quality"] = None  # B on the first prompt
        report = self.analyse(rows, n=4, quality_source="validated_judge", judge_validated=True)
        self.assertEqual(report["quality"]["B"]["reason"], "Missing judge scores")
        self.assertIsNone(report["conditions"]["B"]["accepted"])
        self.assertIsNone(report["conditions"]["B"]["cost_per_accepted_response"])
        self.assertTrue(report["quality"]["C-cold"]["supported"])

    def test_human_claims_and_acceptance_are_independent_of_judge_gate(self):
        for flag in (False, True):
            with self.subTest(flag=flag):
                report = self.analyse(quality_source="human", judge_validated=flag)
                self.assertTrue(report["quality"]["B"]["supported"])
                self.assertEqual(report["quality"]["B"]["n"], 60)
                self.assertEqual(report["quality_source"], "human")
                for c in ("A", "B"):
                    self.assertEqual(report["conditions"][c]["accepted"], 60)
                    self.assertEqual(report["conditions"][c]["cost_per_accepted_response"], 2)
                for c in ("C-cold", "C-warm"):
                    self.assertFalse(report["quality"][c]["supported"])
                    self.assertIsNone(report["conditions"][c]["accepted"])

    def test_human_claim_uses_only_rated_subset_including_rated_failures(self):
        for flag in (False, True):
            with self.subTest(flag=flag):
                rows = records(62)
                for row in rows:
                    if row["condition"].startswith("C") or row["prompt_id"] >= "fixture-60":
                        row["quality"] = None
                    if row["prompt_id"] == "fixture-61":
                        row["completed"] = False  # Unrated failures are outside the subset.
                rows[0].update(completed=False, quality=1)  # A rated failure stays in it.
                report = self.analyse(rows, n=62, quality_source="human", judge_validated=flag)
                self.assertEqual(report["quality"]["B"]["n"], 60)
                self.assertAlmostEqual(report["quality"]["B"]["mean_difference"], 4 / 60)
                self.assertEqual(report["quality"]["B"]["sensitivity_completed_only"]["n"], 59)

    def test_human_subset_size_cannot_be_bypassed_by_judge_validation(self):
        for flag in (False, True):
            with self.subTest(flag=flag):
                report = self.analyse(n=59, quality_source="human", judge_validated=flag)
                self.assertEqual(report["quality"]["B"]["reason"], "Need predeclared 60 human pairs")

    def test_pending_human_wording_and_acceptance_gate(self):
        for kwargs in ({}, {"quality_source": "pending"}, {"quality_source": "human pending"}):
            with self.subTest(kwargs=kwargs):
                report = self.analyse(**kwargs)
                self.assertEqual(report["quality_source"], "human pending")
                self.assert_withheld(report, "human pending")

    def test_incomplete_coverage_still_withholds_human_and_judge_claims(self):
        for source in ("human", "validated_judge"):
            with self.subTest(source=source):
                report = self.analyse(records()[:-1], quality_source=source, judge_validated=True)
                self.assertFalse(report["coverage_complete"])
                self.assertTrue(all(q.get("reason") == "Incomplete coverage"
                                    or "Human ratings unavailable" in q.get("reason", "")
                                    for q in report["quality"].values()))
                self.assertTrue(all(not q["supported"] for q in report["quality"].values()))


class RunnerSourceTests(unittest.IsolatedAsyncioTestCase):
    async def test_runner_metadata_and_analysis_say_human_pending(self):
        manifest = dict(sha256="synthetic", version="fixture", human_subset=[],
                        reproducibility_subset=[])
        ledger = MagicMock()
        ledger.barrier.return_value = True
        with tempfile.TemporaryDirectory() as directory:
            args = build_parser().parse_args(["--storage", "test-double", "--output",
                                               str(Path(directory) / "run.json")])
            with patch("benchmark.run.split", return_value=([dict(id="fixture")], manifest)), patch(
                    "benchmark.run.SQLiteLedger", return_value=ledger), patch(
                    "benchmark.run.Router"), patch("benchmark.run.git_commit", return_value="fixture"), patch(
                    "benchmark.run.run_conditions", new=AsyncMock(return_value=("run", records(1)))), patch(
                    "benchmark.run.write_results", return_value={"final": True}) as write, patch(
                    "benchmark.run.totals", return_value={}), patch(
                    "benchmark.run.real_provider", side_effect=AssertionError("Real calls forbidden")), redirect_stdout(io.StringIO()):
                await main(args)
        metadata = write.call_args.args[3]
        self.assertEqual(metadata["quality_source"], "human pending")
        self.assertEqual(metadata["analysis"]["quality_source"], "human pending")
        self.assertFalse(metadata["judge_validated"])
