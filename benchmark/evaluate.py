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
    by = {(r["prompt_id"],r["condition"]):r for r in artifact["records"]}
    # Excluded prompts are exactly the predeclared prompts whose A or B output did not complete with HTTP 200.
    complete = lambda p: all(by.get((p,c),{}).get("completed") is True and by[p,c].get("status") == 200
                             for c in ("A","B"))
    excluded = {p for p in manifest["human_subset"] if not complete(p)}
    kept = set(manifest["human_subset"]) - excluded
    if {m["prompt_id"] for m in mapping} != kept or len(mapping) != len(kept):
        raise ValueError("Mapping must cover exactly the complete predeclared pairs")
    scores = {r["blind_id"]:r for r in ratings}
    if len(scores) != len(kept) or len(ratings) != len(kept):
        raise ValueError("Require one unique blinded rating row per kept pair")
    for m in mapping:
        values = scores[m["blind_id"]]
        for side in ("left","right"):
            value = values[side+"_rating"]
            if type(value) is not int or not 1 <= value <= 5:
                raise ValueError("Human ratings must be integers 1..5")
            c = m["left_condition"] if side == "left" else ("B" if m["left_condition"] == "A" else "A")
            by[m["prompt_id"],c]["quality"] = value
    # Excluded prompts are left out of every analysis, including cost and completion.
    subset = [r for r in artifact["records"] if r["prompt_id"] in kept]
    artifact["analysis"] = analyse(subset,len(kept),artifact["seed"],quality_source="human",
                                   data_source=artifact.get("data_source", "mock"))
    artifact["excluded_prompt_ids"] = sorted(excluded)
    artifact["human_pairs"] = len(kept)
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
    print("Wrote human-subset analysis; C quality claims withheld; data source preserved")
