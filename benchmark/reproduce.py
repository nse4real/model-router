"""Compare existing mock artifacts; never dispatch a provider."""
import argparse
import json
from pathlib import Path
from benchmark.dataset import load_dataset
from benchmark.results import reproducibility
from monitoring.security import redact


def compare(first, second):
    _, manifest = load_dataset()
    for artifact in (first,second):
        if artifact.get("data_source") != "mock" or not artifact.get("final"):
            raise ValueError("Require final mock artifacts")
        if artifact.get("dataset_checksum") != manifest["sha256"]:
            raise ValueError("Dataset mismatch")
    for field in ("seed","model_ids","price_table_version","thresholds"):
        if first.get(field) != second.get(field):
            raise ValueError("Reproducibility metadata mismatch: " + field)
    return dict(data_source="mock",subset=manifest["reproducibility_subset"],
                comparison=reproducibility(first["records"],second["records"],
                                           manifest["reproducibility_subset"],first["seed"]))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("first")
    p.add_argument("second")
    p.add_argument("--output",default="results/reproducibility-mock.json")
    a = p.parse_args()
    result = compare(json.loads(Path(a.first).read_text()),json.loads(Path(a.second).read_text()))
    Path(a.output).parent.mkdir(parents=True,exist_ok=True)
    Path(a.output).write_text(json.dumps(redact(result),indent=2),encoding="utf8")
    print("Wrote mock reproducibility comparison; no provider calls")
