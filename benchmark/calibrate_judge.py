"""Calibrate the mid judge on the 25 blinded run-5 pairs (dry run by default)."""
import argparse
import asyncio
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, replace
import hashlib
import json
import logging
import os
from pathlib import Path
from uuid import NAMESPACE_URL, uuid4, uuid5

from api.contracts import Request
from benchmark.dataset import load_dataset
from fallback.providers import Output, real_provider
from monitoring.ledger import PostgresLedger, StorageUnavailable
from monitoring.quality import Quality, ensure_job_reasons, validate_calibration
from routing.admission import shared_admission
from routing.config import Config


ARTIFACT = "results/calibration-real-5.json"
RATINGS = "results/ratings-calibration-run5/ratings-blinded.json"
MAPPING = "results/ratings-calibration-run5/mapping-private.json"
OUTPUT = "results/judge-calibration-run5.json"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class Rubric:
    version: str
    text: str
    sha256: str

    @property
    def metadata(self):
        return dict(rubric=self.version, rubric_sha256=self.sha256)


def load_rubric(version):
    if version not in ("v1", "v2"):
        raise ValueError("Rubric must be v1 or v2")
    path = Path(__file__).resolve().parents[1] / "monitoring" / f"judge_prompt_{version}.md"
    raw = path.read_bytes()
    # Match read_text's newline handling while hashing the exact file bytes.
    content = raw.decode("utf8").replace("\r\n", "\n").replace("\r", "\n")
    return Rubric(version, content, hashlib.sha256(raw).hexdigest())


def _unique(rows, key):
    indexed = {}
    for row in rows:
        value = row[key]
        if value in indexed:
            raise ValueError(f"Duplicate {key}: {value}")
        indexed[value] = row
    return indexed


def load_pairs(artifact, ratings, mapping):
    """Join blind IDs to source A/B records; reject stale or ambiguous exports."""
    rows, manifest = load_dataset()
    if artifact.get("dataset_checksum") != manifest["sha256"]:
        raise ValueError("Dataset version mismatch")
    if (artifact.get("storage") != "postgres" or artifact.get("data_source") == "mock"
            or artifact.get("split") != "calibration" or not artifact.get("run_id")):
        raise ValueError("Calibration requires a real calibration artifact with Postgres storage")
    public, private = _unique(ratings, "blind_id"), _unique(mapping, "blind_id")
    if len(public) != 25 or public.keys() != private.keys():
        raise ValueError("Need exactly 25 matched, individually rated A/B pairs")
    prompts = {r["id"]: r for r in rows if r["split"] == "calibration"}
    records = {}
    for record in artifact["records"]:
        key = record["prompt_id"], record["condition"]
        if key in records:
            raise ValueError(f"Duplicate source record: {key}")
        records[key] = record
    pairs, seen = [], set()
    for blind_id, rating in public.items():
        mapping_row = private[blind_id]
        prompt_id = mapping_row["prompt_id"]
        if prompt_id in seen or prompt_id not in prompts:
            raise ValueError(f"Duplicate or non-calibration prompt: {blind_id}")
        seen.add(prompt_id)
        prompt = prompts[prompt_id]
        if rating["prompt"] != prompt["prompt"] or any(
                mapping_row[field] != prompt[field] for field in ("tier", "task_type")):
            raise ValueError(f"Prompt or stratification mismatch: {blind_id}")
        left = mapping_row["left_condition"]
        if left not in ("A", "B"):
            raise ValueError(f"Invalid condition mapping: {blind_id}")
        right = "B" if left == "A" else "A"
        if mapping_row.get("right_condition", right) != right:
            raise ValueError(f"Invalid right condition mapping: {blind_id}")
        pair = dict(blind_id=blind_id, prompt_id=prompt_id, prompt=rating["prompt"],
                    tier=mapping_row["tier"], task_type=mapping_row["task_type"],
                    judge_a=None, judge_b=None, records={})
        for side, condition in (("left", left), ("right", right)):
            value = rating[side + "_rating"]
            if type(value) is not int or not 1 <= value <= 5:
                raise ValueError(f"Invalid human rating: {blind_id}/{side}")
            record = records.get((prompt_id, condition), {})
            if (mapping_row.get(side + "_completed") is not True
                    or record.get("completed") is not True or record.get("status") != 200
                    or not record.get("request_id") or not isinstance(record.get("content"), str)
                    or rating[side] != record["content"]):
                raise ValueError(f"Incomplete or mismatched rated output: {blind_id}/{side}")
            suffix = condition.lower()
            pair["human_" + suffix] = value
            pair["records"][suffix] = record
        pairs.append(pair)
    parents = [r["request_id"] for p in pairs for r in p["records"].values()]
    if len(set(parents)) != 50:
        raise ValueError("Rated outputs require unique parent request IDs")
    return pairs


