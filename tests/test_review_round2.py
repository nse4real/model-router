"""Round-two review regressions. Every provider is a local fixture or stub."""
import asyncio
from contextlib import contextmanager
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.contracts import RouterError
from benchmark.dataset import split
from benchmark.recover import recover
from benchmark.run import run_conditions
from cache.exact import MemoryCache, MemoryRate, key
from fallback.providers import MockJudgeProvider, MockProvider, Output
from monitoring.ledger import SQLiteLedger, StorageUnavailable
from monitoring.metrics import snapshot
from monitoring.quality import Quality
from monitoring.security import redact
from routing.config import Config
from routing.latency import Health, MemoryHealthStore
from routing.service import Router
from tests.test_stage2 import req
from tests.test_stage3 import Clock


class ReviewSecurityTests(unittest.TestCase):
    def test_entire_authorization_and_provider_secrets(self):
        for scheme in ('Basic dXNlcjpzZWNyZXQ=', 'Bearer abc.def', 'Custom first second'):
            value = redact('Authorization: ' + scheme + '\r\nNext: safe')
            self.assertEqual(value, 'Authorization: [REDACTED]\r\nNext: safe')
        value = redact('provider sk-abc123 sk-ant-api03-secret bearer opaque-token')
        for secret in ('sk-abc123', 'sk-ant-api03-secret', 'opaque-token'):
            self.assertNotIn(secret, value)

    def test_provider_parameters_and_version_change_cache_key(self):
        request = req()
        base = key(request, 'm', 'p', {'seed': 1, 'stop': ['END'], 'top_p': .9}, 'v1')
        for parameters, version in (({'seed': 2, 'stop': ['END'], 'top_p': .9}, 'v1'),
                                    ({'seed': 1, 'stop': ['STOP'], 'top_p': .9}, 'v1'),
                                    ({'seed': 1, 'stop': ['END'], 'top_p': .8}, 'v1'),
                                    ({'seed': 1, 'stop': ['END'], 'top_p': .9}, 'v2')):
            self.assertNotEqual(base, key(request, 'm', 'p', parameters, version))
        self.assertEqual(base, key(request, 'm', 'p', {'top_p': .9, 'stop': ['END'], 'seed': 1}, 'v1'))


class ReviewRoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_cache_write_and_quality_submit_leave_response_path(self):
        entered, release = threading.Event(), threading.Event()
        class SlowCache(MemoryCache):
            def put(self, name, value):
                entered.set()
                release.wait(2)
                super().put(name, value)
        class SlowQuality:
            def raised(self, tier):
                return False
            def submit(self, *args):
                entered.set()
                release.wait(2)
        router = Router(SQLiteLedger(), cache=SlowCache(), quality=SlowQuality(),
                        background_size=1)
        try:
            started = asyncio.get_running_loop().time()
            result = await asyncio.wait_for(router.route(req(deadline_ms=100)), .5)
            self.assertLess(asyncio.get_running_loop().time() - started, .5)
            self.assertTrue(result['content'])
            await asyncio.to_thread(entered.wait, .5)
            self.assertTrue(entered.is_set())
            # One queued job plus one active job is the fixed memory bound.
            for _ in range(10):
                router.enqueue_background(lambda: asyncio.sleep(0))
            self.assertLessEqual(router.background.qsize(), 1)
        finally:
            release.set()
            await router.drain_background()

    async def test_deadline_after_settlement_and_finish_keeps_measured_spend(self):
        for slow_operation in ('provider', 'settle', 'finish'):
            with self.subTest(slow_operation=slow_operation):
                clock = Clock()
                ledger = SQLiteLedger()
                class Timed(MockProvider):
                    async def complete(self, model, request, timeout):
                        if slow_operation == 'provider':
                            clock.now += .006
                        return Output('ok', 2, 3)
                if slow_operation != 'provider':
                    original = getattr(ledger, slow_operation)
                    def slow(*args, **kwargs):
                        result = original(*args, **kwargs)
                        clock.now += .006
                        return result
                    setattr(ledger, slow_operation, slow)
                router = Router(ledger, provider=Timed(), clock=clock)
                with self.assertRaises(RouterError) as caught:
                    await router.route(req(deadline_ms=5), use_cache=False)
                self.assertEqual(caught.exception.status, 504)
                attempt = ledger.attempts()[0]
                self.assertEqual((attempt['state'], attempt['input_tokens'], attempt['output_tokens']),
                                 ('settled', 2, 3))
                self.assertGreater(Decimal(str(attempt['cost'])), 0)
                self.assertEqual(ledger.requests()[0]['outcome'], '504')

    async def test_lost_probe_settles_measured_usage_without_health_change(self):
        clock = Clock()
        store = MemoryHealthStore(clock)
        health = Health(store, slo_ms=1)
        cfg = Config()
        model = cfg.model('cheap')['id']
        for _ in range(20):
            health.observe(model, health.acquire(model), True, 2)
        clock.now = 61
        class Lost(MockProvider):
            async def complete(self, model, request, timeout):
                store.locks.clear()
                return Output('paid answer', 7, 3)
        ledger = SQLiteLedger()
        router = Router(ledger, provider=Lost(), health=health)
        with self.assertRaises(RouterError) as caught:
            await router.route(req(), force_model='cheap', use_cache=False)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(len(ledger.attempts()), 1)
        attempt = ledger.attempts()[0]
        self.assertEqual((attempt['input_tokens'], attempt['output_tokens']), (7, 3))
        self.assertEqual(Decimal(str(attempt['cost'])), cfg.cost('cheap', 7, 3))
        self.assertTrue(store.states[model]['opened'])
        self.assertEqual(store.states[model]['until'], 60)

    async def test_router_key_uses_model_generation_parameters(self):
        cfg = Config()
        cfg.models['cheap']['litellm'] = {'seed': 1}
        router = Router(SQLiteLedger(), config=cfg)
        await router.route(req())
        await router.drain_background()
        cfg.models['cheap']['litellm']['seed'] = 2
        response = await router.route(req())
        self.assertFalse(response['cache_hit'])
        await router.drain_background()


class ReviewJudgeTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_between_claim_and_reserve_cannot_enable_paid_call_after_barrier(self):
        clock = [0]
        ledger = SQLiteLedger(clock=lambda: clock[0])
        ledger.begin('r', 'run')
        provider = MockJudgeProvider()
        quality = Quality(ledger, Config(), provider, sample_rate=1)
        quality.submit('r', 'simple', req(), Output('ok', 1, 1))
        job = quality.queue.get_nowait()
        # This hook reproduced the old two-transaction race exactly.
        original = ledger.claim_job
        def recover_after_claim(attempt):
            claimed = original(attempt)
            clock[0] = 61
            ledger.recover()
            self.assertTrue(ledger.barrier('run'))
            return claimed
        ledger.claim_job = recover_after_claim
        await quality.process(job)
        barrier_spend = sum(Decimal(str(a['cost'])) for a in ledger.attempts())
        self.assertTrue(ledger.barrier('run'))
        await quality.process(job)
        self.assertEqual(sum(Decimal(str(a['cost'])) for a in ledger.attempts()), barrier_spend)
        # Atomic claim/reserve never invokes the separated claim hook.
        self.assertEqual(clock[0], 0)
        self.assertEqual(len(provider.calls), 1)

    async def test_terminal_job_cannot_reserve_after_barrier(self):
        ledger = SQLiteLedger(clock=lambda: 0)
        ledger.begin('r', 'run')
        ledger.job('j', 'r')
        ledger.clock = lambda: 61
        ledger.recover()
        self.assertTrue(ledger.barrier('run'))
        self.assertFalse(ledger.claim_and_reserve('j', 'r', 'm', 'mock', 'v', Decimal('.1')))
        self.assertEqual(ledger.attempts(), [])

    async def test_judge_shares_inference_rpm_and_cooldown(self):
        for kind in ('rpm', 'cooldown'):
            with self.subTest(kind=kind):
                cfg = Config()
                cfg.models['mid']['rpm'] = 1
                model = cfg.model('mid')['id']
                ledger = SQLiteLedger()
                rate = MemoryRate(clock=lambda: 0)
                health = Health(slo_ms=1)
                provider = MockJudgeProvider()
                quality = Quality(ledger, cfg, provider, sample_rate=1)
                router = Router(ledger, config=cfg, rate=rate, health=health, quality=quality)
                if kind == 'rpm':
                    await router.route(req(), force_model='mid', use_cache=False)
                    await router.drain_background()
                else:
                    ledger.begin('r')
                    for _ in range(20):
                        health.observe(model, health.acquire(model), True, 2)
                    quality.submit('r', 'simple', req(), Output('ok', 1, 1))
                await quality.drain()
                self.assertEqual(provider.calls, [])
                self.assertFalse(any(a['purpose'] == 'evaluation' for a in ledger.attempts()))
                self.assertEqual(ledger.requests()[0]['quality_status'], 'quality_unknown')

    async def test_judge_latency_in_shared_model_window(self):
        ledger = SQLiteLedger()
        ledger.begin('r')
        cfg = Config()
        health = Health()
        quality = Quality(ledger, cfg, MockJudgeProvider(), sample_rate=1, health=health)
        quality.submit('r', 'simple', req(), Output('ok', 1, 1))
        await quality.drain()
        self.assertEqual(len(health.store.states[cfg.model('mid')['id']]['samples']), 1)


