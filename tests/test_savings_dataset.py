"""Savings split v2: loads with its own checksum and is disjoint from the frozen v1 prompts."""
from collections import Counter
from pathlib import Path
import unittest

from benchmark.dataset import DATA, load_dataset

V2 = Path(DATA) / "savings-v2"


def normalised(text):
    return " ".join(text.split()).casefold()


class SavingsDatasetTests(unittest.TestCase):
    def test_v1_is_unchanged_and_still_loads(self):
        rows, manifest = load_dataset()
        self.assertEqual(manifest["counts"], {"dev": 43, "calibration": 50, "held-out": 60})
        self.assertEqual(len(rows), 153)

    def test_v2_loads_with_its_own_checksum_and_balanced_tiers(self):
        rows, manifest = load_dataset(V2)
        self.assertEqual(manifest["counts"], {"savings": 60})
        self.assertEqual(len(rows), 60)
        self.assertEqual(Counter(r["tier"] for r in rows),
                         {"simple": 20, "medium": 20, "complex": 20})
        self.assertTrue(all(r["split"] == "savings" for r in rows))
        self.assertTrue(all("assistant-authored" in r["source"] for r in rows))

    def test_v2_is_disjoint_from_v1_by_id_and_text(self):
        v1, _ = load_dataset()
        v2, _ = load_dataset(V2)
        self.assertFalse({r["id"] for r in v1} & {r["id"] for r in v2})
        self.assertFalse({normalised(r["prompt"]) for r in v1}
                         & {normalised(r["prompt"]) for r in v2})


if __name__ == "__main__":
    unittest.main()
