"""Offline provider fixtures and optional local infrastructure SLO checks."""
import asyncio
from contextlib import closing
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from api.contracts import Request, RouterError
from benchmark.dataset import split
from benchmark.judge import judge
from benchmark.results import analyse, write_results
from benchmark.run import run_conditions
from fallback.providers import MockJudgeProvider, MockProvider, Output, ProviderError
from monitoring.ledger import PostgresLedger, SQLiteLedger, StorageUnavailable
from monitoring.quality import Quality
from routing.admission import Admission, shared_admission
from routing.config import Config
from routing.latency import Health, MemoryHealthStore, RedisHealthStore
from routing.service import Router
from tests.test_services import isolated_postgres


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class TimedProvider(MockProvider):
    def __init__(self, clock, seconds=5, error=None):
        super().__init__()
        self.clock, self.seconds, self.error = clock, seconds, error

    async def complete(self, model, request, timeout):
        self.calls.append(model["id"])
        self.clock.now += self.seconds
        if self.error:
            raise self.error
        return Output("fixture", 1, 1, resolved_model=model["id"])


def request():
    return Request.parse(dict(messages=[dict(role="user", content="Hello")]))


async def assert_standalone_judge_isolation(case, live, storage):
    """Exercise the real judge entry point using only timed mock providers."""
    prompts, manifest = split("dev")
    models = [m["id"] for m in Config().models.values()]
    scopes = [live]
    qualities = []
    clock = Clock()

    class TimedJudge(TimedProvider):
        async def complete(self, model, request, timeout):
            result = await super().complete(model, request, timeout)
            result.content = '{"score":3}'
            return result

    provider = TimedJudge(clock, .25)

    def quality(*args, **kwargs):
        q = Quality(*args, **kwargs, clock=clock)
        qualities.append(q)
        scopes.append(q.admission.health)
        return q

    def breach(health):
        for model in models:
            for _ in range(20):
                permit = health.acquire(model)
                if permit is None:
                    break
                health.observe(model, permit, True, 20001)
            case.assertIsNone(health.acquire(model))

    def snapshot(health):
        if isinstance(health.store, RedisHealthStore):
            return {key: health.store.client.get(key) for model in models for key in health.store.keys(model)}
        return {model: (list(state["samples"]), state["opened"], state["until"])
                for model, state in health.store.states.items()}

    try:
        breach(live)
        before_live = snapshot(live)
        with TemporaryDirectory() as directory, closing(SQLiteLedger()) as ledger:
            router = Router(ledger)
            run_id, records = await run_conditions(router, prompts[:1])
            path = Path(directory, "judge.json")
            for attempt in ledger.attempts():
                ledger.record_latency(attempt["attempt_id"], 125)
            write_results(path, ledger, run_id, dict(storage=storage, seed=13, config=Config().data,
                dataset_checksum=manifest["sha256"], analysis=analyse(records, 1, resamples=10)), records)
            settle = ledger.settle

            def delayed_settle(*args, **kwargs):
                clock.now += 1  # Storage is excluded from full-completion latency.
                return settle(*args, **kwargs)

            with patch("benchmark.judge.SQLiteLedger", return_value=ledger), patch(
                    "benchmark.judge.PostgresLedger", return_value=ledger), patch(
                    "benchmark.judge.shared_admission", return_value=Admission(live)) as shared, patch(
                    "benchmark.judge.Health", return_value=live), patch(
                    "benchmark.judge.Quality", side_effect=quality), patch(
                    "benchmark.judge.MockJudgeProvider", return_value=provider), patch.object(
                    ledger, "settle", side_effect=delayed_settle):
                first = await judge(path)
                case.assertTrue(first["final"])
                case.assertEqual(len(provider.calls), 4)
                case.assertTrue(all(r["quality"] == 3 for r in first["records"]))
                previous = qualities[0].admission.health
                case.assertIsNot(previous, live)
                breach(previous)
                before_previous = snapshot(previous)
                second = await judge(path)
                case.assertEqual(first["totals"], second["totals"])
                case.assertEqual(len(provider.calls), 4)  # Idempotency remains intact.
                case.assertEqual(len(ledger.attempts()), 7)
                current = qualities[1].admission.health
                case.assertIsNot(current, previous)
                case.assertIsNot(current, live)
                for model in models:
                    case.assertFalse(current.acquire(model).probe)
                if storage == "postgres":
                    case.assertEqual(shared.call_count, 2)
                    shared.assert_called_with(20000)
                    case.assertNotEqual(current.store.namespace, previous.store.namespace)
                for q in qualities:
                    case.assertEqual(q.admission.health.slo, 20000)
                case.assertEqual(snapshot(live), before_live)
                case.assertEqual(snapshot(previous), before_previous)
                for record in second["records"]:
                    attempts = ledger.attempts(record["request_id"])
                    expected = {a["attempt_id"]: float(a["latency_ms"]) for a in attempts}
                    case.assertEqual({a["attempt_id"]: a["latency_ms"] for a in record["attempt_latencies"]}, expected)
                    case.assertEqual({a["attempt_id"]: a["latency_ms"] for a in record["attempts"]}, expected)
                    for attempt in attempts:
                        case.assertEqual(attempt["latency_ms"], 250 if attempt["purpose"] == "evaluation" else 125)
    finally:
        if isinstance(live.store, RedisHealthStore):
            keys = [key for health in scopes for model in models for key in health.store.keys(model)]
            live.store.client.delete(*keys)


class BenchmarkSLOTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_full_completion_slo_for_live_benchmark_and_judge(self):
        config = Config()
        self.assertEqual(config.slo_ms, 20000)
        self.assertEqual(Health().slo, 20000)
        self.assertNotIn("benchmark_slo_ms", config.data)
        fallback = Config(data={k: v for k, v in config.data.items() if k != "slo_ms"})
        self.assertEqual(fallback.slo_ms, 20000)
        self.assertEqual(shared_admission().health.slo, 20000)
        for benchmark in (False, True):
            with self.subTest(benchmark=benchmark), closing(SQLiteLedger()) as ledger:
                clock = Clock()
                provider = TimedProvider(clock)
                router = Router(ledger, config=config, benchmark=benchmark,
                                clock=clock, provider=provider)
                quality = Quality(ledger, config, MockJudgeProvider(), benchmark=benchmark)
                self.assertEqual(router.health.slo, 20000)
                self.assertEqual(quality.admission.health.slo, 20000)
                for _ in range(20):
                    await router.route(request(), use_cache=False, force_model="mid")
                model = config.model("mid")["id"]
                self.assertIsNotNone(router.health.acquire(model))
                self.assertTrue(all(a["latency_ms"] == 5000 for a in ledger.attempts()))
        # Changing the one config value applies equally in every execution mode.
        config.data["slo_ms"] = 1234
        with closing(SQLiteLedger()) as ledger:
            self.assertEqual(Router(ledger, config=config).health.slo, 1234)
            self.assertEqual(Router(ledger, config=config, benchmark=True).health.slo, 1234)
            self.assertEqual(Quality(ledger, config, MockJudgeProvider()).admission.health.slo, 1234)

    async def test_full_completion_slo_boundary_in_both_modes(self):
        for benchmark in (False, True):
            for milliseconds in (20000, 20001):
                with self.subTest(benchmark=benchmark, milliseconds=milliseconds), closing(SQLiteLedger()) as ledger:
                    clock = Clock()
                    router = Router(ledger, benchmark=benchmark, clock=clock,
                                    provider=TimedProvider(clock, milliseconds / 1000))
                    for _ in range(20):
                        req = Request.parse(dict(messages=[dict(role="user", content="hello")], deadline_ms=60000))
                        await router.route(req, use_cache=False, force_model="mid")
                    model = router.config.model("mid")["id"]
                    self.assertEqual(router.health.acquire(model) is None, milliseconds > 20000)
                    for attempt in ledger.attempts():
                        self.assertAlmostEqual(attempt["latency_ms"], milliseconds)

    async def test_api_keeps_cooldown_between_requests(self):
        import httpx
        from api.app import create_app
        with closing(SQLiteLedger()) as ledger:
            router = Router(ledger)
            live = router.health
            for model in router.config.models.values():
                for _ in range(20):
                    live.observe(model["id"], live.acquire(model["id"]), True, 20001)
            with patch.object(live, "for_benchmark", side_effect=AssertionError("API reset health")):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(router)),
                                             base_url="http://test") as client:
                    for _ in range(2):
                        response = await client.post("/v1/route", json={"messages": [{"role": "user", "content": "hello"}]})
                        self.assertEqual(response.status_code, 503)
                        self.assertIn("request_id", response.json())
            self.assertIs(router.health, live)
            self.assertEqual(router.health.slo, 20000)
            self.assertEqual(router.provider.calls, [])
            self.assertEqual(ledger.attempts(), [])
            for model in router.config.models.values():
                self.assertIsNone(live.acquire(model["id"]))

    async def test_standalone_judge_memory_isolation_and_full_completion_export(self):
        await assert_standalone_judge_isolation(self, Health(), "test-double")

    async def test_latency_records_success_failure_retry_and_timeout(self):
        for error, count in ((None, 1), (ProviderError(400), 1),
                             (ProviderError(500), 2), (ConnectionError(), 2),
                             (TimeoutError(), 2)):
            with self.subTest(error=type(error).__name__), closing(SQLiteLedger()) as ledger:
                clock = Clock()
                router = Router(ledger, provider=TimedProvider(clock, .25, error), clock=clock)
                if error:
                    with self.assertRaises(RouterError):
                        await router.route(request(), use_cache=False, force_model="mid")
                else:
                    await router.route(request(), use_cache=False, force_model="mid")
                attempts = ledger.attempts()
                self.assertEqual(len(attempts), count)
                self.assertTrue(all(a["latency_ms"] == 250 for a in attempts))
                if isinstance(error, ProviderError) and error.status == 400:
                    self.assertEqual(attempts[0]["cost_status"], "rejected")
                    self.assertEqual(Decimal(str(attempts[0]["cost"])), 0)

    async def test_latency_excludes_settlement_and_write_outage_keeps_answer(self):
        with closing(SQLiteLedger()) as ledger:
            clock = Clock()
            router = Router(ledger, provider=TimedProvider(clock, .25), clock=clock)
            settle = ledger.settle
            def delayed_settle(*args, **kwargs):
                clock.now += 1
                return settle(*args, **kwargs)
            with patch.object(ledger, "settle", side_effect=delayed_settle):
                await router.route(request(), use_cache=False)
            self.assertEqual(ledger.attempts()[0]["latency_ms"], 250)
            with patch.object(ledger, "record_latency", side_effect=StorageUnavailable()):
                response = await router.route(request(), use_cache=False)
            self.assertEqual(response["content"], "fixture")

    async def test_runner_resets_previous_run_health_and_exports_latency(self):
        with closing(SQLiteLedger()) as ledger, TemporaryDirectory() as directory:
            clock = Clock()
            router = Router(ledger, provider=TimedProvider(clock, .25), clock=clock)
            live = router.health
            for model in router.config.models.values():
                for _ in range(20):
                    live.observe(model["id"], live.acquire(model["id"]), True, 20001)
            _, first = await run_conditions(router, [dict(id="fixture", prompt="hello")], run_id="first")
            previous = router.health
            for model in router.config.models.values():
                for _ in range(20):
                    permit = previous.acquire(model["id"])
                    if permit is None:
                        break
                    previous.observe(model["id"], permit, True, 20001)
            run_id, records = await run_conditions(router, [dict(id="fixture", prompt="hello")], run_id="second")
            self.assertTrue(all(r["status"] == 200 for r in first + records))
            self.assertIsNot(previous, router.health)
            self.assertEqual(router.health.slo, 20000)
            for model in router.config.models.values():
                self.assertIsNone(live.acquire(model["id"]))
                self.assertIsNone(previous.acquire(model["id"]))
            path = Path(directory, "result.json")
            write_results(path, ledger, run_id, {}, records)
            result = json.loads(path.read_text())
            for row in result["records"]:
                measured = row["attempt_latencies"]
                attempts = ledger.attempts(row["request_id"])
                self.assertEqual({a["attempt_id"] for a in measured}, {a["attempt_id"] for a in attempts})
                self.assertTrue(all(a["latency_ms"] == 250 for a in measured))
                if row["condition"] == "C-warm":
                    self.assertEqual(measured, [])
            _, _, durable = ledger.load_run(run_id)
            self.assertTrue(all("attempt_latencies" in r for r in durable))

    async def test_cancellation_at_request_timeout_records_attempt_latency(self):
        with closing(SQLiteLedger()) as ledger:
            config = Config()
            provider = MockProvider({config.model("mid")["id"]: [1.]})
            router = Router(ledger, config=config, provider=provider)
            req = Request.parse(dict(messages=[dict(role="user", content="hello")], deadline_ms=100))
            with self.assertRaises(RouterError) as error:
                await router.route(req, use_cache=False, force_model="mid")
            self.assertEqual(error.exception.status, 504)
            self.assertEqual(len(ledger.attempts()), 1)
            self.assertGreater(ledger.attempts()[0]["latency_ms"], 0)

    async def test_failed_run_exports_each_fallback_latency(self):
        with closing(SQLiteLedger()) as ledger, TemporaryDirectory() as directory:
            clock = Clock()
            router = Router(ledger, provider=TimedProvider(clock, .25, ProviderError(400)), clock=clock)
            run, records = await run_conditions(router, [dict(id="fixture", prompt="hello")])
            path = Path(directory, "failed.json")
            write_results(path, ledger, run, {}, records)
            result = json.loads(path.read_text())
            self.assertTrue(all(r["status"] == 503 for r in result["records"]))
            for row in result["records"]:
                self.assertEqual(len(row["attempt_latencies"]), len(row["attempts"]))
                self.assertTrue(all(a["latency_ms"] == 250 for a in row["attempt_latencies"]))

    async def test_concurrent_dispatches_keep_their_own_latency(self):
        class YieldingProvider(TimedProvider):
            async def complete(self, model, request, timeout):
                await asyncio.sleep(0)
                return await super().complete(model, request, timeout)
        with closing(SQLiteLedger()) as ledger:
            clocks = [Clock(), Clock()]
            requests = [request(), request()]
            routers = [Router(ledger, clock=clock, provider=YieldingProvider(clock, index + 1))
                       for index, clock in enumerate(clocks)]
            await asyncio.gather(*(router.route(req, use_cache=False)
                                   for router, req in zip(routers, requests)))
            for index, req in enumerate(requests):
                self.assertEqual(ledger.attempts(req.request_id)[0]["latency_ms"], (index + 1) * 1000)

    async def test_attached_judge_attempts_have_latency_and_isolated_health(self):
        with closing(SQLiteLedger()) as ledger:
            config = Config()
            quality = Quality(ledger, config, MockJudgeProvider(), sample_rate=1)
            router = Router(ledger, config=config, quality=quality)
            live = router.health
            _, records = await run_conditions(router, [dict(id="fixture", prompt="hello")])
            self.assertIs(quality.admission.health, router.health)
            self.assertIsNot(quality.admission.health, live)
            self.assertEqual(quality.admission.health.slo, 20000)
            attempts = ledger.attempts()
            self.assertEqual(sum(a["purpose"] == "evaluation" for a in attempts), 4)
            self.assertTrue(all(a["latency_ms"] is not None and a["latency_ms"] >= 0 for a in attempts))
            self.assertEqual(sum(len(r["attempt_latencies"]) for r in records), len(attempts))


