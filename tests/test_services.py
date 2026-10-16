"""Opt-in LOCAL infrastructure checks. No real provider paths are invoked."""
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import os
import unittest
from uuid import uuid4
from monitoring.ledger import PostgresLedger, SpendExceeded


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
