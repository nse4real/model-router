"""Classifier diagnostic on dev labels only; this is not answer quality."""
import json
from api.contracts import Request
from benchmark.dataset import split
from classifier.scorer import score
from routing.config import Config


def diagnostic():
    rows, manifest = split("dev")
    thresholds = Config().data["thresholds"]
    correct = sum(score(Request.parse(dict(messages=[dict(role="user",content=r["prompt"])])),
                        thresholds)[0] == r["tier"] for r in rows)
    return dict(split="dev",n=len(rows),correct=correct,accuracy=correct/len(rows),
                dataset_checksum=manifest["sha256"], thresholds=thresholds,
                scope="heuristic agreement with author-assigned tier labels; not answer quality")


if __name__ == "__main__":
    print(json.dumps(diagnostic()))
