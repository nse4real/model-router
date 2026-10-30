"""Offline human-rating application regressions with synthetic artifacts."""
import copy
import unittest
from unittest.mock import patch

from benchmark.evaluate import apply_human
from benchmark.results import CONDITIONS


class EvaluateHumanTests(unittest.TestCase):
    def setUp(self):
        self.ids = [f"fixture-{i:02}" for i in range(3)]
        self.rows = [dict(id=p, split="savings") for p in self.ids]
        self.manifest = dict(sha256="synthetic", human_subset=self.ids)
        dataset = patch("benchmark.evaluate.load_dataset", return_value=([], self.manifest))
        savings = patch("benchmark.evaluate.split", return_value=(self.rows, self.manifest))
        dataset.start()
        savings.start()
        self.addCleanup(dataset.stop)
        self.addCleanup(savings.stop)

    def artifact(self, split="savings", incomplete=()):
        records = [dict(prompt_id=p, condition=c, cost=1, completed=True, status=200, quality=None)
                   for p in self.ids for c in CONDITIONS]
        for record in records:
            if record["prompt_id"] in incomplete and record["condition"] in {"A", "B"}:
                record.update(completed=False, status=503, quality=5)
        return dict(split=split, final=True, dataset_checksum="synthetic", seed=13,
                    data_source="real", records=records)

    def blinded(self, scores):
        mapping, ratings = [], []
        for i, (prompt_id, (a, b)) in enumerate(scores.items()):
            left = "A" if i % 2 == 0 else "B"
            blind_id = f"blind-{prompt_id}"
            mapping.append(dict(prompt_id=prompt_id, blind_id=blind_id, left_condition=left))
            ratings.append(dict(blind_id=blind_id,
                                left_rating=a if left == "A" else b,
                                right_rating=b if left == "A" else a))
        return ratings, mapping

    def test_savings_happy_path_uses_all_prompts_for_cost_and_kept_pairs_for_quality(self):
        artifact = self.artifact(incomplete={"fixture-02"})
        ratings, mapping = self.blinded({"fixture-00": (4, 5), "fixture-01": (3, 3)})
        result = apply_human(artifact, ratings, mapping)
        self.assertEqual(result["human_pairs"], 2)
        self.assertEqual(result["excluded_prompt_ids"], ["fixture-02"])
        self.assertEqual(result["analysis"]["conditions"]["A"]["prompts"], 3)
        self.assertEqual(result["analysis"]["conditions"]["A"]["observed"], 3)
        self.assertEqual(result["analysis"]["conditions"]["A"]["completion_rate"], 2 / 3)
        self.assertEqual(result["analysis"]["conditions"]["A"]["total_cost"], 3)
        self.assertEqual(result["descriptive_quality"]["n_pairs"], 2)
        self.assertEqual(result["descriptive_quality"]["mean_A"], 3.5)
        self.assertEqual(result["descriptive_quality"]["mean_B"], 4)
        self.assertEqual(result["descriptive_quality"]["mean_diff"], .5)
        self.assertEqual((result["descriptive_quality"]["B_higher"],
                          result["descriptive_quality"]["tied"],
                          result["descriptive_quality"]["B_lower"]), (1, 1, 0))
        for record in result["records"]:
            if record["prompt_id"] == "fixture-02" and record["condition"] in {"A", "B"}:
                self.assertIsNone(record["quality"])

    def test_savings_mapping_mismatch_is_rejected(self):
        ratings, mapping = self.blinded({p: (4, 5) for p in self.ids})
        mapping[0]["prompt_id"] = "not-in-the-artifact"
        with self.assertRaisesRegex(ValueError, "Mapping must cover exactly"):
            apply_human(self.artifact(), ratings, mapping)

    def test_savings_bad_ratings_are_rejected(self):
        ratings, mapping = self.blinded({p: (4, 5) for p in self.ids})
        for value in (True, 1.0, 0, 6, None, "4"):
            with self.subTest(value=value):
                invalid = copy.deepcopy(ratings)
                invalid[0]["left_rating"] = value
                with self.assertRaisesRegex(ValueError, "integers 1..5"):
                    apply_human(self.artifact(), invalid, mapping)

    def test_savings_formal_quality_gate_still_requires_exactly_60_pairs(self):
        ratings, mapping = self.blinded({p: (4, 5) for p in self.ids})
        result = apply_human(self.artifact(), ratings, mapping)
        self.assertFalse(result["analysis"]["quality"]["B"]["supported"])
        self.assertEqual(result["analysis"]["quality"]["B"]["reason"],
                         "Need predeclared 60 human pairs")
        self.assertNotIn("mean_difference", result["analysis"]["quality"]["B"])
        self.assertEqual(result["descriptive_quality"]["n_pairs"], 3)

    def test_descriptive_quality_numbers_are_hand_checkable(self):
        ratings, mapping = self.blinded({"fixture-00": (4, 5), "fixture-01": (3, 4),
                                         "fixture-02": (5, 5)})
        result = apply_human(self.artifact(), ratings, mapping)
        quality = result["descriptive_quality"]
        self.assertEqual(quality["n_pairs"], 3)
        self.assertEqual(quality["mean_A"], 4)
        self.assertEqual(quality["mean_B"], 14 / 3)
        self.assertAlmostEqual(quality["mean_diff"], 2 / 3)
        self.assertEqual(quality["ci"], [0, 1])
        self.assertEqual((quality["B_higher"], quality["tied"], quality["B_lower"]), (2, 1, 0))
        self.assertEqual(quality["sign_test_p"], .5)
        self.assertEqual(quality["note"],
                         "Descriptive only; no predeclared rule; no formal quality claim")

    def test_held_out_still_excludes_incomplete_pairs_from_cost_and_completion(self):
        ratings, mapping = self.blinded({"fixture-00": (4, 5), "fixture-01": (3, 3)})
        result = apply_human(self.artifact(split="held-out", incomplete={"fixture-02"}),
                             ratings, mapping)
        self.assertEqual(result["human_pairs"], 2)
        self.assertEqual(result["excluded_prompt_ids"], ["fixture-02"])
        self.assertEqual(result["analysis"]["conditions"]["A"]["prompts"], 2)
        self.assertEqual(result["analysis"]["conditions"]["A"]["observed"], 2)
        self.assertEqual(result["analysis"]["conditions"]["A"]["completion_rate"], 1)
        self.assertEqual(result["analysis"]["conditions"]["A"]["total_cost"], 2)
        self.assertEqual(result["descriptive_quality"]["n_pairs"], 2)


if __name__ == "__main__":
    unittest.main()