def parse_score(content):
    """Strict JSON object and integer score, including rejecting duplicate keys."""
    def object_pairs(items):
        if len(dict(items)) != len(items):
            raise ValueError("Duplicate judge JSON keys")
        return dict(items)

    parsed = json.loads(content, object_pairs_hook=object_pairs)
    if (not isinstance(parsed, dict) or set(parsed) != {"score"}
            or type(parsed["score"]) is not int or not 1 <= parsed["score"] <= 5):
        raise ValueError("Expected exactly {\"score\": integer in 1..5}")
    return parsed["score"]


class StrictJudgeProvider:
    """Keep usage for settlement; malformed responses have no usable score."""
    def __init__(self, provider, raw_debug=0, debug_file=None):
        self.provider = provider
        self.error = None
        self.raw_debug, self.debug_file = raw_debug, debug_file
        self.calls = 0
        self.context = {}

    async def complete(self, model, request, timeout):
        self.calls += 1
        call = self.calls
        result = await self.provider.complete(model, request, timeout)
        if call <= self.raw_debug and self.debug_file is not None:
            try:
                self.debug_file.write(json.dumps(dict(self.context, call=call,
                                                      raw_text=result.content)) + "\n")
                self.debug_file.flush()
            except Exception:
                # Diagnostics must never bypass parsing or paid-usage settlement.
                LOG.warning("Could not write raw judge diagnostic for call %s", call)
        try:
            parse_score(result.content)
            if result.stop_reason != "stop":
                raise ValueError("Judge response did not complete normally")
        except (ValueError, TypeError):
            self.error = "Malformed or incomplete judge response"
            # Quality settles the original usage before rejecting this missing score.
            # No fallback score is introduced; paid responses stay accounted for.
            return replace(result, content="{}")
        return result


@contextmanager
def _exclusive_outputs(output, report, debug=None):
    """Claim all paths before any provider call, rolling back a partial claim."""
    created = []
    with ExitStack() as stack:
        try:
            for path in (output, report) + ((debug,) if debug is not None else ()):
                path.parent.mkdir(parents=True, exist_ok=True)
                created.append((path, stack.enter_context(path.open("x", encoding="utf8"))))
        except Exception:
            stack.close()
            for path, _ in created:
                path.unlink()
            raise
        yield created[0][1], created[1][1], created[2][1] if debug is not None else None


def _attempt_id(run_id, parent, rubric="v1"):
    if isinstance(rubric, Rubric):
        rubric = rubric.version + (":" + rubric.sha256 if rubric.version != "v1" else "")
    return str(uuid5(NAMESPACE_URL, run_id + ":" + parent + ":judge-calibration-" + rubric))


