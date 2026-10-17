"""Mock-default runner. Real calls and held-out execution require explicit flags."""
import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
import random
import subprocess
from uuid import uuid4
from api.contracts import Request, RouterError
from benchmark.dataset import split
from benchmark.preflight import estimate
from benchmark.results import analyse, write_results, totals
from monitoring.ledger import SQLiteLedger, PostgresLedger
from routing.config import Config
from routing.service import Router
from fallback.providers import real_provider
from routing.admission import shared_admission


def git_commit():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def select_prompts(prompts, prompt_ids_file, split_name):
    """Filter the frozen calibration split without changing its order or manifest."""
    if split_name != "calibration":
        raise ValueError("--prompt-ids-file requires --split calibration")
    ids = json.loads(Path(prompt_ids_file).read_text(encoding="utf8"))
    if (not isinstance(ids, list) or not ids or
            any(not isinstance(value, str) or not value.strip() for value in ids)):
        raise ValueError("Prompt IDs must be a nonempty JSON list of strings")
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate prompt IDs")
    selected = set(ids)
    unknown = selected - {row["id"] for row in prompts}
    if unknown:
        raise ValueError("Prompt IDs outside the calibration split: " + ", ".join(sorted(unknown)))
    return [row for row in prompts if row["id"] in selected]


async def run_conditions(router, prompts, seed=13, run_id=None, on_record=None, metadata=None,
                         real=False):
    if router.provider.mode != "mock":
        if not real:
            raise ValueError("Real benchmarks require --real")
        real_provider(real)  # confirmation must precede even the first reservation
    elif real:
        raise ValueError("--real requires the guarded real provider")
    router.benchmark = True
    if router.quality:
        router.quality.benchmark = True
    run_id = run_id or str(uuid4())
    router.health = router.admission.health = router.health.for_benchmark(run_id)
    if router.quality and hasattr(router.quality, "admission"):
        router.quality.admission = router.admission
    metadata = dict(metadata or {}, seed=seed, config=router.config.data,
                    price_table_version=router.config.version,
                    benchmark_deadline_ms=router.config.benchmark_deadline_ms,
                    benchmark_max_tokens=router.config.benchmark_max_tokens,
                    data_source=router.provider.mode,
                    model_ids=[m["id"] for m in router.config.models.values()])
    if "dataset_checksum" not in metadata:
        _, manifest = split("dev")
        metadata["dataset_checksum"] = manifest["sha256"]
    router.ledger.save_run(run_id, metadata)
    rng = random.Random(seed)
    order = list(prompts)
    rng.shuffle(order)
    router.cache.clear()
    records = []
    aborted = False

    def refresh_attempts(record):
        attempts = router.ledger.attempts(record["request_id"])
        record["cost"] = str(sum((Decimal(str(a["cost"])) for a in attempts), Decimal(0)))
        record["attempt_latencies"] = [dict(attempt_id=a["attempt_id"], latency_ms=(
            float(a["latency_ms"]) if a["latency_ms"] is not None else None)) for a in attempts]

    async def call(row, condition):
        nonlocal aborted
        request = Request.parse(dict(messages=[dict(role="system", content="Answer accurately and concisely."),
                                                dict(role="user", content=row["prompt"])],
                                     max_tokens=router.config.benchmark_max_tokens, temperature=1,
                                     deadline_ms=router.config.benchmark_deadline_ms))
        record = dict(prompt_id=row["id"], condition=condition, request_id=request.request_id,
                      completed=False, quality=None, attempt_latencies=[])
        # This durable record exists before reservation, including the first call.
        record["status"] = "pending"
        router.ledger.save_record(run_id, record)
        if on_record:
            on_record(records + [record])
        try:
            response = await router.route(request, use_cache=condition.startswith("C"),
                force_model=router.config.chains["complex"][0] if condition == "A" else None,
                run_id=run_id)
            record.update(content=response["content"], model=response["model"],
                          requested_model=response["model"], resolved_model=response["resolved_model"],
                          completed=response["stop_reason"] == "stop", cache_hit=response["cache_hit"], status=200,
                          settlement_status=response.get("settlement_status", "settled"))
        except RouterError as exc:
            record.update(error=exc.message, status=exc.status)
            if "cap" in exc.message.lower():
                aborted = True
        refresh_attempts(record)
        await router.drain_background()
        router.ledger.save_record(run_id, record)
        records.append(record)
        if on_record:
            on_record(records)

    for row in order:
        conditions = ["A", "B", "C-cold"]
        rng.shuffle(conditions)
        for condition in conditions:
            await call(row, condition)
            if aborted:
                break
        if aborted:
            break
    if not aborted:
        rng.shuffle(order)
        for row in order:
            await call(row, "C-warm")
            if aborted:
                break
    await router.drain_background()
    if router.quality:
        await router.quality.drain()
    # Refresh cost per request including any asynchronous evaluation.
    for record in records:
        refresh_attempts(record)
        router.ledger.save_record(run_id, record)
    return run_id, records


