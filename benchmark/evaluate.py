"""Apply blinded human ratings without making provider calls."""
import argparse
import json
import math
from pathlib import Path
from statistics import mean
from benchmark.dataset import load_dataset, split
from benchmark.results import analyse, bootstrap_ci
from monitoring.security import redact


def descriptive_quality(by, kept, seed):
    a = [by[p,"A"]["quality"] for p in sorted(kept)]
    b = [by[p,"B"]["quality"] for p in sorted(kept)]
    differences = [vb-va for va,vb in zip(a,b)]
    higher = sum(d > 0 for d in differences)
    lower = sum(d < 0 for d in differences)
    non_ties = higher + lower
    p_value = (min(1.,2*sum(math.comb(non_ties,k) for k in range(min(higher,lower)+1))/2**non_ties)
               if non_ties else 1.)
    return dict(n_pairs=len(a),mean_A=mean(a),mean_B=mean(b),mean_diff=mean(differences),
                ci=bootstrap_ci(differences,seed),B_higher=higher,tied=len(a)-non_ties,B_lower=lower,
                sign_test_p=p_value,
                note="Descriptive only; no predeclared rule; no formal quality claim")


def apply_human(artifact, ratings, mapping):
    split_name = artifact.get("split")
    if split_name not in {"held-out","savings"} or not artifact.get("final"):
        raise ValueError("Need a final savings artifact" if split_name == "savings"
                         else "Need a final held-out artifact")
    if split_name == "savings":
        rows, manifest = split("savings")
        candidates = {r["id"] for r in rows}
    else:
        _, manifest = load_dataset()
        candidates = set(manifest["human_subset"])
    if artifact.get("dataset_checksum") != manifest["sha256"]:
        raise ValueError("Dataset checksum mismatch")
    by = {(r["prompt_id"],r["condition"]):r for r in artifact["records"]}
    # Excluded prompts are exactly the candidates whose A or B output did not complete with HTTP 200.
    complete = lambda p: all(by.get((p,c),{}).get("completed") is True and by[p,c].get("status") == 200
                             for c in ("A","B"))
    excluded = {p for p in candidates if not complete(p)}
    kept = candidates - excluded
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
    if split_name == "savings":
        for p in excluded:
            for c in ("A","B"):
                if (p,c) in by:
                    by[p,c]["quality"] = None
        subset = [r for r in artifact["records"] if r["prompt_id"] in candidates]
        n = len(candidates)
    else:
        # Held-out exclusions are left out of every analysis, including cost and completion.
        subset = [r for r in artifact["records"] if r["prompt_id"] in kept]
        n = len(kept)
    artifact["analysis"] = analyse(subset,n,artifact["seed"],quality_source="human",
                                   data_source=artifact.get("data_source", "mock"))
    artifact["descriptive_quality"] = descriptive_quality(by,kept,artifact["seed"])
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
    p.add_argument("--output",default=None)
    a = p.parse_args()
    result = apply_human(*(json.loads(Path(f).read_text()) for f in (a.artifact,a.ratings,a.mapping)))
    output = a.output or ("results/human-analysis-savings.json" if result["split"] == "savings"
                          else "results/human-analysis-mock.json")
    Path(output).parent.mkdir(parents=True,exist_ok=True)
    Path(output).write_text(json.dumps(redact(result),indent=2),encoding="utf8")
    print("Wrote human-subset analysis; C quality claims withheld; data source preserved")
