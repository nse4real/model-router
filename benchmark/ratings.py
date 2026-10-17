"""Blinded seeded A/B rating exports and offline calibration validation."""
import argparse
import hashlib
import json
from pathlib import Path
import random
from benchmark.dataset import load_dataset
from monitoring.quality import validate_calibration
from monitoring.security import redact


def export_ratings(artifact, output, mode="human-held-out", seed=13):
    output = Path(output)
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output directory: {output}")
    rows, manifest = load_dataset()
    rng = random.Random(seed)
    by = {(r["prompt_id"],r["condition"]):r for r in artifact["records"]}
    if mode == "calibration":
        candidates = [r for r in rows if r["split"] == "calibration" and all(
            by.get((r["id"], c), {}).get("completed") is True and
            by.get((r["id"], c), {}).get("status") == 200 for c in ("A", "B"))]
        if not candidates:
            raise ValueError("Calibration export requires at least 1 complete A/B pair "
                             "(both completed=True and status=200)")
        # Round-robin strata before selection keeps the 25 pairs diverse.
        buckets = {}
        for row in candidates:
            buckets.setdefault((row["tier"],row["task_type"]),[]).append(row["id"])
        for group in buckets.values():
            rng.shuffle(group)
        ids = []
        while len(ids) < 25 and any(buckets.values()):
            for group in buckets.values():
                if group and len(ids) < 25:
                    ids.append(group.pop())
    elif mode == "human-complete-pairs":
        # Only prompts whose A and B outputs both completed with HTTP 200; the rest are reported, not rated.
        complete = lambda p: all(by.get((p, c), {}).get("completed") is True and
                                 by.get((p, c), {}).get("status") == 200 for c in ("A", "B"))
        ids = [p for p in manifest["human_subset"] if complete(p)]
        excluded = [p for p in manifest["human_subset"] if not complete(p)]
        if not ids:
            raise ValueError("Complete-pair export requires at least 1 complete A/B pair")
    else:
        ids = manifest["human_subset"]
    public, private = [], []
    data = {r["id"]:r for r in rows}
    for prompt_id in ids:
        if any((prompt_id,c) not in by for c in ("A","B")):
            raise ValueError("Missing required A/B outputs")
        conditions = ["A","B"]
        rng.shuffle(conditions)
        blind_id = hashlib.sha256(f"{seed}:{prompt_id}".encode()).hexdigest()[:16]
        public.append(dict(blind_id=blind_id,prompt=data[prompt_id]["prompt"],
                           left=by[prompt_id,conditions[0]].get("content",""),
                           right=by[prompt_id,conditions[1]].get("content",""),
                           left_rating=None,right_rating=None))
        private.append(dict(blind_id=blind_id,prompt_id=prompt_id,left_condition=conditions[0],
                            tier=data[prompt_id]["tier"], task_type=data[prompt_id]["task_type"],
                            left_completed=by[prompt_id,conditions[0]].get("completed", False),
                            right_completed=by[prompt_id,conditions[1]].get("completed", False)))
    rng.shuffle(public)
    output.mkdir(parents=True,exist_ok=False)
    for name, value in (("ratings-blinded.json",public),("mapping-private.json",private)):
        (output/name).write_text(json.dumps(redact(value),indent=2),encoding="utf8")
    result = dict(pairs=len(ids),individual_ratings=2*len(ids),seed=seed)
    if mode == "human-complete-pairs":
        result["excluded_prompt_ids"] = excluded
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--artifact")
    p.add_argument("--output",default="results/ratings")
    p.add_argument("--mode",choices=["calibration","human-held-out","human-complete-pairs"],default="calibration")
    p.add_argument("--validate-calibration")
    args = p.parse_args()
    if args.validate_calibration:
        result = validate_calibration(json.loads(Path(args.validate_calibration).read_text()))
        Path(args.output).parent.mkdir(parents=True,exist_ok=True)
        Path(args.output).write_text(json.dumps(result,indent=2),encoding="utf8")
    else:
        result = export_ratings(json.loads(Path(args.artifact).read_text()),args.output,args.mode)
    print(json.dumps(result))
