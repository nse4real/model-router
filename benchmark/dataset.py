"""Frozen, checksummed splits. Loading never executes prompts."""
import hashlib
import json
from pathlib import Path

DATA = Path(__file__).with_name("data")
KNOWN_SPLITS = {"dev", "calibration", "held-out", "savings"}


def load_dataset(directory=DATA):
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    raw = (directory / "prompts.jsonl").read_bytes()
    if hashlib.sha256(raw).hexdigest() != manifest["sha256"]:
        raise ValueError("Dataset checksum mismatch")
    # A manifest declares which splits its file contains; v1 declares the original three.
    splits = set(manifest["counts"])
    if not splits <= KNOWN_SPLITS:
        raise ValueError("Invalid split")
    rows = [json.loads(line) for line in raw.decode().splitlines() if line]
    ids, texts = set(), set()
    for row in rows:
        canonical = " ".join(row["prompt"].split()).casefold()
        if row["id"] in ids or canonical in texts:
            raise ValueError("Dataset duplicates across splits")
        if row["split"] not in splits:
            raise ValueError("Invalid split")
        ids.add(row["id"])
        texts.add(canonical)
    counts = {s: sum(r["split"] == s for r in rows) for s in splits}
    if counts != manifest["counts"]:
        raise ValueError("Split counts mismatch")
    return rows, manifest


def split(name, directory=DATA):
    if name not in KNOWN_SPLITS:
        raise ValueError("Unknown split")
    rows, manifest = load_dataset(directory)
    return [r for r in rows if r["split"] == name], manifest
