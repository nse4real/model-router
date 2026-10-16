"""Score an existing MOCK artifact; every judge call reserves shared spend."""
import argparse
import asyncio
from decimal import Decimal
import json
import os
from pathlib import Path
from uuid import uuid5, NAMESPACE_URL
from api.contracts import Request
from benchmark.dataset import load_dataset
from benchmark.results import analyse, write_results
from fallback.providers import MockJudgeProvider, Output
from monitoring.ledger import PostgresLedger, SQLiteLedger
from monitoring.quality import Quality
from routing.config import Config


async def judge(path):
    path = Path(path)
    artifact = json.loads(path.read_text())
    if artifact.get("data_source") != "mock":
        raise ValueError("Only mock artifacts may be judged")
    ledger = (SQLiteLedger(path.with_suffix(".sqlite")) if artifact.get("storage") == "test-double"
              else PostgresLedger(os.environ.get("DATABASE_URL",
                                   "postgresql://router:router@localhost:5432/router")))
    records = artifact.pop("records")
    rows, manifest = load_dataset()
    if artifact["dataset_checksum"] != manifest["sha256"]:
        raise ValueError("Dataset version mismatch")
    prompts = {r["id"]:r["prompt"] for r in rows}
    q = Quality(ledger,Config(),MockJudgeProvider(),benchmark=True,sample_rate=1)
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
    artifact["quality_source"] = "diagnostic_mock_judge"
    artifact["judge_validated"] = False
    n = artifact["analysis"]["conditions"]["A"]["prompts"]
    artifact["analysis"] = analyse(records,n,artifact["seed"],quality_source="diagnostic_mock_judge")
    return write_results(path,ledger,artifact["run_id"],artifact,records)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("artifact")
    result = asyncio.run(judge(p.parse_args().artifact))
    print(json.dumps({"data_source":"mock","final":result["final"],"quality_claim":"withheld"}))