class HealthExpiryTests(unittest.TestCase):
    def test_memory_ttl_expires_and_refreshes_on_write(self):
        clock = Clock()
        store = MemoryHealthStore(clock)
        health = Health(store)
        for _ in range(20):
            health.observe("model", health.acquire("model"), True, 20001)
        self.assertIsNone(health.acquire("model"))
        self.assertEqual(store.states["model"]["expires"], 3600)
        clock.now = 61
        probe = health.acquire("model")
        self.assertTrue(health.observe("model", probe, False, 5000))
        self.assertEqual(store.states["model"]["expires"], 3661)
        clock.now = 3661
        self.assertFalse(health.acquire("model").probe)
        self.assertEqual(len(store.states["model"]["samples"]), 0)


class LatencyMigrationTests(unittest.TestCase):
    def test_existing_sqlite_rows_migrate_idempotently_without_inventing_latency(self):
        with TemporaryDirectory() as directory:
            path = Path(directory, "old.sqlite")
            schema = Path("monitoring/schema.sql").read_text().replace("latency_ms NUMERIC, ", "")
            with closing(sqlite3.connect(path)) as db:
                db.executescript(schema)
                db.execute("INSERT INTO requests(request_id,created_at) VALUES('r',0)")
                db.execute("INSERT INTO attempts(attempt_id,parent_request_id,model,provider,purpose,"
                           "price_version,reserved,cost,state,cost_status,created_at) "
                           "VALUES('a','r','fixture','mock','inference','v',0,0,'settled','measured',0)")
                db.commit()
            for index in range(2):
                with closing(SQLiteLedger(path)) as ledger:
                    self.assertEqual(ledger.attempts()[0]["latency_ms"], None if index == 0 else 123.5)
                    ledger.record_latency("a", 123.5)
                    self.assertEqual(ledger.attempts()[0]["state"], "settled")

    @unittest.skipUnless(os.environ.get("ROUTER_TEST_POSTGRES_DSN"), "Local Postgres DSN not supplied")
    def test_postgres_existing_schema_migration_and_numeric_latency(self):
        import psycopg
        with isolated_postgres() as dsn:
            schema = Path("monitoring/schema.sql").read_text().replace("latency_ms NUMERIC, ", "")
            with psycopg.connect(dsn) as conn:
                conn.execute(schema)
            ledger = PostgresLedger(dsn)
            ledger.begin("r")
            ledger.reserve("a", "r", "fixture", "mock", "inference", "v", ".1")
            ledger.dispatch("a")
            self.assertIsNone(ledger.attempts()[0]["latency_ms"])
            ledger.record_latency("a", 125.5)
            self.assertEqual(PostgresLedger(dsn).attempts()[0]["latency_ms"], Decimal("125.5"))


