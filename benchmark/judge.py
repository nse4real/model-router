"""Score an artifact with guarded providers and durable shared spend admission."""
import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid4, uuid5, NAMESPACE_URL
from api.contracts import Request
from benchmark.dataset import load_dataset
from benchmark.results import analyse, write_results
from fallback.providers import MockJudgeProvider, Output, real_provider
from monitoring.ledger import PostgresLedger, SQLiteLedger
from monitoring.quality import Quality
from routing.config import Config
from routing.admission import shared_admission
from routing.latency import Health


async def judge(path, real=False):
    path = Path(path)
    artifact = json.loads(path.read_text())
    is_real = artifact.get("data_source") != "mock"
    if is_real:
        if artifact.get("storage") != "postgres":
            raise ValueError("Real entry points require shared Postgres spend admission")
        provider = real_provider(real)
    elif real:
        raise ValueError("--real cannot be used to judge a mock artifact")
    else:
        provider = MockJudgeProvider()
    config = Config(data=artifact.get("config"))
    caps = dict(cap=config.data["global_cap_usd"], evaluation_cap=config.data["evaluation_cap_usd"])
    ledger = (SQLiteLedger(path.with_suffix(".sqlite"), **caps) if artifact.get("storage") == "test-double"
              else PostgresLedger(os.environ.get("DATABASE_URL",
                                   "postgresql://router:router@localhost:5432/router"), **caps))
    records = artifact.pop("records")
    rows, manifest = load_dataset()
    if artifact["dataset_checksum"] != manifest["sha256"]:
        raise ValueError("Dataset version mismatch")
    prompts = {r["id"]:r["prompt"] for r in rows}
    admission = shared_admission(config.slo_ms) if artifact.get("storage") == "postgres" else None
    # A fresh invocation must not inherit live, generation or prior judge cooldowns.
    health = (admission.health if admission else Health(slo_ms=config.slo_ms)).for_benchmark(
        str(uuid4()))
    q = Quality(ledger, config, provider, benchmark=True, sample_rate=1,
                health=health,
                rate=admission.rate if admission else None)
    for record in records:
        if not record["completed"]:
            record["quality"] = 1
            continue
        attempt = str(uuid5(NAMESPACE_URL,artifact["run_id"]+":"+record["request_id"]+":judge-v1"))
        ledger.job(attempt,record["request_id"])
        request = Request.parse(dict(messages=[dict(role="system",content="Answer accurately and concisely."),
                                             dict(role="user",content=prompts[record["prompt_id"]])]))
        await q.process((attempt,record["request_id"],"benchmark",request,Output(record["content"],0,0)))
        with ledger.transaction() as db:
            job = db.execute("SELECT * FROM judge_jobs WHERE attempt_id=?",(attempt,)).fetchone()
        record["quality"] = float(job["score"]) if job["state"] == "completed" else None
    for record in records:
        record["cost"] = str(sum((Decimal(str(a["cost"])) for a in
                                  ledger.attempts(record["request_id"])),Decimal(0)))
    artifact["quality_source"] = "diagnostic_real_judge" if is_real else "diagnostic_mock_judge"
    artifact["judge_validated"] = False
    n = artifact["analysis"]["conditions"]["A"]["prompts"]
    artifact["analysis"] = analyse(records,n,artifact["seed"],quality_source=artifact["quality_source"],
                                    data_source=artifact["data_source"])
    return write_results(path,ledger,artifact["run_id"],artifact,records)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("artifact")
    p.add_argument("--real", action="store_true")
    args = p.parse_args()
    result = asyncio.run(judge(args.artifact, real=args.real))
    print(json.dumps({"data_source":result["data_source"],"final":result["final"],"quality_claim":"withheld"}))
