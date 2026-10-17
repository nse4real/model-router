"""Recover a stopped run from its ledger without dispatching or replaying calls."""
import argparse
from decimal import Decimal
import json
import os
from pathlib import Path
from benchmark.results import write_results
from monitoring.ledger import PostgresLedger, SQLiteLedger
from monitoring.settlement import SettlementJournal
from monitoring.quality import ensure_job_reasons


class _RunSettlementJournal(SettlementJournal):
    """Replay and export only entries owned by this run's durable attempts."""
    def __init__(self, ledger, run_id):
        super().__init__()
        self.ledger = ledger
        self.run_id = run_id

    def entries(self):
        with self.ledger.transaction() as db:
            parents = {row['attempt_id']: row['parent_request_id'] for row in db.execute(
                "SELECT a.attempt_id,a.parent_request_id FROM attempts a JOIN requests r "
                "ON r.request_id=a.parent_request_id WHERE r.run_id=?", (self.run_id,)).fetchall()}
        # The journal payload cannot grant ownership or update another request.
        return [entry for entry in super().entries() if entry['attempt_id'] in parents and
                (not entry['payload'].get('response') or
                 entry['payload']['response'].get('request_id') == parents[entry['attempt_id']])]


def recover(path, run_id=None, storage=None):
    path = Path(path)
    artifact = json.loads(path.read_text()) if path.suffix != '.sqlite' and path.exists() else None
    storage = storage or (artifact or {}).get('storage')
    ledger_path = path if path.suffix == '.sqlite' else path.with_suffix('.sqlite')
    ledger = (SQLiteLedger(ledger_path) if storage == 'test-double' or
              (storage is None and ledger_path.exists()) else
              PostgresLedger(os.environ.get('DATABASE_URL',
                             'postgresql://router:router@localhost:5432/router')))
    try:
        if artifact:
            run_id = artifact['run_id']
            try:
                _, metadata, records = ledger.load_run(run_id)
            except ValueError:
                # Compatibility with artifacts made before durable run metadata.
                metadata, records = dict(artifact), artifact.get('records', [])
        else:
            run_id, metadata, records = ledger.load_run(run_id)
        journal = _RunSettlementJournal(ledger, run_id)
        journal.recover(ledger)
        ledger.recover(run_id=run_id)
        saved = {e["payload"].get("response", {}).get("request_id"): e for e in journal.entries()
                 if e["payload"].get("response")}
        # Live reservations keep their jobs pending. Recovery must never publish
        # a final barrier while an admitted paid call could still be in flight.
        with ledger.transaction(lock=True) as db:
            ensure_job_reasons(db)
            db.execute("UPDATE judge_jobs SET state='quality_unknown' WHERE parent_request_id IN "
                       "(SELECT request_id FROM requests WHERE run_id=?) "
                       "AND state NOT IN ('completed','failed','quality_unknown') "
                       "AND NOT EXISTS (SELECT 1 FROM attempts a WHERE a.attempt_id=judge_jobs.attempt_id "
                       "AND a.state NOT IN ('settled','unknown'))", (run_id,))
            db.execute("INSERT INTO judge_job_reasons(attempt_id,reason) "
                       "SELECT attempt_id,'Recovered incomplete judge job; no replay' FROM judge_jobs "
                       "WHERE parent_request_id IN (SELECT request_id FROM requests WHERE run_id=?) "
                       "AND state='quality_unknown' ON CONFLICT(attempt_id) DO NOTHING", (run_id,))
        for record in records:
            record['cost'] = str(sum((Decimal(str(a['cost'])) for a in
                                      ledger.attempts(record['request_id'])), Decimal(0)))
            entry = saved.get(record['request_id'])
            if entry:
                response = entry['payload']['response']
                record.update(content=response['content'], model=response['model'],
                              requested_model=response['model'], resolved_model=response['resolved_model'],
                              completed=response['stop_reason'] == 'stop' and record.get('status') in ('pending', 200),
                              status=200 if record.get('status') == 'pending' else record['status'],
                              settlement_status=entry['status'])
            # A pending record is retained as non-completed, never replayed.
            if record.get('status') == 'pending':
                record.update(completed=False, status='crash_unknown')
        for name in ('totals', 'analysis', 'records', 'final'):
            metadata.pop(name, None)
        metadata['status'] = 'recovered_incomplete_no_replay'
        metadata['headline_claim_supported'] = False
        output = path.with_suffix('.json') if path.suffix == '.sqlite' else path
        return write_results(output, ledger, run_id, metadata, records)
    finally:
        ledger.close() if isinstance(ledger, SQLiteLedger) else None


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('artifact', help='Output JSON path (may be absent) or SQLite ledger path')
    parser.add_argument('--run-id')
    parser.add_argument('--storage', choices=['postgres', 'test-double'])
    args = parser.parse_args()
    result = recover(args.artifact, run_id=args.run_id, storage=args.storage)
    print(json.dumps({'final': result['final'], 'status': result['status']}))
