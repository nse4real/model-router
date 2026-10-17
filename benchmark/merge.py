"""Merge benchmark outputs without replaying calls or discarding incurred spend."""
import argparse
from copy import deepcopy
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from uuid import uuid4


MATCH_FIELDS = ("split", "seed", "dataset_checksum", "dataset_version", "data_source",
                "price_table_version", "model_ids", "thresholds")


def _records(artifact, source, field="records"):
    rows = deepcopy(artifact.get(field, []))
    if not isinstance(rows, list):
        raise ValueError(f"{source}: {field} must be a list")
    for record in rows:
        if not isinstance(record, dict) or not isinstance(record.get("prompt_id"), str):
            raise ValueError(f"{source}: each record needs a prompt_id")
        record.setdefault("source", source)
    return rows


def _totals(records):
    costs = {"inference": Decimal(0), "evaluation": Decimal(0)}
    seen = set()
    for record in records:
        # Never substitute a record's cached cost for its individual attempts.
        attempts = record.get("attempts")
        if not isinstance(attempts, list):
            raise ValueError("Every record must export attempts before merging; recover its source first")
        for attempt in attempts:
            attempt_id = attempt["attempt_id"]
            if attempt_id in seen:
                raise ValueError("Duplicate attempt ID across merge inputs: " + attempt_id)
            seen.add(attempt_id)
            purpose = attempt["purpose"]
            if purpose not in costs:
                raise ValueError("Unknown attempt purpose: " + str(purpose))
            cost = Decimal(str(attempt["cost"]))
            if not cost.is_finite() or cost < 0:
                raise ValueError("Attempt cost must be finite and nonnegative")
            # Unknown outcomes already carry their conservative reserved charge.
            costs[purpose] += cost
    return dict(inference_cost=str(costs["inference"]), evaluation_cost=str(costs["evaluation"]),
                total_cost=str(sum(costs.values(), Decimal(0))))


def merge(base_path, subset_path, output_path):
    """Replace selected answers, retain superseded charges, and write a new artifact."""
    base_path, subset_path, output_path = map(Path, (base_path, subset_path, output_path))
    if output_path.exists():
        raise FileExistsError("Refusing to overwrite an existing output: " + str(output_path))
    if base_path.resolve() == subset_path.resolve():
        raise ValueError("Base and subset must be different artifacts")
    base, subset = [json.loads(path.read_text(encoding="utf8")) for path in (base_path, subset_path)]
    for field in MATCH_FIELDS:
        if base.get(field) != subset.get(field):
            raise ValueError("Merge metadata mismatch: " + field)
    base_source, subset_source = str(base_path.resolve()), str(subset_path.resolve())
    base_records, subset_records = _records(base, base_source), _records(subset, subset_source)
    replaced = sorted({row["prompt_id"] for row in subset_records})
    if not replaced:
        raise ValueError("Subset contains no prompt records")
    if not set(replaced) <= {row["prompt_id"] for row in base_records}:
        raise ValueError("Subset contains prompt IDs absent from the base")
    records = [row for row in base_records if row["prompt_id"] not in replaced] + subset_records
    superseded = (_records(base, base_source, "superseded_records") +
                  [row for row in base_records if row["prompt_id"] in replaced] +
                  _records(subset, subset_source, "superseded_records"))
    all_records = records + superseded
    totals = _totals(all_records)
    result = deepcopy(base)
    # Prior statistics describe different answers and omit rerun charges. Withhold
    # them rather than exporting stale savings or quality claims.
    for field in ("analysis", "status", "prompt_ids_file", "prompt_ids"):
        result.pop(field, None)
    result.update(merged=True, run_id=str(uuid4()), storage="merged-artifact",
                  timestamp=datetime.now(timezone.utc).isoformat(),
                  final=bool(base.get("final") and subset.get("final")),
                  records=records, superseded_records=superseded, totals=totals,
                  prompt_count=base.get("prompt_count", len({r["prompt_id"] for r in base_records})),
                  totals_scope="all attempts in records and superseded_records, including unknown outcomes",
                  merge=dict(base_path=base_source, subset_path=subset_source, replaced_prompt_ids=replaced),
                  source_metadata={source: {key: value for key, value in artifact.items()
                                           if key not in {"records", "superseded_records"}}
                                   for source, artifact in ((base_source, base), (subset_source, subset))})
    attempts = [a for row in all_records for a in row["attempts"]]
    result["resolved_model_ids"] = sorted({a["resolved_model"] for a in attempts if a.get("resolved_model")})
    resolutions = {}
    for attempt in attempts:
        identity = (attempt.get("requested_model"), attempt.get("resolved_model"), attempt["purpose"])
        resolutions[identity] = resolutions.get(identity, 0) + 1
    result["model_resolutions"] = [dict(requested_model=requested, resolved_model=resolved,
                                       purpose=purpose, calls=count)
                                   for (requested, resolved, purpose), count in resolutions.items()]
    serialized = json.dumps(result, indent=2, allow_nan=False)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation also closes the race after the early existence check.
    with output_path.open("x", encoding="utf8") as destination:
        destination.write(serialized + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_path")
    parser.add_argument("subset_path")
    parser.add_argument("output_path")
    args = parser.parse_args()
    result = merge(args.base_path, args.subset_path, args.output_path)
    print(json.dumps(dict(output=args.output_path, replaced_prompt_ids=result["merge"]["replaced_prompt_ids"],
                          totals=result["totals"])))


if __name__ == "__main__":
    main()