def load_unresolved(pairs, previous, previous_report, run_id, rubric=None):
    """Carry completed ratings forward; retry only null scores with audit identity."""
    rows = _unique(json.loads(previous.read_text(encoding="utf8")), "blind_id")
    report = json.loads(previous_report.read_text(encoding="utf8"))
    if rubric is not None:
        # Legacy outputs have no rubric metadata and belong to v1 only.
        for metadata in [report, *rows.values()]:
            if (metadata.get("rubric", "v1") != rubric.version
                    or metadata.get("rubric_sha256", rubric.sha256 if rubric.version == "v1" else None)
                    != rubric.sha256):
                raise ValueError("Rerun rubric/hash mismatch; score all pairs into new outputs instead")
    if report.get("source_run_id") != run_id or rows.keys() != {p["blind_id"] for p in pairs}:
        raise ValueError("Rerun source does not match the calibration run/pairs")
    failures = {}
    for failure in report["unknown_outputs"]:
        key = failure["blind_id"], failure["side"]
        if key in failures or failure["side"] not in ("a", "b"):
            raise ValueError("Duplicate or invalid unresolved call")
        failures[key] = failure
    unresolved = set()
    for pair in pairs:
        old = rows[pair["blind_id"]]
        for key in ("prompt_id", "human_a", "human_b", "tier", "task_type"):
            if type(old.get(key)) is not type(pair[key]) or old[key] != pair[key]:
                raise ValueError("Rerun pair metadata or human ratings mismatch")
        for side in ("a", "b"):
            score = old.get("judge_" + side)
            attempt = old.get("attempt_" + side)
            if score is None:
                key = pair["blind_id"], side
                unresolved.add(key)
                failure = failures.get(key, {})
                if failure.get("prompt_id") != pair["prompt_id"] or not failure.get("attempt_id"):
                    raise ValueError("Unresolved call needs its previous attempt identity")
                if attempt and attempt != failure["attempt_id"]:
                    raise ValueError("Conflicting previous attempt identity")
                attempt = failure["attempt_id"]
            elif type(score) is not int or not 1 <= score <= 5:
                raise ValueError("Invalid prior judge rating; rerun accepts only unresolved calls")
            pair["judge_" + side] = score
            pair["attempt_" + side] = attempt or _attempt_id(run_id, pair["records"][side]["request_id"], rubric or "v1")
    if not unresolved or unresolved != failures.keys():
        raise ValueError("Rerun requires exactly the reported unresolved calls")


def check_recovered(pairs, ledger):
    """Check every selected parent before reserving any fresh judge attempt."""
    with ledger.transaction(lock=True) as db:
        for pair in pairs:
            for side in ("a", "b"):
                if pair["judge_" + side] is not None:
                    continue
                parent = pair["records"][side]["request_id"]
                previous = pair["attempt_" + side]
                old = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (previous,)).fetchone()
                if old and (old["parent_request_id"] != parent or old["purpose"] != "evaluation"):
                    raise ValueError("Previous judge attempt does not belong to the rated output")
                active = db.execute("SELECT attempt_id FROM attempts WHERE parent_request_id=? "
                    "AND purpose='evaluation' AND state NOT IN ('settled','unknown')", (parent,)).fetchone()
                if active:
                    raise ValueError("Judge attempt still reserved/dispatched; run benchmark.recover "
                                     "for the source run before rerunning: " + active["attempt_id"])
                job = db.execute("SELECT state FROM judge_jobs WHERE attempt_id=?", (previous,)).fetchone()
                if job and job["state"] not in ("completed", "failed", "quality_unknown"):
                    raise ValueError("Judge job still active; run benchmark.recover before rerunning")