class ReviewLedgerTests(unittest.TestCase):
    def test_sqlite_busy_timeout_has_independent_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'ledger.sqlite')
            ledger = SQLiteLedger(path, statement_timeout_ms=30)
            other = sqlite3.connect(path, isolation_level=None)
            other.execute('BEGIN IMMEDIATE')
            try:
                with ledger.deadline(.005, clock=lambda: 0):
                    with self.assertRaises(StorageUnavailable):
                        ledger.begin('r')
                self.assertEqual(ledger.db.execute('PRAGMA busy_timeout').fetchone()[0], 30)
            finally:
                other.rollback()
                other.close()
                ledger.close()

    def test_metrics_snapshot_cannot_mix_concurrent_reservation(self):
        ledger = SQLiteLedger()
        inserted = threading.Event()
        writer = None
        original = ledger.transaction
        @contextmanager
        def transaction(*args, **kwargs):
            nonlocal writer
            with original(*args, **kwargs) as db:
                class BetweenReads:
                    def execute(self, sql, params=()):
                        nonlocal writer
                        result = db.execute(sql, params)
                        if sql == 'SELECT * FROM requests':
                            rows = result.fetchall()
                            def write():
                                ledger.begin('r')
                                ledger.reserve('a', 'r', 'm', 'mock', 'inference', 'v', Decimal('1'))
                                inserted.set()
                            writer = threading.Thread(target=write)
                            writer.start()
                            inserted.wait(.02)
                            return SimpleNamespace(fetchall=lambda: rows)
                        return result
                yield BetweenReads()
        ledger.transaction = transaction
        result = snapshot(ledger)
        writer.join(1)
        self.assertTrue(inserted.is_set())
        self.assertEqual((result['requests'], result['attempts'], result['total_cost']), (0, 0, 0))


