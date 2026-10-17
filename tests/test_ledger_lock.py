"""Spend-lock budgets and private driver diagnostics, without external services."""
from contextlib import contextmanager
from types import SimpleNamespace
import queue
import unittest

from api.contracts import Request, RouterError
from fallback.providers import MockProvider
from monitoring.ledger import Ledger, PostgresLedger, SQLiteLedger, SpendExceeded, StorageUnavailable
from routing.service import Router


class DriverError(Exception):
    sqlstate = "55P03"


class Connection:
    def __init__(self, wait_ms=0, failure=None):
        self.commands = []
        self.settings = {}
        self.now = 0
        self.wait_ms = wait_ms
        self.failure = failure

    closed = False
    info = SimpleNamespace(transaction_status=0)  # idle; ledger returns it to the pool

    def commit(self):
        if self.failure == "commit":
            raise DriverError("private DSN password and prompt text")

    def rollback(self):
        pass

    def close(self):
        self.closed = True

    @contextmanager
    def transaction(self):
        self.commands.append(("bootstrap_begin", ()))
        yield
        self.commands.append(("bootstrap_commit", ()))

    def execute(self, sql, params=()):
        self.commands.append((sql, params))
        if sql.startswith("SELECT set_config("):
            self.settings[sql.split("'")[1]] = int(params[0].removesuffix("ms"))
        if sql == "SELECT id FROM spend_lock WHERE id=1 FOR UPDATE":
            statement = self.settings.get("statement_timeout", 0)
            budget = self.settings.get("lock_timeout", 0)
            if (statement and statement < self.wait_ms) or (budget and budget < self.wait_ms):
                raise DriverError("private DSN password and prompt text")
            self.now += self.wait_ms / 1000
        if self.failure == "schema" and "CREATE TABLE" in sql:
            raise DriverError("private DSN password and prompt text")
        return SimpleNamespace(fetchall=lambda: [])


def fake_ledger(conn, lock_timeout_ms=2000, initialized=True):
    ledger = PostgresLedger.__new__(PostgresLedger)
    Ledger.__init__(ledger)
    ledger._idle = queue.LifoQueue(maxsize=32)
    ledger.lock_timeout_ms = lock_timeout_ms
    ledger.dsn, ledger.dict_row, ledger.initialized = "private DSN", None, initialized
    ledger.psycopg = SimpleNamespace(connect=lambda *a, **kw: conn, Error=DriverError)
    return ledger


