from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import tempfile
import unittest
from uuid import uuid4
from api.contracts import Request, RouterError
from cache.exact import key, MemoryRate, RedisRate
from fallback.providers import MockProvider, ProviderError
from monitoring.ledger import SQLiteLedger, SpendExceeded, StorageUnavailable
from monitoring.security import redact
from routing.config import Config
from routing.service import Router


def req(text="hello", **kw):
    return Request.parse(dict(messages=[dict(role="user", content=text)], **kw))


class LedgerTests(unittest.TestCase):
    def reserve(self, ledger, attempt="a", purpose="inference", amount="1"):
        ledger.begin("r")
        ledger.reserve(attempt, "r", "model", "mock", purpose, "v1", Decimal(amount))

    def test_concurrent_admission(self):
        ledger = SQLiteLedger(cap="5")
        ledger.begin("r")
        def admit(_):
            try:
                ledger.reserve(str(uuid4()), "r", "m", "mock", "inference", "v1", Decimal("1"))
                return True
            except SpendExceeded:
                return False
        with ThreadPoolExecutor(max_workers=20) as pool:
            self.assertEqual(sum(pool.map(admit, range(50))), 5)
        self.assertEqual(sum(Decimal(str(a["cost"])) for a in ledger.attempts()), 5)

    def test_crashes_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = tmp + "/ledger.sqlite"
            ledger = SQLiteLedger(path, clock=lambda: 0)
            self.reserve(ledger, "reserved")
            self.reserve(ledger, "dispatched")
            ledger.dispatch("dispatched")
            self.assertFalse(ledger.barrier())
            ledger.db.close()
            ledger = SQLiteLedger(path, clock=lambda: 59)
            self.assertEqual(ledger.recover(), 0)
            ledger.clock = lambda: 61
            self.assertEqual(ledger.recover(), 2)
            self.assertEqual(ledger.recover(), 0)
            self.assertTrue(ledger.barrier())
            self.assertEqual([a["state"] for a in ledger.attempts()], ["unknown", "unknown"])
            self.assertEqual(Decimal(str(ledger.requests()[0]["inference_cost"])), 2)
            with self.assertRaises(ValueError):
                self.reserve(ledger, "dispatched")
            ledger.db.close()

    def test_settlement_idempotency_and_judge_attribution(self):
        ledger = SQLiteLedger()
        self.reserve(ledger)
        ledger.dispatch("a")
        ledger.settle("a", Decimal(".1"), 10, 10)
        ledger.settle("a", Decimal(".1"), 10, 10)
        self.reserve(ledger, "j", "evaluation")
        ledger.dispatch("j")
        ledger.job("j", "r")
        self.assertFalse(ledger.barrier())
        ledger.settle("j", Decimal(".2"), 20, 20)
        ledger.job("j", "r", "completed", 4)
        self.assertFalse(ledger.job("j", "r", "completed", 4))
        self.assertTrue(ledger.barrier())
        request = ledger.requests()[0]
        self.assertEqual(Decimal(str(request["inference_cost"])), Decimal(".1"))
        self.assertEqual(Decimal(str(request["evaluation_cost"])), Decimal(".2"))
        self.assertEqual(len(ledger.requests()), 1)
        self.assertEqual(len(ledger.attempts()), 2)

    def test_redaction(self):
        value = redact({"Authorization": "Bearer secret", "raw": "api_key=secret sk-abcdef"})
        self.assertNotIn("secret", str(value))
        self.assertNotIn("sk-abcdef", str(value))


class CacheTests(unittest.TestCase):
    def test_adversarial(self):
        base = req("send 10 to Alice then Bob")
        k = key(base, "m", "p1")
        alternatives = [req("do not send 10 to Alice then Bob"), req("send 10 to Bob then Alice"),
                        req("send 11 to Alice then Bob"), req("```send 10 to Alice then Bob"),
                        Request.parse(dict(messages=[dict(role="system", content="different"),
                                                     *base.messages]))]
        for r in alternatives:
            self.assertNotEqual(k, key(r, "m", "p1"))
        self.assertNotEqual(k, key(base, "m2", "p1"))
        self.assertNotEqual(k, key(base, "m", "p2"))
        self.assertNotEqual(k, key(req(base.messages[0]["content"], max_tokens=3), "m", "p1"))
        self.assertNotEqual(k, key(req(base.messages[0]["content"], temperature=0), "m", "p1"))

    def test_rate_fail_open(self):
        class Down:
            def eval(self, *args):
                raise ConnectionError()
        self.assertTrue(RedisRate(Down()).allow("m", 1))
        rate = MemoryRate(clock=lambda: 1)
        self.assertTrue(rate.allow("m", 1))
        self.assertFalse(rate.allow("m", 1))


class RoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ledger = SQLiteLedger()
        self.config = Config()
        self.provider = MockProvider()
        self.router = Router(self.ledger, provider=self.provider)

    async def status(self, expected, **kwargs):
        with self.assertRaises(RouterError) as e:
            await self.router.route(req(**kwargs))
        self.assertEqual(e.exception.status, expected)
        self.assertTrue(e.exception.request_id)

    async def test_success_cache_and_ledger(self):
        a = await self.router.route(req())
        await self.router.drain_background()
        b = await self.router.route(req())
        self.assertFalse(a["cache_hit"])
        self.assertTrue(b["cache_hit"])
        self.assertEqual(b["cost"], 0)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.ledger.requests()), 2)
        self.assertEqual(len(self.ledger.attempts()), 1)

    async def test_outages(self):
        self.ledger.available = False
        await self.status(503)
        self.assertEqual(self.provider.calls, [])
        self.ledger.available = True
        await self.router.route(req())
        await self.router.drain_background()
        self.ledger.available = False
        self.assertTrue((await self.router.route(req()))["cache_hit"])

    async def test_cache_down_rate_down_cap_enforced(self):
        self.router.cache.available = False
        self.router.rate.available = False
        self.assertFalse((await self.router.route(req()))["cache_hit"])
        self.ledger.cap = Decimal(0)
        await self.status(503, text="another")
        self.assertEqual(len(self.provider.calls), 1)

    async def test_budget(self):
        await self.status(402, max_cost_usd=0)
        self.assertEqual(self.provider.calls, [])

    async def test_fallback_error_matrix(self):
        for status, count in ((408, 2), (429, 2), (500, 2), (400, 1), (401, 1), (403, 1), (404, 1)):
            with self.subTest(status=status):
                ledger = SQLiteLedger()
                model = self.config.model("cheap")["id"]
                provider = MockProvider({model: [ProviderError(status, "Bearer secret sk-test")] * count})
                response = await Router(ledger, provider=provider).route(req())
                self.assertEqual(response["fallback_count"], count)
                self.assertEqual(len(ledger.attempts()), count + 1)
                self.assertNotIn("secret", str(ledger.attempts()))
                self.assertEqual(len(ledger.requests()), 1)

    async def test_total_failure(self):
        self.provider.actions = {m["id"]: [ProviderError(500)] * 2 for m in self.config.models.values()}
        await self.status(503)
        self.assertEqual(len(self.provider.calls), 6)

    async def test_deadline(self):
        self.provider.actions = {m["id"]: [.1] * 2 for m in self.config.models.values()}
        with self.assertRaises(RouterError) as caught:
            await self.router.route(req(deadline_ms=20), use_cache=False)
        self.assertEqual(caught.exception.status, 504)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(self.ledger.attempts()[0]["state"], "dispatched")

    async def test_settlement_failure(self):
        def fail(*args, **kw):
            raise StorageUnavailable()
        self.ledger.settle = fail
        with tempfile.TemporaryDirectory() as directory:
            from monitoring.settlement import SettlementJournal
            self.router.settlement_journal = SettlementJournal(directory + "/responses.sqlite")
            with self.assertLogs("router", level="ERROR") as logs:
                response = await self.router.route(req(), use_cache=False)
            self.assertEqual(response["settlement_status"], "unknown")
            self.assertTrue(response["content"])
            self.assertIn("attempt_id", str(logs.output))
            self.assertEqual(self.ledger.attempts()[0]["state"], "unknown")

    async def test_no_eligible_fallback(self):
        self.router.rate.allow = lambda *a: False
        await self.status(503)
        self.assertEqual(self.provider.calls, [])

    async def test_validation_before_spend(self):
        with self.assertRaises(RouterError):
            await self.router.handle({})
        self.assertEqual(len(self.ledger.requests()), 1)
        self.assertEqual(self.provider.calls, [])
