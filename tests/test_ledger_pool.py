"""Pooled Postgres ledger connections under real concurrency (needs ROUTER_TEST_POSTGRES_DSN)."""
from concurrent.futures import ThreadPoolExecutor
import os
import unittest

from monitoring.ledger import PostgresLedger

DSN = os.environ.get("ROUTER_TEST_POSTGRES_DSN")


@unittest.skipUnless(DSN, "Local Postgres DSN not supplied")
class PooledConnectionTests(unittest.TestCase):
    def setUp(self):
        self.ledger = PostgresLedger(DSN)

    def test_threads_share_a_bounded_pool_and_commit_cleanly(self):
        def work(_):
            with self.ledger.transaction() as db:
                db.execute("SELECT 1")

        with ThreadPoolExecutor(max_workers=16) as pool:
            list(pool.map(work, range(200)))
        idle = self.ledger._idle.qsize()
        self.assertGreaterEqual(idle, 1)
        self.assertLessEqual(idle, 32)

    def test_failed_transaction_never_returns_an_open_connection(self):
        class Boom(Exception):
            pass

        with self.assertRaises(Boom):
            with self.ledger.transaction() as db:
                db.execute("SELECT 1")
                raise Boom()
        for _ in range(self.ledger._idle.qsize()):
            conn = self.ledger._idle.get_nowait()
            self.assertEqual(conn.info.transaction_status, 0)  # IDLE: nothing left open
            self.ledger._idle.put_nowait(conn)


if __name__ == "__main__":
    unittest.main()
