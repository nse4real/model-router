from decimal import Decimal
from pathlib import Path
import tempfile
import unittest
from api.contracts import RouterError
from benchmark.recover import recover
from benchmark.results import write_results
from fallback.providers import MockProvider, Output
from monitoring.ledger import SQLiteLedger
from monitoring.security import redact
from routing.config import Config
from routing.service import Router
from tests.test_stage2 import req


class RegressionTests(unittest.IsolatedAsyncioTestCase):
    async def test_no_empty_success(self):
        cfg = Config()
        provider = MockProvider({cfg.model("cheap")["id"]:[Output("",0,0)]})
        result = await Router(SQLiteLedger(),provider=provider).route(req())
        self.assertTrue(result["content"])
        self.assertEqual(result["fallback_count"],1)

    async def test_price_error_status(self):
        cfg = Config()
        del cfg.models["cheap"]["tiers"][0]["input_per_million"]
        router = Router(SQLiteLedger(),config=cfg)
        request = req()
        with self.assertRaises(RouterError) as exc:
            await router.route(request)
        self.assertEqual(exc.exception.status,422)
        self.assertEqual(exc.exception.request_id,request.request_id)
        self.assertEqual(router.provider.calls,[])

    async def test_crash_artifact_recovery_does_not_replay(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp,"run.json")
            ledger = SQLiteLedger(path.with_suffix(".sqlite"),clock=lambda:0)
            ledger.begin("r","run")
            ledger.reserve("a","r","m","mock","inference","v1",Decimal(".1"))
            ledger.dispatch("a")
            ledger.finish("r",outcome="pending")
            write_results(path,ledger,"run",{"storage":"test-double"},[
                dict(request_id="r",condition="A",prompt_id="fixture",completed=False,cost=".1")])
            ledger.close()
            result = recover(path)
            self.assertTrue(result["final"])
            self.assertEqual(Decimal(result["totals"]["total_cost"]),Decimal(".1"))
            ledger = SQLiteLedger(path.with_suffix(".sqlite"))
            self.assertEqual(len(ledger.attempts()),1)
            self.assertEqual(ledger.attempts()[0]["state"],"unknown")
            ledger.close()

    async def test_redaction_json_error_string(self):
        value = redact('{"api_key": "private", "Authorization": "Bearer tokenvalue"}')
        self.assertNotIn("private",value)
        self.assertNotIn("tokenvalue",value)
        self.assertNotIn("sk-abc",redact("sk-abc"))

    async def test_global_cap_precedes_request_budget(self):
        ledger = SQLiteLedger(cap="0")
        router = Router(ledger)
        with self.assertRaises(RouterError) as exc:
            await router.route(req(max_cost_usd=0))
        self.assertEqual(exc.exception.status,503)
        self.assertEqual(router.provider.calls,[])

    async def test_redis_health_outage_no_unlocked_probe(self):
        from routing.latency import Health
        class Down:
            def acquire(self, model):
                raise ConnectionError()
        health = Health(Down())
        router = Router(SQLiteLedger(),health=health)
        router.cache.available = False
        router.rate.available = False
        self.assertTrue((await router.route(req()))["content"])
        health.known_open.update(m["id"] for m in router.config.models.values())
        with self.assertRaises(RouterError) as exc:
            await router.route(req("different"))
        self.assertEqual(exc.exception.status,503)

    async def test_quality_threshold_exactly_three_point_five(self):
        from monitoring.quality import Quality
        q = Quality(SQLiteLedger(),Config(),MockProvider(),validated=True,enabled=True)
        for value in [3]*15+[4]*15:
            q.observe("simple",value)
        self.assertFalse(q.raised("simple"))
        q.windows["simple"].clear()
        for value in [3]*30:
            q.observe("simple",value)
        self.assertTrue(q.raised("simple"))
        for value in [3]*15+[4]*15:
            q.observe("simple",value)
        self.assertFalse(q.raised("simple"))

    async def test_injected_deadline_mid_chain(self):
        from tests.test_stage3 import Clock
        from fallback.providers import ProviderError
        clock = Clock()
        class SlowFail(MockProvider):
            async def complete(self, model, request, timeout):
                self.calls.append(model["id"])
                # Advance only the injected clock. Keep real asyncio timeouts
                # above Windows' timer resolution and cache thread startup.
                clock.now += .6
                raise ProviderError(500)
        provider = SlowFail()
        with self.assertRaises(RouterError) as exc:
            await Router(SQLiteLedger(),provider=provider,clock=clock).route(req(deadline_ms=1000))
        self.assertEqual(exc.exception.status,504)
        self.assertEqual(len(provider.calls),2)
        self.assertAlmostEqual(clock.now, 1.2)