async def score_pairs(pairs, ledger, config, provider, run_id, admission, *, rerun=False,
                      results=None, failures=None, rubric="v1", raw_debug=0, debug_file=None):
    """Reuse Quality's rubric, reservation, dispatch, settlement and unknown states."""
    if rerun:
        check_recovered(pairs, ledger)
    # Initialize companion metadata before reading either new or legacy jobs.
    try:
        with ledger.transaction(lock=True) as db:
            ensure_job_reasons(db)
    except StorageUnavailable:
        pass  # Per-call failures below still produce a report without dispatch.
    rubric = load_rubric(rubric) if isinstance(rubric, str) else rubric
    strict = StrictJudgeProvider(provider, raw_debug, debug_file)
    quality = Quality(ledger, config, strict, benchmark=True, sample_rate=1,
                      timeout=config.judge_calibration_timeout_s,
                      health=admission.health.for_benchmark(str(uuid4())), rate=admission.rate,
                      rubric=rubric.text)
    results = [] if results is None else results
    failures = [] if failures is None else failures
    cancelled = False
    for pair in pairs:
        result = {k: pair[k] for k in ("human_a", "human_b", "judge_a", "judge_b",
                                       "tier", "task_type", "blind_id", "prompt_id")}
        results.append(result)
        request = Request.parse(dict(messages=[
            dict(role="system", content="Answer accurately and concisely."),
            dict(role="user", content=pair["prompt"])]))
        for side in ("a", "b"):
            record = pair["records"][side]
            parent = record["request_id"]
            if rerun and result["judge_" + side] is not None:
                result["attempt_" + side] = pair["attempt_" + side]
                continue
            attempt = str(uuid4()) if rerun else _attempt_id(run_id, parent, rubric)
            result["attempt_" + side] = attempt
            strict.error = None
            strict.context = dict(blind_id=pair["blind_id"], prompt_id=pair["prompt_id"],
                                  side=side, attempt_id=attempt)
            job_args = (attempt, parent, "benchmark", request, Output(record["content"], 0, 0))
            job = None
            try:
                ledger.begin(parent, run_id)
                ledger.job(attempt, parent)
                if cancelled:
                    quality.unknown(job_args, "CancelledError: calibration cancelled before dispatch")
                else:
                    try:
                        await quality.process(job_args)
                    except asyncio.CancelledError:
                        cancelled = True
                with ledger.transaction() as db:
                    job = db.execute("SELECT j.*,d.reason FROM judge_jobs j LEFT JOIN judge_job_reasons d "
                                     "ON d.attempt_id=j.attempt_id WHERE j.attempt_id=?", (attempt,)).fetchone()
            except StorageUnavailable as exc:
                quality.unknown(job_args, exc)
            if job and job["state"] == "completed":
                score = job["score"]
                if score in (1, 2, 3, 4, 5):
                    result["judge_" + side] = int(score)
            if result["judge_" + side] is None:
                reason = strict.error or quality.failure_reasons.get(attempt)
                if not reason:
                    reason = (dict(job).get("reason") if job else None) or (
                        "Judge job did not complete: " + (job["state"] if job else "missing"))
                # Also persist reasons for malformed responses and legacy unknown jobs.
                if quality.failure_reasons.get(attempt) != reason:
                    quality.unknown(job_args, reason)
                failures.append(dict(blind_id=pair["blind_id"], prompt_id=pair["prompt_id"],
                                     side=side, attempt_id=attempt, reason=reason))
    if cancelled:
        raise asyncio.CancelledError
    return results, failures


