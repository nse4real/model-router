import asyncio
from concurrent.futures import ThreadPoolExecutor
from decimal import Decimal
import unittest
from api.contracts import RouterError
from fallback.providers import MockProvider, Output, ProviderError
from monitoring.ledger import SQLiteLedger
from monitoring.quality import Quality, validate_calibration
from routing.config import Config
from routing.latency import Health, MemoryHealthStore
from routing.service import Router
from tests.test_stage2 import req


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class LatencyTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.store = MemoryHealthStore(self.clock)
        self.health = Health(self.store, slo_ms=100)

    def breach(self):
        for _ in range(20):
            self.health.observe("m", self.health.acquire("m"), True, 101)

    def test_minimum_samples_and_cooldown(self):
        for _ in range(19):
            self.health.observe("m", self.health.acquire("m"), True, 101)
        self.assertIsNotNone(self.health.acquire("m"))
        self.health.observe("m", self.health.acquire("m"), True, 101)
        self.assertIsNone(self.health.acquire("m"))
        self.clock.now = 59
        self.assertIsNone(self.health.acquire("m"))
        self.clock.now = 60
        self.assertTrue(self.health.acquire("m").probe)

    def test_probe_success(self):
        self.breach()
        self.clock.now = 61
        probe = self.health.acquire("m")
        self.assertTrue(self.health.observe("m", probe, True, 100))
        self.assertFalse(self.health.acquire("m").probe)

    def test_probe_latency_only_failure(self):
        self.breach()
        self.clock.now = 61
        self.health.observe("m", self.health.acquire("m"), True, 101)
        self.assertIsNone(self.health.acquire("m"))
        self.clock.now = 122
        self.health.observe("m", self.health.acquire("m"), False, 1)
        self.assertIsNone(self.health.acquire("m"))

    def test_two_workers_one_probe(self):
        self.breach()
        self.clock.now = 61
        workers = [Health(self.store, slo_ms=100), Health(self.store, slo_ms=100)]
        with ThreadPoolExecutor(max_workers=2) as pool:
            permits = list(pool.map(lambda h: h.acquire("m"), workers))
        self.assertEqual(sum(p is not None for p in permits), 1)

    def test_ttl_renew_and_stale_result(self):
        self.breach()
        self.clock.now = 61
        probe = self.health.acquire("m")
        self.clock.now = 100
        self.assertTrue(self.store.renew("m", probe.token))
        self.clock.now = 140
        self.assertIsNone(self.health.acquire("m"))
        self.clock.now = 146
        next_probe = self.health.acquire("m")
        self.assertIsNotNone(next_probe)
        self.assertFalse(self.health.observe("m", probe, True, 1))
        self.assertIsNone(self.health.acquire("m"))


class QualityAndPolicyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config()
        self.ledger = SQLiteLedger()
        self.ledger.begin("r")
        self.provider = MockProvider()
        self.quality = Quality(self.ledger, self.config, self.provider, sample_rate=1)

    async def test_timeout_malformed_cap_and_queue(self):
        for kind in ("timeout", "malformed", "cap", "overflow"):
            with self.subTest(kind=kind):
                ledger = SQLiteLedger(evaluation_cap="0" if kind == "cap" else "3")
                ledger.begin("r")
                provider = MockProvider({self.config.model("mid")["id"]:
                                         [.02 if kind == "timeout" else Output("bad", 2, 1)]})
                q = Quality(ledger, self.config, provider, sample_rate=1, timeout=.001, queue_size=1)
                q.submit("r", "simple", req(), Output("hi", 1, 1))
                if kind == "overflow":
                    ledger.begin("r2")
                    q.submit("r2", "simple", req(), Output("hi", 1, 1))
                await q.drain()
                self.assertTrue(all(r["quality_status"] == "quality_unknown" for r in ledger.requests()))
                if kind == "cap":
                    self.assertEqual(provider.calls, [])

    async def test_valid_judge_attribution_idempotency(self):
        self.provider.actions = {self.config.model("mid")["id"]: [Output('{"score":4}', 5, 4)]}
        self.quality.submit("r", "simple", req(), Output("hi", 1, 1))
        job = self.quality.queue.get_nowait()
        await self.quality.process(job)
        await self.quality.process(job)
        self.assertEqual(len(self.provider.calls), 1)
        self.assertEqual(len(self.ledger.attempts()), 1)
        self.assertEqual(self.ledger.attempts()[0]["purpose"], "evaluation")
        self.assertEqual(self.ledger.requests()[0]["quality_status"], "completed")

    async def test_alert_recovery_and_gates(self):
        q = self.quality
        q.enabled = True
        for _ in range(29):
            q.observe("simple", 3)
        self.assertNotIn("simple", q.alerts)
        q.observe("simple", 3)
        self.assertFalse(q.raised("simple"))
        q.validated = True
        self.assertTrue(q.raised("simple"))
        q.benchmark = True
        self.assertFalse(q.raised("simple"))
        q.benchmark = False
        for _ in range(29):
            q.observe("simple", 4)
        self.assertTrue(q.raised("simple"))
        q.observe("simple", 4)
        self.assertFalse(q.raised("simple"))

    async def test_low_budget_quality_alert(self):
        q = self.quality
        q.enabled = q.validated = True
        q.alerts.add("simple")
        budget = self.config.reserve_cost("cheap", req())
        router = Router(self.ledger, quality=q)
        response = await router.route(req(max_cost_usd=str(budget)), use_cache=False)
        self.assertEqual(response["model"], self.config.model("cheap")["id"])
        self.assertLessEqual(Decimal(str(response["cost"])), budget)

    async def test_budget_exhausted_after_failure(self):
        p = MockProvider({self.config.model("cheap")["id"]: [ProviderError(500)]})
        budget = self.config.reserve_cost("cheap", req())
        with self.assertRaises(RouterError) as e:
            await Router(self.ledger, provider=p).route(req(max_cost_usd=str(budget)))
        self.assertEqual(e.exception.status, 402)
        self.assertEqual(len(p.calls), 1)

    async def test_all_cooldown(self):
        store = MemoryHealthStore()
        health = Health(store, slo_ms=1)
        for model in self.config.models.values():
            for _ in range(20):
                health.observe(model["id"], health.acquire(model["id"]), True, 2)
        p = MockProvider()
        with self.assertRaises(RouterError) as e:
            await Router(self.ledger, provider=p, health=health).route(req())
        self.assertEqual(e.exception.status, 503)
        self.assertEqual(p.calls, [])

    async def test_renewal_failure_abandons(self):
        clock = Clock()
        renewed = asyncio.Event()
        class RenewalStore(MemoryHealthStore):
            def renew(self, model, token):
                result = super().renew(model, token)
                renewed.set()
                return result
        store = RenewalStore(clock)
        health = Health(store, slo_ms=100, renewal_seconds=.001)
        for _ in range(20):
            health.observe("m", health.acquire("m"), True, 101)
        clock.now = 61
        permit = health.acquire("m")
        store.renew_ok = False
        # Completion follows a renewal attempt, not a race between 1 ms/5 ms
        # timers which can become ready together on coarse Windows clocks.
        await asyncio.wait_for(health.run_probe("m", permit, renewed.wait()), timeout=1)
        self.assertTrue(renewed.is_set())
        self.assertFalse(permit.valid)
        self.assertFalse(health.observe("m", permit, True, 1))
        self.assertTrue(store.states["m"]["opened"])

    async def test_injected_deadline_includes_cache(self):
        clock = Clock()
        router = Router(self.ledger, clock=clock)
        def slow_cache(key):
            clock.now += 20
        router.cache.get = slow_cache
        with self.assertRaises(RouterError) as e:
            await router.route(req())
        self.assertEqual(e.exception.status, 504)
        self.assertEqual(router.provider.calls, [])


class GateTests(unittest.TestCase):
    def pairs(self):
        return [dict(human_a=3, human_b=1 + i % 5, judge_a=3, judge_b=1 + i % 5,
                     tier=("simple", "medium", "complex")[i % 3],
                     task_type=("general", "reasoning", "code", "maths")[i % 4]) for i in range(120)]

    def test_validation_pass(self):
        result = validate_calibration(self.pairs(), resamples=300)
        self.assertTrue(result["validated"])

    def test_bias_and_breakdown_gate(self):
        pairs = self.pairs()
        for p in pairs:
            if p["task_type"] == "code":
                p["judge_a"] = 1
        self.assertFalse(validate_calibration(pairs, resamples=100)["validated"])
        self.assertFalse(validate_calibration([])["validated"])
        pairs[0]["human_a"] = 8
        self.assertFalse(validate_calibration(pairs)["validated"])

    def test_degenerate_correlation_fails(self):
        pairs = self.pairs()
        for p in pairs:
            p["human_b"] = p["judge_b"] = 3
        self.assertFalse(validate_calibration(pairs, resamples=100)["validated"])
