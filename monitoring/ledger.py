"""Shared ledger contract. SQLiteLedger is a transactional test double, not Postgres proof."""
from contextlib import contextmanager
from contextvars import ContextVar
from decimal import Decimal
from pathlib import Path
import json
import logging
import os
import random
import sqlite3
import threading
import time
from monitoring.security import redact

DEADLINE = ContextVar("ledger_deadline", default=None)
# Dispatch and provider execution can cross asyncio task boundaries. A shared
# one-use ticket correlates timing with the exact durable attempt, including judges.
ATTEMPT_LATENCY = ContextVar("full_completion_attempt_latency", default=None)
LOG = logging.getLogger(__name__)


class StorageUnavailable(Exception):
    pass


class TransientContention(StorageUnavailable):
    """A rolled-back statement/transaction can be retried with the same identity."""


class SpendExceeded(Exception):
    pass


@contextmanager
def measure_full_completion_latency(clock=time.monotonic):
    """Time provider dispatch through the final response/failure, excluding settlement."""
    ticket = ATTEMPT_LATENCY.get()
    attempt = tuple(ticket) if ticket else None
    if ticket:
        ticket.clear()
    measurement = []
    started = clock()
    try:
        yield measurement
    finally:
        latency_ms = (clock() - started) * 1000
        measurement.append(latency_ms)
        if attempt:
            ledger, attempt_id = attempt
            try:
                ledger.retry_admission(ledger.record_latency, attempt_id, latency_ms)
            except StorageUnavailable:
                LOG.warning("attempt_latency_unavailable attempt_id=%s", attempt_id)