async def calibrate(artifact=ARTIFACT, ratings=RATINGS, mapping=MAPPING, output=OUTPUT,
                    real=False, report=None, rerun_unresolved=None, rerun_report=None,
                    rubric="v1", raw_debug=0):
    rubric = load_rubric(rubric)
    if type(raw_debug) is not int or raw_debug < 0:
        raise ValueError("--raw-debug must be a nonnegative integer")
    output = Path(output)
    report = Path(report) if report else output.with_name(output.stem + "-report.json")
    debug = output.with_name(output.stem + "-raw-debug.jsonl") if raw_debug else None
    for path in (output, report) + ((debug,) if debug is not None else ()):
        if real and path.exists():
            raise FileExistsError(f"Refusing to overwrite existing output: {path}")
    paths = (output, report) + ((debug,) if debug is not None else ())
    if len({p.resolve() for p in paths}) != len(paths):
        raise ValueError("Output, report and debug must be different paths")
    artifact = json.loads(Path(artifact).read_text(encoding="utf8"))
    pairs = load_pairs(artifact, json.loads(Path(ratings).read_text(encoding="utf8")),
                       json.loads(Path(mapping).read_text(encoding="utf8")))
    if rerun_report and not rerun_unresolved:
        raise ValueError("--rerun-report requires --rerun-unresolved")
    if rerun_unresolved:
        previous = Path(rerun_unresolved)
        previous_report = Path(rerun_report) if rerun_report else previous.with_name(
            previous.stem + "-report.json")
        if {p.resolve() for p in paths} & {previous.resolve(), previous_report.resolve()}:
            raise ValueError("Rerun requires new output and report files")
        load_unresolved(pairs, previous, previous_report, artifact["run_id"], rubric)
    plan = dict(dry_run=not real, pairs=len(pairs), planned_calls=sum(
        p["judge_" + side] is None for p in pairs for side in ("a", "b")))
    if rubric.version != "v1":
        plan.update(rubric.metadata)
    if raw_debug:
        plan.update(raw_debug=raw_debug, raw_debug_file=str(debug))
    if not real:
        return plan
    config = Config()
    if config.data["judge"] != "mid":
        raise ValueError("Calibration requires the configured judge alias mid")
    provider = real_provider(True)

    def open_ledger():
        return PostgresLedger(os.environ.get("DATABASE_URL",
            "postgresql://router:router@localhost:5432/router"),
            cap=config.data["global_cap_usd"], evaluation_cap=config.data["evaluation_cap_usd"])

    ledger = open_ledger() if rerun_unresolved else None
    if rerun_unresolved:
        check_recovered(pairs, ledger)
    with _exclusive_outputs(output, report, debug) as (pair_file, report_file, debug_file):
        ledger = ledger if ledger is not None else open_ledger()
        admission = shared_admission(config.slo_ms)
        results, failures = [], []
        cancelled = False
        try:
            await score_pairs(pairs, ledger, config, provider, artifact["run_id"], admission,
                              rerun=bool(rerun_unresolved), results=results, failures=failures,
                              rubric=rubric, raw_debug=raw_debug, debug_file=debug_file)
        except asyncio.CancelledError:
            cancelled = True
        for row in results:
            row.update(rubric.metadata)
        validation = (dict(validated=False, reason=f"judge incomplete ({len(failures)} calls unresolved)")
                      if failures else validate_calibration(results))
        validation.update(rubric.metadata)
        if raw_debug:
            validation.update(raw_debug=raw_debug, raw_debug_file=str(debug))
        validation["unknown_outputs"] = failures
        validation["judge_alias"] = "mid"
        validation["judge_model"] = config.model("mid")["id"]
        validation["source_run_id"] = artifact["run_id"]
        if rerun_unresolved:
            validation["rerun_of"] = str(previous)
        json.dump(results, pair_file, indent=2, allow_nan=False)
        json.dump(validation, report_file, indent=2, allow_nan=False)
    if cancelled:
        raise asyncio.CancelledError
    return dict(plan, validated=validation["validated"], unknown_outputs=failures,
                output=str(output), report=str(report))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", default=ARTIFACT)
    parser.add_argument("--ratings", default=RATINGS)
    parser.add_argument("--mapping", default=MAPPING)
    parser.add_argument("--output", default=OUTPUT)
    parser.add_argument("--report")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--rubric", choices=("v1", "v2"), default="v1")
    parser.add_argument("--raw-debug", type=int, default=0,
                        help="Capture the first N raw judge responses in <output-stem>-raw-debug.jsonl")
    parser.add_argument("--rerun-unresolved", help="Previous pair output; retry only its unresolved calls")
    parser.add_argument("--rerun-report", help="Previous report if it uses a custom path")
    args = parser.parse_args(argv)
    result = asyncio.run(calibrate(**vars(args)))
    print(json.dumps(result))
    return 1 if args.real and not result["validated"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
