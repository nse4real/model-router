"""Apply the predeclared blinded human subset without making provider calls."""
import argparse
import json
from pathlib import Path
from benchmark.dataset import load_dataset
from benchmark.results import analyse
from monitoring.security import redact


def apply_human(artifact, ratings, mapping):
    if artifact.get("split") != "held-out" or not artifact.get("final"):
        raise ValueError("Need a final held-out artifact")
    _, manifest = load_dataset()
    if artifact.get("dataset_checksum") != manifest["sha256"]:
        raise ValueError("Dataset checksum mismatch")
    expected = set(manifest["human_subset"])
    if len(mapping) != 60 or {m["prompt_id"] for m in mapping} != expected:
        raise ValueError("Require exactly the predeclared 60 pairs")
    scores = {r["blind_id"]:r for r in ratings}
    if len(scores) != 60 or len(ratings) != 60:
        raise ValueError("Require 60 unique blinded rating rows")
    by = {(r["prompt_id"],r["condition"]):r for r in artifact["records"]}
    for m in mapping:
        values = scores[m["blind_id"]]
        for side in ("left","right"):
            value = values[side+"_rating"]
            if type(value) is not int or not 1 <= value <= 5:
                raise ValueError("Human ratings must be integers 1..5")
            c = m["left_condition"] if side == "left" else ("B" if m["left_condition"] == "A" else "A")
            row = by[m["prompt_id"],c]
            row["quality"] = value if row["completed"] else 1
    n = artifact["analysis"]["conditions"]["A"]["prompts"]
    artifact["analysis"] = analyse(artifact["records"],n,artifact["seed"],quality_source="human")
    artifact["quality_source"] = "human"
    artifact["judge_validated"] = False
    return artifact


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("artifact")
    p.add_argument("ratings")
    p.add_argument("mapping")
    p.add_argument("--output",default="results/human-analysis-mock.json")
    a = p.parse_args()
    result = apply_human(*(json.loads(Path(f).read_text()) for f in (a.artifact,a.ratings,a.mapping)))
    Path(a.output).parent.mkdir(parents=True,exist_ok=True)
    Path(a.output).write_text(json.dumps(redact(result),indent=2),encoding="utf8")
    print("Wrote human-subset analysis; C quality claims withheld; data source remains mock")
