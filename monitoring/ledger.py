"""Shared ledger contract. SQLiteLedger is a transactional test double, not Postgres proof."""
from contextlib import contextmanager
from decimal import Decimal
from pathlib import Path
import json
import sqlite3
import threading
import time
from monitoring.security import redact


class StorageUnavailable(Exception):
    pass


class SpendExceeded(Exception):
    pass


class Ledger:
    def __init__(self, cap="25", evaluation_cap="3", clock=time.time):
        self.cap = Decimal(cap)
        self.evaluation_cap = Decimal(evaluation_cap)
        if not 0 <= self.cap <= 25 or not 0 <= self.evaluation_cap <= 3:
            raise ValueError("Caps must not exceed USD 25 global / USD 3 evaluation")
        self.clock = clock
        self.available = True

    def ping(self):
        with self.transaction() as db:
            db.execute("SELECT id FROM spend_lock WHERE id=1")

    def begin(self, request_id, run_id=None):
        with self.transaction() as db:
            db.execute("INSERT INTO requests(request_id, run_id, created_at) VALUES(?,?,?) "
                       "ON CONFLICT (request_id) DO NOTHING", (request_id, run_id, self.clock()))

    @staticmethod
    def _total(rows, purpose=None):
        return sum((Decimal(str(r["cost"])) for r in rows
                    if purpose is None or r["purpose"] == purpose), Decimal(0))

    def check(self, amount):
        with self.transaction(lock=True) as db:
            rows = db.execute("SELECT cost,purpose FROM attempts").fetchall()
            if self._total(rows) + Decimal(amount) > self.cap:
                raise SpendExceeded("Global cap exceeded")

    def reserve(self, attempt_id, parent, model, provider, purpose, version, amount):
        amount = Decimal(amount)
        if not amount.is_finite() or amount < 0:
            raise ValueError("Invalid reservation")
        with self.transaction(lock=True) as db:
            existing = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if existing:
                raise ValueError("Attempt IDs cannot be replayed")
            rows = db.execute("SELECT cost,purpose FROM attempts").fetchall()
            if self._total(rows) + amount > self.cap:
                raise SpendExceeded("Global cap exceeded")
            if purpose == "evaluation" and self._total(rows, "evaluation") + amount > self.evaluation_cap:
                raise SpendExceeded("Evaluation cap exceeded")
            db.execute("INSERT INTO attempts(attempt_id,parent_request_id,model,provider,purpose,"
                       "price_version,reserved,cost,state,cost_status,created_at) "
                       "VALUES(?,?,?,?,?,?,?,?,'reserved','estimated',?)",
                       (attempt_id, parent, model, provider, purpose, version,
                        str(amount), str(amount), self.clock()))
            self._refresh(db, parent)

    def dispatch(self, attempt_id):
        with self.transaction() as db:
            changed = db.execute("UPDATE attempts SET state='dispatched' "
                                 "WHERE attempt_id=? AND state='reserved'", (attempt_id,)).rowcount
            if changed != 1:
                raise ValueError("Attempt is not a fresh reservation")

    def settle(self, attempt_id, cost, input_tokens, output_tokens, error=None):
        with self.transaction(lock=True) as db:
            row = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not row:
                raise ValueError("Unknown attempt")
            if row["state"] in ("settled", "unknown"):
                return  # idempotent; recovery is never silently undone
            if Decimal(cost) > Decimal(str(row["reserved"])) or Decimal(cost) < 0:
                raise ValueError("Usage exceeds conservative reservation")
            db.execute("UPDATE attempts SET state='settled',cost=?,input_tokens=?,output_tokens=?,"
                       "cost_status='measured',error=? WHERE attempt_id=?",
                       (str(cost), input_tokens, output_tokens,
                        json.dumps(redact(error)) if error else None, attempt_id))
            self._refresh(db, row["parent_request_id"])

    def error(self, attempt_id, error):
        with self.transaction() as db:
            db.execute("UPDATE attempts SET error=? WHERE attempt_id=?",
                       (json.dumps(redact(error)), attempt_id))

    def recover(self, timeout=30, grace=30):
        with self.transaction(lock=True) as db:
            rows = db.execute("SELECT * FROM attempts WHERE state IN ('reserved','dispatched') "
                              "AND created_at <= ?", (self.clock() - timeout - grace,)).fetchall()
            for row in rows:
                db.execute("UPDATE attempts SET state='unknown',cost=reserved,cost_status='unknown' "
                           "WHERE attempt_id=?", (row["attempt_id"],))
                self._refresh(db, row["parent_request_id"])
                db.execute("UPDATE requests SET outcome='503' WHERE request_id=? "
                           "AND outcome='pending'", (row["parent_request_id"],))
            jobs = db.execute("SELECT * FROM judge_jobs WHERE state IN ('queued','running') "
                              "AND created_at <= ?", (self.clock()-timeout-grace,)).fetchall()
            for job in jobs:
                db.execute("UPDATE judge_jobs SET state='quality_unknown' WHERE attempt_id=?",
                           (job["attempt_id"],))
                db.execute("UPDATE requests SET quality_status='quality_unknown' WHERE request_id=?",
                           (job["parent_request_id"],))
            return len(rows)

    def _refresh(self, db, parent):
        rows = db.execute("SELECT cost,purpose FROM attempts WHERE parent_request_id=?", (parent,)).fetchall()
        db.execute("UPDATE requests SET inference_cost=?,evaluation_cost=? WHERE request_id=?",
                   (str(self._total(rows, "inference")), str(self._total(rows, "evaluation")), parent))

    def finish(self, request_id, **fields):
        allowed = {"outcome", "tier", "final_model", "latency_ms", "cache_hit", "fallback_count",
                   "quality_status"}
        if set(fields) - allowed:
            raise ValueError("Unknown request columns")
        with self.transaction() as db:
            if fields:
                db.execute("UPDATE requests SET " + ",".join(k + "=?" for k in fields) +
                           " WHERE request_id=?", (*fields.values(), request_id))
            self._refresh(db, request_id)

    def attempts(self, parent=None):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM attempts" +
                    (" WHERE parent_request_id=?" if parent else ""), (parent,) if parent else ()).fetchall()]

    def requests(self, run_id=None):
        with self.transaction() as db:
            return [dict(r) for r in db.execute("SELECT * FROM requests" +
                    (" WHERE run_id=?" if run_id else ""), (run_id,) if run_id else ()).fetchall()]

    def claim_job(self, attempt_id):
        with self.transaction() as db:
            return db.execute("UPDATE judge_jobs SET state='running' WHERE attempt_id=? "
                              "AND state='queued'", (attempt_id,)).rowcount == 1

    def job(self, attempt_id, parent, state="queued", score=None):
        with self.transaction() as db:
            old = db.execute("SELECT state FROM judge_jobs WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old and old["state"] in ("completed", "failed", "quality_unknown"):
                return False
            db.execute("INSERT INTO judge_jobs(attempt_id,parent_request_id,state,score,created_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(attempt_id) DO UPDATE SET state=excluded.state,score=excluded.score",
                       (attempt_id, parent, state, score, self.clock()))
            db.execute("UPDATE requests SET quality_status=? WHERE request_id=?", (state, parent))
            return True

    def barrier(self, run_id=None):
        with self.transaction() as db:
            suffix = " AND r.run_id=?" if run_id else ""
            params = (run_id,) if run_id else ()
            a = db.execute("SELECT COUNT(*) AS n FROM attempts a JOIN requests r ON "
                           "r.request_id=a.parent_request_id WHERE a.state NOT IN ('settled','unknown')" +
                           suffix, params).fetchone()["n"]
            j = db.execute("SELECT COUNT(*) AS n FROM judge_jobs j JOIN requests r ON "
                           "r.request_id=j.parent_request_id WHERE j.state NOT IN "
                           "('completed','failed','quality_unknown')" + suffix, params).fetchone()["n"]
            return a == 0 and j == 0


class SQLiteLedger(Ledger):
    def __init__(self, path=":memory:", **kwargs):
        super().__init__(**kwargs)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys=ON")
        self.mutex = threading.RLock()
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(judge_jobs)")}
        if "created_at" not in columns:
            self.db.execute("ALTER TABLE judge_jobs ADD COLUMN created_at NUMERIC NOT NULL DEFAULT 0")

    def close(self):
        self.db.close()

    def __del__(self):
        try:
            self.db.close()
        except Exception:
            pass

    @contextmanager
    def transaction(self, lock=False):
        if not self.available:
            raise StorageUnavailable("Spend storage unavailable")
        with self.mutex:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                yield self.db
                self.db.execute("COMMIT")
            except Exception as exc:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise StorageUnavailable("Spend storage unavailable") from None
                raise


class _PostgresSession:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s"), params)


class PostgresLedger(Ledger):
    def __init__(self, dsn, **kwargs):
        super().__init__(**kwargs)
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.dsn = dsn
        self.initialized = False

    @contextmanager
    def transaction(self, lock=False):
        if not self.available:
            raise StorageUnavailable("Spend storage unavailable")
        try:
            with self.psycopg.connect(self.dsn, row_factory=self.dict_row, connect_timeout=2) as conn:
                db = _PostgresSession(conn)
                if not self.initialized:
                    for statement in Path(__file__).with_name("schema.sql").read_text().split(";"):
                        if statement.strip():
                            db.execute(statement)
                    db.execute("ALTER TABLE judge_jobs ADD COLUMN IF NOT EXISTS "
                               "created_at NUMERIC NOT NULL DEFAULT 0")
                if lock:
                    db.execute("SELECT id FROM spend_lock WHERE id=1 FOR UPDATE")
                yield db
            self.initialized = True
        except self.psycopg.Error:
            raise StorageUnavailable("Spend storage unavailable") from None