@unittest.skipUnless(os.environ.get("ROUTER_TEST_REDIS_URL"), "Local Redis URL not supplied")
class RedisBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_standalone_judge_redis_isolation_and_full_completion_export(self):
        import redis
        client = redis.Redis.from_url(os.environ["ROUTER_TEST_REDIS_URL"], decode_responses=True)
        live = Health(RedisHealthStore(client, "fixture-live:" + uuid4().hex))
        await assert_standalone_judge_isolation(self, live, "postgres")

    async def test_run_isolation_health_ttl_refresh_and_probe_namespace(self):
        import redis
        client = redis.Redis.from_url(os.environ["ROUTER_TEST_REDIS_URL"], decode_responses=True)
        token = uuid4().hex
        model = "fixture-" + token
        live = Health(RedisHealthStore(client))
        first = live.for_benchmark(token + "-first")
        second = first.for_benchmark(token + "-second")
        try:
            for health, latency in ((live, 20001), (first, 20001)):
                for _ in range(20):
                    health.observe(model, health.acquire(model), True, latency)
                self.assertIsNone(health.acquire(model))
                self.assertGreater(client.ttl(health.store.keys(model)[0]), 0)
                self.assertLessEqual(client.ttl(health.store.keys(model)[0]), 3600)
            before = {key: client.get(key) for health in (live, first) for key in health.store.keys(model)}
            self.assertFalse(second.acquire(model).probe)
            second.observe(model, second.acquire(model), True, 5000)
            self.assertEqual(before, {key: client.get(key) for key in before})
            key = second.store.keys(model)[0]
            client.expire(key, 1)
            second.observe(model, second.acquire(model), True, 5000)
            self.assertGreater(client.ttl(key), 3500)
            state = json.loads(client.get(key))
            state.update(opened=True, until=0)
            client.set(key, json.dumps(state), ex=3600)
            probe = second.acquire(model)
            self.assertTrue(probe.probe)
            self.assertGreater(client.ttl(second.store.keys(model)[1]), 0)
            self.assertTrue(second.observe(model, probe, True, 5000))
            self.assertEqual(before, {key: client.get(key) for key in before})
        finally:
            keys = [key for health in (live, first, second) for key in health.store.keys(model)]
            client.delete(*keys)

    async def test_runner_ignores_prior_redis_run_and_accesses_only_new_health_namespace(self):
        import redis
        client = redis.Redis.from_url(os.environ["ROUTER_TEST_REDIS_URL"], decode_responses=True)
        old_run, new_run = uuid4().hex, uuid4().hex
        config = Config()
        prior = Health(RedisHealthStore(client, "benchmark:" + old_run))
        models = [m["id"] for m in config.models.values()]
        fresh = prior.for_benchmark(new_run)
        try:
            for model in models:
                for _ in range(20):
                    prior.observe(model, prior.acquire(model), True, 20001)
                self.assertIsNone(prior.acquire(model))
            before = {key: client.get(key) for model in models for key in prior.store.keys(model)}
            with closing(SQLiteLedger()) as ledger:
                router = Router(ledger, config=config, health=prior)
                with patch.object(client, "eval", wraps=client.eval) as evaluate:
                    _, records = await run_conditions(router, [dict(id="fixture", prompt="hello")], run_id=new_run)
                self.assertTrue(all(r["status"] == 200 for r in records))
                for call in evaluate.call_args_list:
                    count = call.args[1]
                    for key in call.args[2:2 + count]:
                        self.assertIn(":benchmark:" + new_run + ":", key)
                self.assertTrue(evaluate.called)
            self.assertEqual(before, {key: client.get(key) for key in before})
        finally:
            keys = [key for health in (prior, fresh) for model in models for key in health.store.keys(model)]
            client.delete(*keys)
