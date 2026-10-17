"""Durable local response journal for failed post-provider settlement writes."""
import asyncio
from decimal import Decimal
import json
import logging
import os
from pathlib import Path
import random
import sqlite3
from monitoring.security import redact

LOG = logging.getLogger("router")


class SettlementJournal:
    def __init__(self, path=None):
        self.path = Path(path or os.environ.get("LEDGER_RESPONSE_STORE", "results/settlement-recovery.sqlite"))

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        db.execute("CREATE TABLE IF NOT EXISTS responses (attempt_id TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, status TEXT NOT NULL, synced INTEGER NOT NULL DEFAULT 0)")
        return db

    def save(self, attempt_id, payload, status="retrying", synced=False):
        db = self._connect()
        try:
            with db:
                db.execute("INSERT INTO responses VALUES(?,?,?,?) ON CONFLICT(attempt_id) DO UPDATE SET "
                           "payload=excluded.payload,status=excluded.status,synced=excluded.synced",
                           (attempt_id, json.dumps(redact(payload)), status, int(synced)))
        finally:
            db.close()

    def entries(self):
        if not self.path.exists():
            return []
        db = self._connect()
        try:
            return [dict(row, payload=json.loads(row["payload"])) for row in db.execute("SELECT * FROM responses")]
        finally:
            db.close()

    @staticmethod
    def apply(ledger, attempt, payload):
        ledger.resolve(attempt, payload["resolved_model"])
        return ledger.settle(attempt, Decimal(payload["actual"]), payload["input_tokens"],
                             payload["output_tokens"], error=payload["error"], rejected=payload["rejected"])

    def recover(self, ledger):
        """No provider replay. Terminal unknown charges are never refunded."""
        for row in self.entries():
            if row["synced"]:
                continue
            attempt, payload = row["attempt_id"], row["payload"]
            try:
                if row["status"] == "unknown":
                    state = ledger.mark_unknown(attempt)
                else:
                    state = self.apply(ledger, attempt, payload)
                status = "unknown" if state == "unknown" else "settlement_retried"
                response = payload.get("response")
                if response:
                    # Preserve an explicit deadline/error outcome already recorded.
                    with ledger.transaction() as db:
                        db.execute("UPDATE requests SET outcome='200',final_model=?,tier=? "
                                   "WHERE request_id=? AND outcome='pending'",
                                   (response["model"], response["tier"], response["request_id"]))
                self.save(attempt, payload, status, synced=True)
            except Exception:
                LOG.warning("settlement_recovery_pending attempt_id=%s", attempt)


async def settle_response(ledger, journal, attempt, result, actual, reserved, *, error=None,
                          rejected=False, response=None):
    payload = dict(resolved_model=result.resolved_model, input_tokens=result.input_tokens,
                   output_tokens=result.output_tokens, actual=str(actual), reserved=str(reserved),
                   error=error, rejected=rejected, response=response,
                   content=result.content, stop_reason=result.stop_reason)
    try:
        state = journal.apply(ledger, attempt, payload)
        return ("unknown", reserved) if state == "unknown" else ("settled", actual)
    except Exception:
        # Persist before any retry, with a FULL SQLite commit; never log the answer.
        LOG.error("settlement_failed attempt_id=%s", attempt)
    try:
        journal.save(attempt, payload)
        persisted = True
    except Exception:
        persisted = False
        LOG.critical("response_journal_unavailable attempt_id=%s", attempt)
        if response is not None:
            response["recovery_persisted"] = False
    for retry in range(2):
        await asyncio.sleep(random.uniform(.01, .03) * (2 ** retry))
        try:
            state = journal.apply(ledger, attempt, payload)
        except Exception:
            continue
        status = "unknown" if state == "unknown" else "settlement_retried"
        if persisted:
            try:
                # Keep unsynced until request outcome has also been reconciled.
                journal.save(attempt, payload, status)
            except Exception:
                LOG.critical("response_journal_update_failed attempt_id=%s", attempt)
        return status, reserved if status == "unknown" else actual
    # Record the conservative decision locally before attempting its SQL mirror.
    if persisted:
        try:
            journal.save(attempt, payload, "unknown")
        except Exception:
            LOG.critical("response_journal_update_failed attempt_id=%s", attempt)
    try:
        state = ledger.mark_unknown(attempt)
        if state == "settled":
            return "settlement_retried", actual  # lost commit acknowledgement
    except Exception:
        LOG.warning("settlement_unknown_pending attempt_id=%s", attempt)
    return "unknown", reserved