async def main(args):
    output = Path(args.output)
    if output.exists():
        raise SystemExit("Refusing to overwrite an existing run; use a new output or benchmark.recover")
    if args.real and args.storage != "postgres":
        raise ValueError("Real entry points require shared Postgres spend admission")
    if args.second_run and args.split != "held-out":
        raise SystemExit("The fixed second-run subset is held-out only")
    if args.split == "held-out" and not (args.allow_held_out if args.real else args.allow_held_out_mock):
        raise SystemExit("Held-out is protected; use the explicit held-out flag for the selected mode")
    prompts, manifest = split(args.split)
    prompt_ids_file = getattr(args, "prompt_ids_file", None)
    if prompt_ids_file:
        prompts = select_prompts(prompts, prompt_ids_file, args.split)
    config = Config(args.config)
    provider = real_provider(True) if args.real else None
    source = provider.mode if provider else "mock"
    preflight = None
    if args.split == "held-out":
        if not args.dev_results:
            raise SystemExit("Matching dev results are required for preflight")
        dev = json.loads(Path(args.dev_results).read_text())
        if not dev.get("final") or dev.get("split") != "dev" or dev.get("data_source") != source:
            raise SystemExit("Preflight needs final dev results from the same provider mode")
        if (dev.get("dataset_checksum") != manifest["sha256"] or
                dev.get("price_table_version") != config.version or
                dev.get("model_ids") != [m["id"] for m in config.models.values()] or
                dev.get("thresholds") != config.data["thresholds"]):
            raise SystemExit("Dev metadata must match frozen dataset, models, prices and thresholds")
        costs = {}
        for row in dev["records"]:
            costs.setdefault(row["prompt_id"], []).append(Decimal(row["cost"]))
        if any(len(c) != 4 for c in costs.values()):
            raise SystemExit("Dev run must have all four conditions")
        preflight = estimate([sum(c) for c in costs.values()], len(prompts))
        rng = random.Random(args.seed)
        rng.shuffle(prompts)
        prompts = prompts[:preflight["admitted"]]
        if not prompts:
            raise SystemExit("Preflight admits no prompts")
    if args.second_run:
        prompts = [r for r in prompts if r["id"] in manifest["reproducibility_subset"]]
        if len(prompts) != 30:
            raise SystemExit("Preflight does not admit the complete fixed 30-prompt repeat subset")
    run_id = str(uuid4())
    output.parent.mkdir(parents=True, exist_ok=True)
    # Claim the path exclusively before opening storage or dispatching any work.
    # Later checkpoints replace only this run's own artifact.
    try:
        with output.open("x", encoding="utf8") as destination:
            destination.write("{}\n")
    except FileExistsError:
        raise SystemExit("Refusing to overwrite an existing run; choose a new output") from None
    ledger_path = output.with_suffix(".sqlite")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = (SQLiteLedger(ledger_path, cap=config.data["global_cap_usd"],
                            evaluation_cap=config.data["evaluation_cap_usd"]) if args.storage == "test-double" else
              PostgresLedger(os.environ.get("DATABASE_URL",
                             "postgresql://router:router@localhost:5432/router"),
                             cap=config.data["global_cap_usd"],
                             evaluation_cap=config.data["evaluation_cap_usd"]))
    ledger.recover()
    admission = shared_admission(config.slo_ms) if args.storage == "postgres" else None
    router = Router(ledger, config=config, provider=provider, benchmark=True,
                    health=admission.health if admission else None,
                    rate=admission.rate if admission else None)
    metadata = dict(data_source=source, config=config.data, git_commit=git_commit(), dataset_checksum=manifest["sha256"],
                    dataset_version=manifest["version"], model_ids=[m["id"] for m in config.models.values()],
                    price_table_version=config.version, seed=args.seed, split=args.split,
                    preflight=preflight, prompt_count=len(prompts), quality_source="human pending", judge_validated=False, storage=args.storage,
                    benchmark_deadline_ms=config.benchmark_deadline_ms,
                    benchmark_max_tokens=config.benchmark_max_tokens,
                    repetition_rate=0, warm_pass_repetition_rate=1,
                    thresholds=config.data["thresholds"], human_subset=manifest["human_subset"],
                    reproducibility_subset=manifest["reproducibility_subset"])
    if prompt_ids_file:
        metadata.update(prompt_ids_file=str(prompt_ids_file), prompt_ids=[row["id"] for row in prompts])
    def checkpoint(records):
        write_results(output, ledger, run_id, metadata, records)
    ledger.save_run(run_id, metadata)
    checkpoint([])
    run_id, records = await run_conditions(router, prompts, args.seed, run_id, checkpoint,
                                           metadata=metadata, real=args.real)
    if ledger.barrier(run_id):
        metadata["analysis"] = analyse(records, len(prompts), args.seed, data_source=source)
    result = write_results(output, ledger, run_id, metadata, records)
    print(json.dumps(dict(output=str(output), final=result["final"],
                          totals=totals(ledger,run_id) if result["final"] else "pending")))


def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev","calibration","held-out"], default="dev")
    parser.add_argument("--allow-held-out-mock", action="store_true")
    parser.add_argument("--allow-held-out", action="store_true")
    parser.add_argument("--real", action="store_true")
    parser.add_argument("--config", help="Model/provider configuration including versioned prices")
    parser.add_argument("--dev-results")
    parser.add_argument("--second-run", action="store_true")
    parser.add_argument("--prompt-ids-file", help="JSON list of calibration prompt IDs to rerun")
    parser.add_argument("--storage", choices=["postgres","test-double"], default="postgres")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--output", default="results/benchmark-mock-dev.json")
    return parser


if __name__ == "__main__":
    asyncio.run(main(build_parser().parse_args()))