class ReviewBenchmarkTests(unittest.IsolatedAsyncioTestCase):
    async def test_crash_first_reservation_recover_from_ledger_without_artifact(self):
        prompts, manifest = split('dev')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'run.json')
            ledger = SQLiteLedger(path.with_suffix('.sqlite'), clock=lambda: 0)
            original = ledger.reserve
            def crash(*args, **kwargs):
                original(*args, **kwargs)
                raise KeyboardInterrupt('simulated crash after reservation')
            ledger.reserve = crash
            router = Router(ledger)
            with self.assertRaises(KeyboardInterrupt):
                await run_conditions(router, prompts[:1], run_id='run',
                                     metadata={'storage': 'test-double', 'seed': 13,
                                               'dataset_checksum': manifest['sha256']})
            self.assertFalse(path.exists())
            ledger.close()
            result = recover(path.with_suffix('.sqlite'))
            self.assertTrue(result['final'])
            self.assertFalse(result['headline_claim_supported'])
            self.assertEqual(result['seed'], 13)
            self.assertEqual(result['dataset_checksum'], manifest['sha256'])
            self.assertEqual(result['config'], router.config.data)
            self.assertEqual(result['price_table_version'], router.config.version)
            self.assertEqual(len(result['records']), 1)
            self.assertFalse(result['records'][0]['completed'])
            self.assertGreater(Decimal(result['totals']['total_cost']), 0)

    async def test_real_guard_and_stubbed_litellm_shared_cap(self):
        from fallback.providers import real_provider
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                real_provider(True)
        with patch.dict('os.environ', {'ROUTER_CONFIRM_REAL_SPEND': 'YES'}, clear=True):
            with self.assertRaises(ValueError):
                real_provider(False)
            provider = real_provider(True)
            calls = []
            async def completion(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='stub'),
                                                                finish_reason='stop')],
                                       usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2))
            stub = SimpleNamespace(acompletion=completion)
            with patch.dict('sys.modules', {'litellm': stub}):
                router = Router(SQLiteLedger(cap='0'), provider=provider)
                with self.assertRaises(RouterError) as caught:
                    await router.route(req(), use_cache=False)
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(calls, [])
                router = Router(SQLiteLedger(), provider=provider)
                prompts, _ = split('dev')
                with self.assertRaises(ValueError):
                    await run_conditions(router, prompts[:1])
                _, records = await run_conditions(router, prompts[:1], real=True)
                self.assertEqual(len(records), 4)
                self.assertTrue(calls)
                self.assertTrue(all(c['num_retries'] == 0 for c in calls))
                self.assertTrue(all(c['timeout'] > 0 for c in calls))
                self.assertTrue(router.ledger.barrier())
                self.assertEqual(len(router.ledger.attempts()), len(calls))


class ReviewClassifierTests(unittest.TestCase):
    def test_original_keyword_rules_for_short_prompts(self):
        from classifier.scorer import score
        for text, expected, value, keywords in (
            ("Prove a lower bound for comparison sorting.", "medium", 2, 1),
            ("Design a protocol that preserves charged spend after a crash.", "simple", 0, 0),
            ("Explain why a checksum detects accidental changes.", "simple", 0, 0),
            ("A runner does 3 sprints of 60 metres, 3 times a week. How many metres a week?", "simple", 0, 0),
            ("Name the closest planet to the Sun.", "simple", 0, 0),
        ):
            with self.subTest(text=text):
                tier, actual_value, features = score(req(text))
                self.assertEqual((tier, actual_value), (expected, value))
                self.assertEqual(features['keywords'], keywords)

    def test_original_frozen_rules_dev_agreement(self):
        from benchmark.diagnostics import diagnostic
        result = diagnostic()
        self.assertEqual(result['split'], 'dev')
        self.assertEqual(result['n'], 43)
        self.assertEqual(result['correct'], 20)
        self.assertAlmostEqual(result['accuracy'], 20 / 43)
        self.assertEqual(result['thresholds'], [2, 5])


class ReviewPostgresContractTests(unittest.TestCase):
    def test_postgres_repeatable_snapshot_and_deadline_timeouts(self):
        from monitoring.ledger import Ledger, PostgresLedger
        commands = []
        committed = {'changed': False}
        class Connection:
            repeatable = False
            visible = None
            closed = False
            info = SimpleNamespace(transaction_status=0)  # idle; ledger returns it to the pool
            def commit(self):
                pass
            def rollback(self):
                pass
            def close(self):
                self.closed = True
            def execute(self, sql, params=()):
                commands.append((sql, params))
                if sql == 'SET TRANSACTION ISOLATION LEVEL REPEATABLE READ':
                    self.repeatable = True
                if sql == 'SELECT * FROM requests':
                    self.visible = committed['changed']
                    # Another connection commits a request + reservation between
                    # the two reads. READ COMMITTED would expose the attempt.
                    committed['changed'] = True
                    return SimpleNamespace(fetchall=lambda: [])
                if sql == 'SELECT * FROM attempts':
                    visible = self.visible if self.repeatable else committed['changed']
                    rows = [{'cost': 1, 'purpose': 'inference', 'state': 'reserved'}] if visible else []
                    return SimpleNamespace(fetchall=lambda: rows)
                return SimpleNamespace(fetchall=lambda: [])
        import queue
        ledger = PostgresLedger.__new__(PostgresLedger)
        Ledger.__init__(ledger)
        ledger._idle = queue.LifoQueue(maxsize=32)
        ledger.dsn, ledger.dict_row, ledger.initialized = 'local-stub', None, True
        ledger.psycopg = SimpleNamespace(connect=lambda *a, **kw: Connection(),
                                           Error=type('StubDatabaseError', (Exception,), {}))
        with ledger.deadline(.005, clock=lambda: 0):
            result = snapshot(ledger)
        self.assertEqual((result['requests'], result['attempts'], result['total_cost']), (0, 0, 0))
        self.assertIn(("SELECT set_config('lock_timeout', %s, true)", ('2000ms',)), commands)
        self.assertIn(("SELECT set_config('statement_timeout', %s, true)", ('2000ms',)), commands)
        self.assertLess(commands.index(('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ', ())),
                        commands.index(('SELECT * FROM requests', ())))


