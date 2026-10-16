"""Recover a stopped run without dispatching or replaying any model call."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
from benchmark.results import write_results
from monitoring.ledger import PostgresLedger, SQLiteLedger


def recover(path):
    path = Path(path)
    artifact = json.loads(path.read_text())
    if artifact.get("data_source") != "mock":
        raise ValueError("Only mock artifacts supported")
    ledger = (SQLiteLedger(path.with_suffix(".sqlite")) if artifact.get("storage") == "test-double"
              else PostgresLedger(os.environ.get("DATABASE_URL",
                                   "postgresql://router:router@localhost:5432/router")))
    ledger.recover()
    # A stopped benchmark's in-memory queue is gone. Its jobs cannot be replayed
    # as free work; mark them terminal unknown, retaining every incurred reservation.
    with ledger.transaction() as db:
        db.execute("UPDATE judge_jobs SET state='quality_unknown' WHERE parent_request_id IN "
                   "(SELECT request_id FROM requests WHERE run_id=?) "
                   "AND state NOT IN ('completed','failed','quality_unknown')", (artifact["run_id"],))
    records = artifact.pop("records")
    for record in records:
        record["cost"] = str(sum((Decimal(str(a["cost"])) for a in
                                  ledger.attempts(record["request_id"])), Decimal(0)))
    artifact.pop("totals", None)
    artifact["status"] = "recovered_incomplete_no_replay"
    artifact["headline_claim_supported"] = False
    return write_results(path,ledger,artifact["run_id"],artifact,records)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("artifact")
    result = recover(parser.parse_args().artifact)
    print(json.dumps({"final":result["final"],"status":result["status"]}))
