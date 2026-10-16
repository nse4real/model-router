"""Mock-only runner. Defaults to dev; held-out execution needs an explicit flag."""
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


def git_commit():
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


async def run_conditions(router, prompts, seed=13, run_id=None, on_record=None):
    if router.provider.mode != "mock":
        raise ValueError("Benchmarks may use mock providers only")
    router.benchmark = True
    if router.quality:
        router.quality.benchmark = True
    run_id = run_id or str(uuid4())
    rng = random.Random(seed)
    order = list(prompts)
    rng.shuffle(order)
    router.cache.clear()
    records = []
    aborted = False

    async def call(row, condition):
        nonlocal aborted
        request = Request.parse(dict(messages=[dict(role="system", content="Answer accurately and concisely."),
                                                dict(role="user", content=row["prompt"])],
                                     max_tokens=512, temperature=0))
        record = dict(prompt_id=row["id"], condition=condition, request_id=request.request_id,
                      completed=False, quality=None)
        try:
            response = await router.route(request, use_cache=condition.startswith("C"),
                force_model=router.config.chains["complex"][0] if condition == "A" else None,
                run_id=run_id)
            record.update(content=response["content"], model=response["model"],
                          completed=response["stop_reason"] == "stop", cache_hit=response["cache_hit"])
        except RouterError as exc:
            record.update(error=exc.message, status=exc.status)
            if "cap" in exc.message.lower():
                aborted = True
        record["cost"] = str(sum((Decimal(str(a["cost"])) for a in
                                  router.ledger.attempts(request.request_id)), Decimal(0)))
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
    if router.quality:
        await router.quality.drain()
    # Refresh cost per request including any asynchronous evaluation.
    for record in records:
        record["cost"] = str(sum((Decimal(str(a["cost"])) for a in
                                  router.ledger.attempts(record["request_id"])), Decimal(0)))
    return run_id, records


async def main(args):
    if args.second_run and args.split != "held-out":
        raise SystemExit("The fixed second-run subset is held-out only")
    if args.split == "held-out" and not args.allow_held_out_mock:
        raise SystemExit("Held-out is protected; explicit --allow-held-out-mock required")
    prompts, manifest = split(args.split)
    config = Config()
    preflight = None
    if args.split == "held-out":
        if not args.dev_results:
            raise SystemExit("Measured mock dev results are required for preflight")
        dev = json.loads(Path(args.dev_results).read_text())
        if not dev.get("final") or dev.get("split") != "dev" or dev.get("data_source") != "mock":
            raise SystemExit("Preflight needs final, mock, dev results")
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
    output = Path(args.output)
    if output.exists():
        raise SystemExit("Refusing to overwrite an existing run; use a new output or benchmark.recover")
    ledger_path = output.with_suffix(".sqlite")
    ledger_path.parent.mkdir(parents=True, exist_ok=True)
    ledger = (SQLiteLedger(ledger_path) if args.storage == "test-double" else
              PostgresLedger(os.environ.get("DATABASE_URL",
                             "postgresql://router:router@localhost:5432/router")))
    ledger.recover()
    router = Router(ledger, config=config, benchmark=True)
    metadata = dict(git_commit=git_commit(), dataset_checksum=manifest["sha256"],
                    dataset_version=manifest["version"], model_ids=[m["id"] for m in config.models.values()],
                    price_table_version=config.version, seed=args.seed, split=args.split,
                    preflight=preflight, prompt_count=len(prompts), quality_source="pending", judge_validated=False, storage=args.storage,
                    repetition_rate=0, warm_pass_repetition_rate=1,
                    thresholds=config.data["thresholds"], human_subset=manifest["human_subset"],
                    reproducibility_subset=manifest["reproducibility_subset"])
    def checkpoint(records):
        write_results(output, ledger, run_id, metadata, records)
    run_id, records = await run_conditions(router, prompts, args.seed, run_id, checkpoint)
    if ledger.barrier(run_id):
        metadata["analysis"] = analyse(records, len(prompts), args.seed)
    result = write_results(output, ledger, run_id, metadata, records)
    print(json.dumps(dict(output=str(output), final=result["final"],
                          totals=totals(ledger,run_id) if result["final"] else "pending")))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--split", choices=["dev","calibration","held-out"], default="dev")
    parser.add_argument("--allow-held-out-mock", action="store_true")
    parser.add_argument("--dev-results")
    parser.add_argument("--second-run", action="store_true")
    parser.add_argument("--storage", choices=["postgres","test-double"], default="postgres")
    parser.add_argument("--seed", type=int, default=13)
    parser.add_argument("--output", default="results/benchmark-mock-dev.json")
    asyncio.run(main(parser.parse_args()))