class ReviewAtomicClaimTests(unittest.TestCase):
    def test_recovery_contends_with_claim_and_reservation_as_one_transaction(self):
        ledger = SQLiteLedger(clock=lambda: 0)
        ledger.begin('r', 'run')
        ledger.job('j', 'r')
        entered, release, recovering = threading.Event(), threading.Event(), threading.Event()
        original = ledger._reserve
        def paused(*args):
            entered.set()
            self.assertTrue(release.wait(1))
            original(*args)
        ledger._reserve = paused
        results, failures = [], []
        def claim():
            try:
                results.append(ledger.claim_and_reserve('j', 'r', 'm', 'mock', 'v', Decimal('.1')))
            except BaseException as exc:
                failures.append(exc)
        def recovery():
            recovering.set()
            try:
                ledger.recover()
                results.append(ledger.barrier('run'))
            except BaseException as exc:
                failures.append(exc)
        worker = threading.Thread(target=claim)
        worker.start()
        self.assertTrue(entered.wait(1))
        # Queue time is old, but the new reservation uses its actual claim time.
        ledger.clock = lambda: 61
        recoverer = threading.Thread(target=recovery)
        recoverer.start()
        self.assertTrue(recovering.wait(1))
        release.set()
        worker.join(1)
        recoverer.join(1)
        self.assertFalse(worker.is_alive() or recoverer.is_alive())
        self.assertEqual(failures, [])
        self.assertEqual(results, [True, False])
        self.assertEqual(ledger.attempts()[0]['state'], 'reserved')
        ledger.dispatch('j')
        ledger.settle('j', Decimal('.01'), 1, 1)
        ledger.job('j', 'r', 'completed', 3)
        self.assertTrue(ledger.barrier('run'))
        self.assertFalse(ledger.claim_and_reserve('j', 'r', 'm', 'mock', 'v', Decimal('.1')))
        self.assertEqual(len(ledger.attempts()), 1)


class ReviewAPICompositionTests(unittest.IsolatedAsyncioTestCase):
    async def test_api_real_flags_and_shared_spend_with_stubbed_client(self):
        # Load the actual composition code with a minimal transport stub when
        # optional FastAPI is absent. This does not claim HTTP verification.
        import importlib.util
        from types import ModuleType
        fake_fastapi = ModuleType('fastapi')
        class App:
            def __init__(self, **kwargs):
                self.state = SimpleNamespace()
            def get(self, *args):
                return lambda function: function
            post = get
            exception_handler = get
        fake_fastapi.FastAPI, fake_fastapi.Request = App, object
        fake_responses = ModuleType('fastapi.responses')
        fake_responses.JSONResponse = fake_responses.Response = object
        redis_stub = SimpleNamespace(Redis=SimpleNamespace(from_url=lambda *a, **kw: object()))
        spec = importlib.util.spec_from_file_location('review_api_composition', 'api/app.py')
        module = importlib.util.module_from_spec(spec)
        with patch.dict('sys.modules', {'fastapi': fake_fastapi, 'fastapi.responses': fake_responses,
                                        'redis': redis_stub}):
            spec.loader.exec_module(module)
            with patch.dict('os.environ', {'PROVIDER_MODE': 'real'}, clear=True):
                with self.assertRaises(ValueError):
                    module.build_router()
                with self.assertRaises(ValueError):
                    module.build_router(real=True)
            with patch.dict('os.environ', {'ROUTER_CONFIRM_REAL_SPEND': 'YES',
                                           'STORAGE_MODE': 'test-double'}, clear=True):
                with self.assertRaises(ValueError):
                    module.build_router(real=True)
            calls = []
            async def completion(**kwargs):
                calls.append(kwargs)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='stub'),
                                                                finish_reason='stop')],
                                       usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))
            with patch.dict('os.environ', {'ROUTER_CONFIRM_REAL_SPEND': 'YES'}, clear=True), patch.dict(
                    'sys.modules', {'litellm': SimpleNamespace(acompletion=completion)}):
                with patch.object(module, 'PostgresLedger', side_effect=lambda *a, **kw: SQLiteLedger(**kw)):
                    router = module.build_router(real=True)
                # The composition supplies one admission object to both callers.
                self.assertIs(router.quality.admission, router.admission)
                # Substitute local infrastructure so only the provider adapter is exercised.
                router.admission = __import__('routing.admission', fromlist=['Admission']).Admission()
                router.health, router.rate = router.admission.health, router.admission.rate
                router.quality = None
                result = await router.route(req(), use_cache=False)
                self.assertEqual(result['data_source'], 'real-unverified')
                self.assertEqual(len(calls), 1)
                self.assertEqual(router.ledger.attempts()[0]['state'], 'settled')
                from monitoring.metrics import prometheus
                self.assertIn('source="real-unverified"', prometheus(router.ledger, data_source=router.provider.mode))


class ReviewBackgroundAndJudgeEntryTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_quality_submission_is_off_response_path(self):
        entered, release = threading.Event(), threading.Event()
        class SlowQuality:
            def raised(self, tier):
                return False
            def submit(self, *args):
                entered.set()
                release.wait(2)
        router = Router(SQLiteLedger(), quality=SlowQuality())
        started = asyncio.get_running_loop().time()
        try:
            result = await router.route(req(deadline_ms=100), use_cache=False)
            self.assertLess(asyncio.get_running_loop().time() - started, .5)
            self.assertTrue(result['content'])
            self.assertTrue(await asyncio.to_thread(entered.wait, .5))
        finally:
            release.set()
            await router.drain_background()

    async def test_real_judge_requires_flag_and_uses_only_stubbed_litellm(self):
        import benchmark.judge as module
        from benchmark.results import analyse, write_results
        prompts, manifest = split('dev')
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, 'stub-real.json')
            ledger = SQLiteLedger(path.with_suffix('.sqlite'))
            try:
                ledger.begin('r', 'run')
                records = [dict(request_id='r', prompt_id=prompts[0]['id'], condition='A',
                                content='stub answer', completed=True, cost='0', quality=None)]
                write_results(path, ledger, 'run',
                              dict(data_source='real-unverified', storage='postgres', seed=13,
                                   config=Config().data, dataset_checksum=manifest['sha256'],
                                   analysis=analyse(records, 1, resamples=10)), records)
                with patch.dict('os.environ', {'ROUTER_CONFIRM_REAL_SPEND': 'YES'}, clear=True):
                    with self.assertRaises(ValueError):
                        await module.judge(path)
                    calls = []
                    async def completion(**kwargs):
                        calls.append(kwargs)
                        return SimpleNamespace(choices=[SimpleNamespace(
                            message=SimpleNamespace(content='{"score":3}'), finish_reason='stop')],
                            usage=SimpleNamespace(prompt_tokens=2, completion_tokens=2))
                    with patch.dict('sys.modules', {'litellm': SimpleNamespace(acompletion=completion)}), patch.object(
                            module, 'PostgresLedger', return_value=ledger), patch.object(
                                module, 'shared_admission', return_value=__import__(
                                    'routing.admission', fromlist=['Admission']).Admission()):
                        result = await module.judge(path, real=True)
                        await module.judge(path, real=True)
                    self.assertEqual(result['data_source'], 'real-unverified')
                    self.assertEqual(result['quality_source'], 'diagnostic_real_judge')
                    self.assertEqual(len(calls), 1)
                    self.assertEqual(ledger.attempts()[0]['purpose'], 'evaluation')
                    self.assertEqual(ledger.attempts()[0]['state'], 'settled')
            finally:
                ledger.close()
