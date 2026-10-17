"""Offline blinded-export regressions using only synthetic, mocked dataset rows."""
from collections import Counter
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from benchmark.ratings import export_ratings


class RatingsCompletePairsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        self.rows = [dict(id=f"fixture-{i:02}", prompt=f"Synthetic prompt {i}",
                          split="calibration", tier=("simple", "medium", "complex")[i % 3],
                          task_type=("general", "reasoning", "code", "maths")[i % 4])
                     for i in range(60)]
        self.artifact = {"records": [dict(prompt_id=row["id"], condition=condition,
                                         content=f"Synthetic {condition} answer {row['id']}",
                                         completed=True, status=200)
                                     for row in self.rows for condition in ("A", "B")]}
        dataset = patch("benchmark.ratings.load_dataset", return_value=(
            self.rows, {"human_subset": [row["id"] for row in self.rows]}))
        self.dataset = dataset.start()
        self.addCleanup(dataset.stop)
        self.next_output = 0

    def export(self, artifact=None, **kwargs):
        output = self.directory / str(self.next_output)
        self.next_output += 1
        result = export_ratings(self.artifact if artifact is None else artifact, output,
                                mode=kwargs.pop("mode", "calibration"), **kwargs)
        public = json.loads((output / "ratings-blinded.json").read_text(encoding="utf8"))
        private = json.loads((output / "mapping-private.json").read_text(encoding="utf8"))
        return result, public, private

    def test_incomplete_pair_is_never_selected(self):
        for condition in ("A", "B"):
            for content in ("", "Truncated synthetic answer"):
                with self.subTest(condition=condition, content=content):
                    artifact = copy.deepcopy(self.artifact)
                    artifact["records"] = artifact["records"][:6]
                    failed = next(r for r in artifact["records"]
                                  if r["prompt_id"] == "fixture-00" and r["condition"] == condition)
                    failed.update(completed=False, content=content)
                    result, public, private = self.export(artifact)
                    self.assertEqual(result["pairs"], 2)
                    self.assertEqual({r["prompt_id"] for r in private}, {"fixture-01", "fixture-02"})
                    self.assertTrue(all(r["left"] and r["right"] for r in public))

    def test_both_sides_require_explicit_completion_and_http_200(self):
        for condition in ("A", "B"):
            for field, value in (("status", 503), ("status", "200"), ("status", None),
                                 ("completed", None), ("completed", 1), ("completed", "true")):
                with self.subTest(condition=condition, field=field, value=value):
                    artifact = copy.deepcopy(self.artifact)
                    artifact["records"] = artifact["records"][:4]
                    failed = next(r for r in artifact["records"]
                                  if r["prompt_id"] == "fixture-00" and r["condition"] == condition)
                    if value is None:
                        del failed[field]
                    else:
                        failed[field] = value
                    result, _, private = self.export(artifact)
                    self.assertEqual(result["pairs"], 1)
                    self.assertEqual([r["prompt_id"] for r in private], ["fixture-01"])

    def test_missing_side_and_other_splits_are_not_candidates(self):
        artifact = copy.deepcopy(self.artifact)
        artifact["records"] = artifact["records"][:5]  # The third prompt has only A.
        self.rows[0]["split"] = "dev"
        self.rows[3]["split"] = "held-out"
        result, _, private = self.export(artifact)
        self.assertEqual(result["pairs"], 1)
        self.assertEqual([r["prompt_id"] for r in private], ["fixture-01"])

    def test_private_mapping_records_completion_and_public_schema_stays_blinded(self):
        _, public, private = self.export()
        public_by_id = {row["blind_id"]: row for row in public}
        self.assertEqual(len(public_by_id), 25)
        for row in private:
            self.assertEqual(set(row), {"blind_id", "prompt_id", "left_condition", "tier",
                                        "task_type", "left_completed", "right_completed"})
            self.assertIs(row["left_completed"], True)
            self.assertIs(row["right_completed"], True)
            blinded = public_by_id[row["blind_id"]]
            self.assertEqual(set(blinded), {"blind_id", "prompt", "left", "right",
                                           "left_rating", "right_rating"})
            right_condition = "B" if row["left_condition"] == "A" else "A"
            self.assertEqual(blinded["left"], f"Synthetic {row['left_condition']} answer {row['prompt_id']}")
            self.assertEqual(blinded["right"], f"Synthetic {right_condition} answer {row['prompt_id']}")
            self.assertIsNone(blinded["left_rating"])
            self.assertIsNone(blinded["right_rating"])

    def test_missing_content_never_adds_a_public_completion_label(self):
        artifact = {"records": [dict(r) for r in self.artifact["records"][:2]]}
        for record in artifact["records"]:
            del record["content"]
        result, public, _ = self.export(artifact)
        self.assertEqual(result["pairs"], 1)
        self.assertEqual(public[0]["left"], "")
        self.assertEqual(public[0]["right"], "")
        self.assertNotIn("completed", json.dumps(public))

    def test_existing_output_directory_is_refused_without_changes(self):
        for populated in (False, True):
            with self.subTest(populated=populated):
                output = self.directory / str(populated)
                output.mkdir()
                if populated:
                    for name in ("ratings-blinded.json", "mapping-private.json"):
                        (output / name).write_bytes(b"existing synthetic export")
                before = {p.name: p.read_bytes() for p in output.iterdir()}
                with self.assertRaisesRegex(FileExistsError, "Refusing to overwrite"):
                    export_ratings(self.artifact, output, mode="calibration")
                self.assertEqual({p.name: p.read_bytes() for p in output.iterdir()}, before)
        self.dataset.assert_not_called()

    def test_output_directory_creation_race_is_refused(self):
        output = self.directory / "race"
        original = Path.mkdir

        def competing_mkdir(path, *args, **kwargs):
            if path == output:
                original(path)
                (path / "ratings-blinded.json").write_bytes(b"competing export")
            return original(path, *args, **kwargs)

        with patch.object(Path, "mkdir", new=competing_mkdir), self.assertRaises(FileExistsError):
            export_ratings(self.artifact, output, mode="calibration")
        self.assertEqual((output / "ratings-blinded.json").read_bytes(), b"competing export")
        self.assertFalse((output / "mapping-private.json").exists())

    def test_fewer_than_25_complete_pairs_export_all_and_report_actual_count(self):
        for count in (1, 7, 24):
            with self.subTest(count=count):
                artifact = {"records": self.artifact["records"][:2 * count]}
                result, public, private = self.export(artifact, seed=42)
                self.assertEqual(result, dict(pairs=count, individual_ratings=2 * count, seed=42))
                self.assertEqual(len(public), count)
                self.assertEqual({r["prompt_id"] for r in private},
                                 {r["id"] for r in self.rows[:count]})

    def test_no_complete_pairs_raise_clear_error_without_creating_output(self):
        for records in ([], [dict(r, completed=False) for r in self.artifact["records"]]):
            with self.subTest(empty=not records):
                output = self.directory / "no-pairs"
                with self.assertRaisesRegex(ValueError, "at least 1 complete A/B pair"):
                    export_ratings({"records": records}, output, mode="calibration")
                self.assertFalse(output.exists())

    def test_seed_is_repeatable_and_selection_is_stratified_and_capped(self):
        first = self.export(seed=13)
        self.assertEqual(first, self.export(seed=13))
        self.assertNotEqual(first[1:], self.export(seed=42)[1:])
        self.assertEqual(first[0], dict(pairs=25, individual_ratings=50, seed=13))
        strata = Counter((r["tier"], r["task_type"]) for r in first[2])
        self.assertEqual(len(strata), 12)
        self.assertEqual(sorted(strata.values()), [2] * 11 + [3])
        self.assertEqual({r["left_condition"] for r in first[2]}, {"A", "B"})

    def test_complete_pair_mode_excludes_incomplete_prompts_and_reports_them(self):
        self.rows[0]["split"] = "held-out"
        self.artifact["records"][0]["completed"] = False
        self.artifact["records"][0]["status"] = 503
        result, public, private = self.export(mode="human-complete-pairs")
        self.assertEqual(result, dict(pairs=59, individual_ratings=118, seed=13,
                                      excluded_prompt_ids=["fixture-00"]))
        self.assertNotIn("fixture-00", {r["prompt_id"] for r in private})
        self.assertTrue(all(r["left_completed"] and r["right_completed"] for r in private))
        self.assertEqual(len(public), 59)

    def test_held_out_keeps_manifest_selection_and_maps_each_sides_completion(self):
        self.rows[0]["split"] = "held-out"
        self.artifact["records"][0]["completed"] = False
        self.artifact["records"][0]["status"] = 503
        result, _, private = self.export(mode="human-held-out")
        self.assertEqual(result["pairs"], 60)
        for row in private:
            self.assertEqual(row["left_completed"],
                             row["prompt_id"] != "fixture-00" or row["left_condition"] != "A")
            self.assertEqual(row["right_completed"],
                             row["prompt_id"] != "fixture-00" or row["left_condition"] == "A")