class PostgresLockBudgetTests(unittest.TestCase):
    def test_wait_over_statement_budget_succeeds_and_recomputes_deadline(self):
        conn = Connection(wait_ms=300)
        ledger = fake_ledger(conn)
        with ledger.deadline(.35, clock=lambda: conn.now):
            with ledger.transaction(lock=True):
                self.assertGreater(conn.settings["statement_timeout"], 0)
                self.assertEqual(conn.settings["statement_timeout"], 2000)
        lock = conn.commands.index(("SELECT id FROM spend_lock WHERE id=1 FOR UPDATE", ()))
        self.assertIn(("SELECT set_config('lock_timeout', %s, true)", ("2000ms",)),
                      conn.commands[:lock])
        self.assertEqual(conn.commands[lock - 1],
                         ("SELECT set_config('statement_timeout', %s, true)", ("0",)))

    def test_expired_deadline_keeps_independent_settlement_budget(self):
        conn = Connection(wait_ms=300)
        ledger = fake_ledger(conn)
        with ledger.deadline(.005, clock=lambda: conn.now):
            with ledger.transaction(lock=True):
                self.assertEqual(conn.settings["statement_timeout"], 2000)
                self.assertEqual(conn.settings["lock_timeout"], 2000)

    def test_configured_lock_budget_fails_closed_and_logs_private_cause(self):
        conn = Connection(wait_ms=300)
        ledger = fake_ledger(conn, lock_timeout_ms=25)
        with self.assertLogs("monitoring.ledger", level="ERROR") as logs:
            with self.assertRaises(StorageUnavailable) as error:
                with ledger.transaction(lock=True):
                    self.fail("Timed-out lock must not yield a session")
        self.assertEqual(str(error.exception), "Spend storage unavailable")
        self.assertTrue(error.exception.__suppress_context__)
        self.assertIn("error_class=DriverError sqlstate=55P03", logs.output[0])
        self.assertIs(logs.records[0].storage_exception, error.exception.__context__)
        self.assertIsNone(logs.records[0].exc_info)
        self.assertNotIn("private DSN", logs.output[0])
        self.assertNotIn("password", logs.output[0])
        self.assertNotIn("prompt text", logs.output[0])

    def test_connection_schema_and_commit_errors_log_only_safe_metadata(self):
        for phase in ("connect", "schema", "commit"):
            with self.subTest(phase=phase):
                conn = Connection(failure=phase)
                ledger = fake_ledger(conn, initialized=phase != "schema")
                cause = DriverError("private DSN password and prompt text")
                if phase == "connect":
                    def fail_connect(*args, **kwargs):
                        raise cause
                    ledger.psycopg.connect = fail_connect
                with self.assertLogs("monitoring.ledger", level="ERROR") as logs:
                    with self.assertRaises(StorageUnavailable) as error:
                        with ledger.transaction():
                            pass
                self.assertIs(logs.records[0].storage_exception, error.exception.__context__)
                self.assertEqual(str(error.exception), "Spend storage unavailable")
                self.assertNotIn("private DSN", logs.output[0])
                self.assertNotIn("password", logs.output[0])
                self.assertNotIn("prompt text", logs.output[0])

    def test_bootstrap_commits_before_cap_rejection_and_is_not_repeated(self):
        conn = Connection()
        ledger = fake_ledger(conn, initialized=False)
        with self.assertRaises(SpendExceeded):
            with ledger.transaction(lock=True):
                self.assertIn(("bootstrap_commit", ()), conn.commands)
                raise SpendExceeded("Global cap exceeded")
        self.assertTrue(ledger.initialized)
        with ledger.transaction(lock=True):
            pass
        self.assertEqual(conn.commands.count(("bootstrap_begin", ())), 1)
        bootstrap_lock = next(i for i, (sql, _) in enumerate(conn.commands)
                              if "pg_advisory_xact_lock" in sql)
        self.assertLess(bootstrap_lock, conn.commands.index(("bootstrap_commit", ())))

    def test_bootstrap_commits_before_repeatable_read_snapshot(self):
        conn = Connection()
        ledger = fake_ledger(conn, initialized=False)
        with ledger.transaction(snapshot=True):
            pass
        self.assertLess(conn.commands.index(("bootstrap_commit", ())),
                        conn.commands.index(("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ", ())))

    def test_invalid_lock_budgets_are_rejected(self):
        for budget in (0, -1, True, 1.5, "2000"):
            with self.subTest(budget=budget), self.assertRaises(ValueError):
                PostgresLedger("unused", lock_timeout_ms=budget)


class RequestDeadlineAfterLockTests(unittest.IsolatedAsyncioTestCase):
    async def test_wait_exhausting_deadline_prevents_dispatch(self):
        now = [0]
        class WaitingLedger(SQLiteLedger):
            def reserve(self, *args):
                super().reserve(*args)
                now[0] += .3
        ledger = WaitingLedger()
        provider = MockProvider()
        router = Router(ledger, provider=provider, clock=lambda: now[0])
        request = Request.parse({"messages": [{"role": "user", "content": "hello"}],
                                 "deadline_ms": 5})
        with self.assertRaises(RouterError) as error:
            await router.route(request, use_cache=False)
        self.assertEqual(error.exception.status, 504)
        self.assertEqual(provider.calls, [])
        self.assertEqual(ledger.attempts()[0]["state"], "reserved")
        ledger.close()