class Ledger:
    def __init__(self, cap="25", evaluation_cap="3", clock=time.time, statement_timeout_ms=None):
        self.cap = Decimal(cap)
        self.evaluation_cap = Decimal(evaluation_cap)
        if not 0 <= self.cap <= 25 or not 0 <= self.evaluation_cap <= 3:
            raise ValueError("Caps must not exceed USD 25 global / USD 3 evaluation")
        self.clock = clock
        self.available = True
        budget = (int(os.environ.get("LEDGER_STATEMENT_TIMEOUT_MS", "2000"))
                  if statement_timeout_ms is None else statement_timeout_ms)
        if type(budget) is not int or budget < 1:
            raise ValueError("Ledger statement timeout must be a positive integer in milliseconds")
        self.timeout_ms = budget

    @contextmanager
    def deadline(self, end, clock=time.monotonic):
        token = DEADLINE.set((end, clock))
        try:
            yield
        finally:
            DEADLINE.reset(token)

    def transaction_timeout(self):
        # Storage has its own budget, including cleanup after a paid response.
        # The router checks its request deadline before dispatch and return.
        return self.timeout_ms

    def retry_admission(self, operation, *args, **kwargs):
        for retry in range(3):
            try:
                return operation(*args, **kwargs)
            except TransientContention:
                deadline = DEADLINE.get()
                delay = random.uniform(.01, .03) * (2 ** retry)
                if retry == 2 or (deadline and deadline[1]() + delay >= deadline[0]):
                    raise
                time.sleep(delay)

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
        with self.transaction(lock=True) as db:
            self._reserve(db, attempt_id, parent, model, provider, purpose, version, amount)

    def _reserve(self, db, attempt_id, parent, model, provider, purpose, version, amount):
        amount = Decimal(amount)
        if not amount.is_finite() or amount < 0:
            raise ValueError("Invalid reservation")
        if purpose == "evaluation":
            job = db.execute("SELECT state FROM judge_jobs WHERE attempt_id=?", (attempt_id,)).fetchone()
            if job and job["state"] in ("completed", "failed", "quality_unknown"):
                raise ValueError("Judge job is terminal")
        existing = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
        if existing:
            identity = (parent, model, provider, purpose, version, amount)
            stored = tuple(existing[k] for k in ("parent_request_id", "model", "provider", "purpose",
                                                "price_version")) + (Decimal(str(existing["reserved"])),)
            if stored == identity and existing["state"] == "reserved":
                return  # Lost commit acknowledgement must never reserve twice.
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

    def claim_and_reserve(self, attempt_id, parent, model, provider, version, amount):
        # Recovery uses this same spend lock. A queued job becomes running only
        # in the transaction that creates its durable paid-call reservation.
        with self.transaction(lock=True) as db:
            changed = db.execute("UPDATE judge_jobs SET state='running' WHERE attempt_id=? "
                                 "AND parent_request_id=? AND state='queued'",
                                 (attempt_id, parent)).rowcount
            if changed != 1:
                return False
            self._reserve(db, attempt_id, parent, model, provider, "evaluation", version, amount)
            return True

    def dispatch(self, attempt_id):
        with self.transaction(lock=True) as db:
            changed = db.execute("UPDATE attempts SET state='dispatched' "
                                 "WHERE attempt_id=? AND state='reserved'", (attempt_id,)).rowcount
            if changed != 1:
                row = db.execute("SELECT state FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
                if not row or row["state"] != "dispatched":
                    raise ValueError("Attempt is not a fresh reservation")
        ATTEMPT_LATENCY.set([self, attempt_id])

    def record_latency(self, attempt_id, latency_ms):
        with self.transaction() as db:
            db.execute("UPDATE attempts SET latency_ms=? WHERE attempt_id=?",
                       (latency_ms, attempt_id))

    def resolve(self, attempt_id, resolved_model):
        # Keep the provider's response identity even if usage settlement or
        # answer validation fails. The model column remains the requested ID.
        if resolved_model is not None:
            with self.transaction() as db:
                db.execute("UPDATE attempts SET resolved_model=? WHERE attempt_id=?",
                           (resolved_model, attempt_id))

    def settle(self, attempt_id, cost, input_tokens, output_tokens, error=None, *, rejected=False):
        with self.transaction(lock=True) as db:
            row = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not row:
                raise ValueError("Unknown attempt")
            if row["state"] in ("settled", "unknown"):
                return row["state"]  # idempotent; recovery is never silently undone
            if rejected and (Decimal(cost) != 0 or input_tokens != 0 or output_tokens != 0):
                raise ValueError("Rejected attempts must have zero cost and usage")
            if Decimal(cost) > Decimal(str(row["reserved"])) or Decimal(cost) < 0:
                raise ValueError("Usage exceeds conservative reservation")
            db.execute("UPDATE attempts SET state='settled',cost=?,input_tokens=?,output_tokens=?,"
                       "cost_status=?,error=? WHERE attempt_id=?",
                       (str(cost), input_tokens, output_tokens,
                        "rejected" if rejected else "measured",
                        json.dumps(redact(error)) if error else None, attempt_id))
            self._refresh(db, row["parent_request_id"])
            return "settled"

    def mark_unknown(self, attempt_id):
        with self.transaction(lock=True) as db:
            row = db.execute("SELECT * FROM attempts WHERE attempt_id=?", (attempt_id,)).fetchone()
            if not row:
                raise ValueError("Unknown attempt")
            if row["state"] != "settled":
                db.execute("UPDATE attempts SET state='unknown',cost=reserved,cost_status='unknown' "
                           "WHERE attempt_id=?", (attempt_id,))
                self._refresh(db, row["parent_request_id"])
                return "unknown"
            return "settled"

    def error(self, attempt_id, error):
        with self.transaction() as db:
            db.execute("UPDATE attempts SET error=? WHERE attempt_id=?",
                       (json.dumps(redact(error)), attempt_id))

    def recover(self, timeout=30, grace=30, *, run_id=None):
        # Omitting run_id retains global maintenance recovery. Benchmarks must
        # scope both attempts and jobs through their durable parent request.
        scope = (" AND parent_request_id IN (SELECT request_id FROM requests WHERE run_id=?)"
                 if run_id is not None else "")
        params = (self.clock() - timeout - grace,) + ((run_id,) if run_id is not None else ())
        with self.transaction(lock=True) as db:
            rows = db.execute("SELECT * FROM attempts WHERE state IN ('reserved','dispatched') "
                              "AND created_at <= ?" + scope, params).fetchall()
            for row in rows:
                db.execute("UPDATE attempts SET state='unknown',cost=reserved,cost_status='unknown' "
                           "WHERE attempt_id=?", (row["attempt_id"],))
                self._refresh(db, row["parent_request_id"])
                db.execute("UPDATE requests SET outcome='503' WHERE request_id=? "
                           "AND outcome='pending'", (row["parent_request_id"],))
            jobs = db.execute("SELECT * FROM judge_jobs WHERE state IN ('queued','running') "
                              "AND created_at <= ?" + scope, params).fetchall()
            for job in jobs:
                active = db.execute("SELECT state FROM attempts WHERE attempt_id=?",
                                    (job["attempt_id"],)).fetchone()
                if active and active["state"] not in ("settled", "unknown"):
                    continue
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
        with self.transaction(lock=True) as db:
            old = db.execute("SELECT state FROM judge_jobs WHERE attempt_id=?", (attempt_id,)).fetchone()
            if old and old["state"] in ("completed", "failed", "quality_unknown"):
                return False
            db.execute("INSERT INTO judge_jobs(attempt_id,parent_request_id,state,score,created_at) VALUES(?,?,?,?,?) "
                       "ON CONFLICT(attempt_id) DO UPDATE SET state=excluded.state,score=excluded.score",
                       (attempt_id, parent, state, score, self.clock()))
            db.execute("UPDATE requests SET quality_status=? WHERE request_id=?", (state, parent))
            return True

    def barrier(self, run_id=None):
        with self.transaction(lock=True) as db:
            suffix = " AND r.run_id=?" if run_id else ""
            params = (run_id,) if run_id else ()
            a = db.execute("SELECT COUNT(*) AS n FROM attempts a JOIN requests r ON "
                           "r.request_id=a.parent_request_id WHERE a.state NOT IN ('settled','unknown')" +
                           suffix, params).fetchone()["n"]
            j = db.execute("SELECT COUNT(*) AS n FROM judge_jobs j JOIN requests r ON "
                           "r.request_id=j.parent_request_id WHERE j.state NOT IN "
                           "('completed','failed','quality_unknown')" + suffix, params).fetchone()["n"]
            return a == 0 and j == 0

    def save_run(self, run_id, metadata):
        with self.transaction() as db:
            db.execute("INSERT INTO benchmark_runs(run_id,metadata) VALUES(?,?) "
                       "ON CONFLICT(run_id) DO NOTHING", (run_id, json.dumps(redact(metadata))))

    def save_record(self, run_id, record):
        with self.transaction() as db:
            db.execute("INSERT INTO requests(request_id,run_id,created_at) VALUES(?,?,?) "
                       "ON CONFLICT(request_id) DO NOTHING",
                       (record["request_id"], run_id, self.clock()))
            db.execute("INSERT INTO benchmark_records(request_id,run_id,record) VALUES(?,?,?) "
                       "ON CONFLICT(request_id) DO UPDATE SET record=excluded.record",
                       (record["request_id"], run_id, json.dumps(redact(record))))

    def load_run(self, run_id=None):
        with self.transaction(snapshot=True) as db:
            runs = db.execute("SELECT * FROM benchmark_runs" + (" WHERE run_id=?" if run_id else ""),
                              (run_id,) if run_id else ()).fetchall()
            if len(runs) != 1:
                raise ValueError("Supply a unique run id for ledger recovery")
            run = runs[0]
            records = db.execute("SELECT record FROM benchmark_records WHERE run_id=?",
                                 (run["run_id"],)).fetchall()
            return run["run_id"], json.loads(run["metadata"]), [json.loads(r["record"]) for r in records]


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
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(attempts)")}
        if "resolved_model" not in columns:
            self.db.execute("ALTER TABLE attempts ADD COLUMN resolved_model TEXT")
        if "latency_ms" not in columns:
            self.db.execute("ALTER TABLE attempts ADD COLUMN latency_ms NUMERIC")

    def close(self):
        self.db.close()

    def __del__(self):
        try:
            self.db.close()
        except Exception:
            pass

    @contextmanager
    def transaction(self, lock=False, snapshot=False):
        if not self.available:
            raise StorageUnavailable("Spend storage unavailable")
        timeout = self.transaction_timeout()
        if not self.mutex.acquire(timeout=timeout / 1000):
            raise TransientContention("Spend storage lock timeout")
        try:
            try:
                self.db.execute(f"PRAGMA busy_timeout={timeout}")
                self.db.execute("BEGIN" if snapshot else "BEGIN IMMEDIATE")
                yield self.db
                self.db.execute("COMMIT")
            except Exception as exc:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
                        raise TransientContention("Spend storage unavailable") from None
                    raise StorageUnavailable("Spend storage unavailable") from None
                raise
        finally:
            self.mutex.release()


class _PostgresSession:
    def __init__(self, conn):
        self.conn = conn

    def execute(self, sql, params=()):
        return self.conn.execute(sql.replace("?", "%s"), params)


class PostgresLedger(Ledger):
    def __init__(self, dsn, lock_timeout_ms=2000, **kwargs):
        super().__init__(**kwargs)
        if type(lock_timeout_ms) is not int or lock_timeout_ms < 1:
            raise ValueError("Spend lock timeout must be a positive integer in milliseconds")
        import psycopg
        from psycopg.rows import dict_row
        self.psycopg = psycopg
        self.dict_row = dict_row
        self.dsn = dsn
        self.lock_timeout_ms = lock_timeout_ms
        self.initialized = False

    def _initialize(self, conn):
        if self.initialized:
            return
        # Commit bootstrap separately: DDL locks must never be carried into
        # reservation work or retried on every cap rejection. The advisory lock
        # serializes first use across instances/processes sharing this schema.
        with conn.transaction():
            db = _PostgresSession(conn)
            db.execute("SELECT set_config('lock_timeout', ?, true)",
                       (str(self.lock_timeout_ms) + "ms",))
            db.execute("SELECT set_config('statement_timeout', ?, true)", ("0",))
            db.execute("SELECT pg_advisory_xact_lock(hashtext(current_schema()), "
                       "hashtext('router_ledger_schema'))")
            if self.initialized:
                return
            statements = [s for s in Path(__file__).with_name("schema.sql").read_text().split(";")
                          if s.strip()]
            # schema.sql creates/seeds the singleton first. Take its lock before
            # any other DDL, matching the order used by established workers.
            for statement in statements[:2]:
                db.execute(statement)
            db.execute("SELECT id FROM spend_lock WHERE id=1 FOR UPDATE")
            db.execute("SELECT set_config('statement_timeout', ?, true)",
                       (str(self.lock_timeout_ms) + "ms",))
            for statement in statements[2:]:
                db.execute(statement)
            db.execute("ALTER TABLE judge_jobs ADD COLUMN IF NOT EXISTS "
                       "created_at NUMERIC NOT NULL DEFAULT 0")
            db.execute("ALTER TABLE attempts ADD COLUMN IF NOT EXISTS resolved_model TEXT")
            db.execute("ALTER TABLE attempts ADD COLUMN IF NOT EXISTS latency_ms NUMERIC")
        self.initialized = True

    @contextmanager
    def transaction(self, lock=False, snapshot=False):
        if not self.available:
            raise StorageUnavailable("Spend storage unavailable")
        try:
            with self.psycopg.connect(self.dsn, row_factory=self.dict_row, connect_timeout=2) as conn:
                self._initialize(conn)
                db = _PostgresSession(conn)
                if snapshot:
                    db.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
                if lock:
                    db.execute("SELECT set_config('lock_timeout', ?, true)",
                               (str(self.lock_timeout_ms) + "ms",))
                    # Only lock_timeout bounds this wait. A shorter statement
                    # timeout (including a DSN default) would defeat that budget.
                    db.execute("SELECT set_config('statement_timeout', ?, true)", ("0",))
                    db.execute("SELECT id FROM spend_lock WHERE id=1 FOR UPDATE")
                # Ordinary statements do not inherit the request deadline.
                timeout = str(self.transaction_timeout()) + "ms"
                db.execute("SELECT set_config('lock_timeout', ?, true)", (timeout,))
                db.execute("SELECT set_config('statement_timeout', ?, true)", (timeout,))
                yield db
        except self.psycopg.Error as exc:
            # Driver messages/tracebacks can contain SQL values or connection
            # details. Attach the cause for diagnostics without formatting it.
            LOG.error("spend_storage_failed error_class=%s sqlstate=%s",
                      type(exc).__name__, exc.sqlstate,
                      extra={"storage_exception": exc})
            error = (TransientContention if exc.sqlstate in {"57014", "55P03", "40001", "40P01"}
                     else StorageUnavailable)
            raise error("Spend storage unavailable") from None
