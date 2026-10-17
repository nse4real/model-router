"""Opt-in LOCAL infrastructure checks. No real provider paths are invoked."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
import os
import time
import unittest
from uuid import uuid4
from monitoring.ledger import PostgresLedger, SpendExceeded, StorageUnavailable


@contextmanager
def isolated_postgres():
    import psycopg
    from psycopg.conninfo import make_conninfo
    schema = "router_test_" + uuid4().hex
    dsn = os.environ["ROUTER_TEST_POSTGRES_DSN"]
    with psycopg.connect(dsn) as conn:
        conn.execute("CREATE SCHEMA " + schema)
    try:
        yield make_conninfo(dsn, options="-c search_path=" + schema, application_name=schema)
    finally:
        with psycopg.connect(dsn) as conn:
            conn.execute("DROP SCHEMA " + schema + " CASCADE")


@unittest.skipUnless(os.environ.get("ROUTER_TEST_POSTGRES_DSN"), "Local Postgres DSN not supplied")
class PostgresIntegrationTests(unittest.TestCase):
    def test_cross_worker_admission(self):
        import psycopg
        from psycopg.conninfo import make_conninfo
        schema = "router_test_" + uuid4().hex
        dsn = os.environ["ROUTER_TEST_POSTGRES_DSN"]
        with psycopg.connect(dsn) as conn:
            conn.execute("CREATE SCHEMA " + schema)
        isolated = make_conninfo(dsn, options="-c search_path=" + schema)
        try:
            a = PostgresLedger(isolated,cap="3")
            b = PostgresLedger(isolated,cap="3")
            a.begin("r")
            def admit(i):
                try:
                    [a,b][i%2].reserve(str(uuid4()),"r","m","mock","inference","v1",Decimal("1"))
                    return True
                except SpendExceeded:
                    return False
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(admit,range(20))),3)
            self.assertEqual(len(a.attempts()),3)
        finally:
            with psycopg.connect(dsn) as conn:
                conn.execute("DROP SCHEMA " + schema + " CASCADE")


    def test_snapshot_column_migrates_existing_postgres_ledger(self):
        import psycopg
        with isolated_postgres() as dsn:
            ledger = PostgresLedger(dsn)
            ledger.begin("r")
            ledger.reserve("a", "r", "configured-alias", "mock", "inference", "v", "0")
            with psycopg.connect(dsn) as conn:
                conn.execute("ALTER TABLE attempts DROP COLUMN resolved_model")
            migrated = PostgresLedger(dsn)
            attempt = migrated.attempts()[0]
            self.assertEqual(attempt["model"], "configured-alias")
            self.assertIsNone(attempt["resolved_model"])
            migrated.resolve("a", "stub-snapshot")
            migrated.settle("a", "0", 2, 3)
            reopened = PostgresLedger(dsn)
            attempt = reopened.attempts()[0]
            self.assertEqual(attempt["model"], "configured-alias")
            self.assertEqual(attempt["resolved_model"], "stub-snapshot")
            self.assertEqual(attempt["state"], "settled")

    def test_lock_wait_over_200ms_succeeds(self):
        import psycopg
        with isolated_postgres() as dsn:
            ledger = PostgresLedger(dsn)
            ledger.begin("r")
            with ThreadPoolExecutor(max_workers=1) as pool:
                with psycopg.connect(dsn) as blocker, psycopg.connect(dsn, autocommit=True) as observer:
                    blocker.execute("SELECT id FROM spend_lock WHERE id=1 FOR UPDATE")
                    future = pool.submit(ledger.reserve, "a", "r", "m", "mock", "inference", "v1", "1")
                    # Observe the actual blocked backend before starting the hold
                    # interval; connection/thread startup cannot shorten the wait.
                    stop = time.monotonic() + 3
                    while not observer.execute(
                        "SELECT 1 FROM pg_stat_activity WHERE datname=current_database() "
                        "AND application_name=current_setting('application_name') "
                        "AND wait_event_type='Lock'"
                    ).fetchone():
                        if future.done():
                            future.result()
                            self.fail("Reservation should wait for the spend lock")
                        self.assertLess(time.monotonic(), stop, "Waiter never reached the lock")
                        time.sleep(.01)
                    time.sleep(.3)
                future.result(timeout=3)
            self.assertEqual(len(ledger.attempts()), 1)
            self.assertEqual(ledger.attempts()[0]["cost"], Decimal("1"))

    def test_lock_budget_exhaustion_fails_closed_with_private_diagnostics(self):
        import psycopg
        with isolated_postgres() as dsn:
            ledger = PostgresLedger(dsn, lock_timeout_ms=50)
            ledger.begin("r")
            with psycopg.connect(dsn) as blocker:
                blocker.execute("SELECT id FROM spend_lock WHERE id=1 FOR UPDATE")
                with self.assertLogs("monitoring.ledger", level="ERROR") as logs:
                    with self.assertRaises(StorageUnavailable) as error:
                        ledger.reserve("a", "r", "m", "mock", "inference", "v1", "1")
            self.assertEqual(str(error.exception), "Spend storage unavailable")
            self.assertIn("error_class=LockNotAvailable sqlstate=55P03", logs.output[0])
            self.assertIsInstance(logs.records[0].storage_exception, psycopg.errors.LockNotAvailable)
            self.assertNotIn("canceling statement", logs.output[0])
            self.assertEqual(ledger.attempts(), [])


@unittest.skipUnless(os.environ.get("ROUTER_TEST_REDIS_URL"), "Local Redis URL not supplied")
class RedisIntegrationTests(unittest.TestCase):
    def test_exact_rate_and_probe_lease(self):
        import redis
        from cache.exact import RedisCache, RedisRate
        from routing.latency import RedisHealthStore, Health
        client = redis.Redis.from_url(os.environ["ROUTER_TEST_REDIS_URL"],decode_responses=True)
        namespace = "router:test:" + uuid4().hex + ":"
        model = "test-" + uuid4().hex
        try:
            cache = RedisCache(client,namespace)
            cache.put("key", {"content":"mock"})
            self.assertEqual(cache.get("key")["content"],"mock")
            rate = RedisRate(client)
            self.assertTrue(rate.allow(model,1))
            self.assertFalse(rate.allow(model,1))
            store = RedisHealthStore(client)
            a,b = Health(store,slo_ms=1),Health(RedisHealthStore(client),slo_ms=1)
            for _ in range(20):
                a.observe(model,a.acquire(model),True,2)
            self.assertIsNone(b.acquire(model))
            state = store.keys(model)[0]
            import json
            value = json.loads(client.get(state))
            value["until"] = 0
            client.set(state,json.dumps(value))
            permit = a.acquire(model)
            self.assertTrue(permit.probe)
            self.assertIsNone(b.acquire(model))
            self.assertTrue(store.renew(model,permit.token))
            self.assertTrue(a.observe(model,permit,True,1))
            self.assertFalse(b.acquire(model).probe)
        finally:
            keys = list(client.scan_iter(match=namespace+"*"))
            keys += list(client.scan_iter(match="router:*"+model+"*"))
            if keys:
                client.delete(*keys)
