"""Timeout/retry/paid-answer regressions. All providers are in-process fakes."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from decimal import Decimal
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from api.contracts import Request, RouterError
from benchmark.results import write_results
from benchmark.run import run_conditions
from fallback.providers import MockProvider, Output
from monitoring.ledger import PostgresLedger, SQLiteLedger, StorageUnavailable, TransientContention
from monitoring.settlement import SettlementJournal
from routing.config import Config
from routing.service import Router


def request(**kwargs):
    return Request.parse(dict(messages=[dict(role='user', content='hello')], **kwargs))


class AdmissionRetryTests(unittest.TestCase):
    def test_bounded_contention_retry_and_no_outage_retry(self):
        ledger = SQLiteLedger()
        self.addCleanup(ledger.close)
        for exc, count in ((TransientContention(), 3), (StorageUnavailable(), 1)):
            operation = unittest.mock.Mock(side_effect=exc)
            with patch('monitoring.ledger.time.sleep') as sleep:
                with self.assertRaises(StorageUnavailable):
                    ledger.retry_admission(operation)
            self.assertEqual(operation.call_count, count)
            self.assertEqual(sleep.call_count, count - 1)
            for call in sleep.call_args_list:
                self.assertGreaterEqual(call.args[0], .01)
                self.assertLessEqual(call.args[0], .06)

    def test_expired_deadline_stops_retries(self):
        ledger = SQLiteLedger()
        self.addCleanup(ledger.close)
        operation = unittest.mock.Mock(side_effect=TransientContention())
        with ledger.deadline(0, clock=lambda: 1), patch('monitoring.ledger.time.sleep') as sleep:
            with self.assertRaises(TransientContention):
                ledger.retry_admission(operation)
        self.assertEqual(operation.call_count, 1)
        sleep.assert_not_called()

    def test_sqlite_n8_idempotent_reserve_and_settle_cycles(self):
        ledger = SQLiteLedger(cap="1")
        self.addCleanup(ledger.close)
        ledger.begin("r")
        def cycle(i):
            attempt = str(i)
            ledger.reserve(attempt, "r", "fake", "mock", "inference", "v", ".1")
            ledger.reserve(attempt, "r", "fake", "mock", "inference", "v", ".1")
            ledger.dispatch(attempt)
            output = Output("in-process fake", 1, 1)
            ledger.settle(attempt, ".01", output.input_tokens, output.output_tokens)
            ledger.settle(attempt, ".01", output.input_tokens, output.output_tokens)
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(cycle, range(8)))
        self.assertEqual(len(ledger.attempts()), 8)
        self.assertEqual(sum(Decimal(str(a["cost"])) for a in ledger.attempts()), Decimal(".08"))

    def test_sqlstate_classification(self):
        from test_ledger_lock import Connection, DriverError, fake_ledger
        for sqlstate in ("57014", "55P03", "40001", "40P01", "08006", "23505"):
            with self.subTest(sqlstate=sqlstate), patch.object(DriverError, 'sqlstate', sqlstate):
                ledger = fake_ledger(Connection())
                with self.assertLogs('monitoring.ledger', level='ERROR'):
                    with self.assertRaises(StorageUnavailable) as error:
                        with ledger.transaction():
                            raise DriverError('private data')
                self.assertEqual(isinstance(error.exception, TransientContention),
                                 sqlstate in {"57014", "55P03", "40001", "40P01"})

    def test_configurable_statement_budget(self):
        with patch.dict(os.environ, LEDGER_STATEMENT_TIMEOUT_MS='1234'):
            ledger = SQLiteLedger()
            with ledger.deadline(0, clock=lambda: 100):
                self.assertEqual(ledger.transaction_timeout(), 1234)
            ledger.close()
        for invalid in (0, -1, True, 1.5, '2000'):
            with self.assertRaises(ValueError):
                SQLiteLedger(statement_timeout_ms=invalid)


class SettlementContract:
    async def test_retried_settlement_preserves_answer_and_durable_content(self):
        with self.storage() as ledger, TemporaryDirectory() as directory:
            journal = SettlementJournal(Path(directory, 'response.sqlite'))
            model = Config().model('cheap')['id']
            provider = MockProvider({model: [Output('paid answer', 2, 3, resolved_model='snapshot')]})
            router = Router(ledger, provider=provider, settlement_journal=journal)
            original = ledger.settle
            calls = []
            def flaky(*args, **kwargs):
                calls.append(args[0])
                if len(calls) == 2:
                    self.assertEqual(SettlementJournal(journal.path).entries()[0]['payload']['content'], 'paid answer')
                if len(calls) < 3:
                    raise TransientContention('fixture')
                return original(*args, **kwargs)
            with patch.object(ledger, 'settle', side_effect=flaky):
                response = await router.route(request(), use_cache=False)
            self.assertEqual(response['content'], 'paid answer')
            self.assertEqual(response['settlement_status'], 'settlement_retried')
            self.assertEqual(len(set(calls)), 1)
            self.assertEqual(len(provider.calls), 1)
            attempt = ledger.attempts()[0]
            self.assertEqual(attempt['state'], 'settled')
            self.assertEqual(Decimal(str(attempt['cost'])), Config().cost('cheap', 2, 3))
            reopened = SettlementJournal(journal.path)
            self.assertEqual(reopened.entries()[0]['payload']['content'], 'paid answer')
            reopened.recover(ledger)
            self.assertTrue(reopened.entries()[0]['synced'])
            self.assertEqual(len(ledger.attempts()), 1)

    async def test_total_outage_after_paid_response_returns_unknown_and_recovers(self):
        with self.storage() as ledger, TemporaryDirectory() as directory:
            journal = SettlementJournal(Path(directory, 'response.sqlite'))
            class OutageProvider(MockProvider):
                async def complete(self, *args):
                    result = await super().complete(*args)
                    ledger.available = False
                    return result
            provider = OutageProvider()
            router = Router(ledger, provider=provider, settlement_journal=journal)
            response = await router.route(request(), use_cache=False)
            self.assertTrue(response['content'])
            self.assertEqual(response['settlement_status'], 'unknown')
            saved = SettlementJournal(journal.path).entries()[0]
            self.assertEqual(saved['payload']['content'], response['content'])
            self.assertFalse(saved['synced'])
            ledger.available = True
            self.assertEqual(Decimal(str(ledger.attempts()[0]['cost'])), Decimal(saved['payload']['reserved']))
            journal.recover(ledger)
            journal.recover(ledger)
            attempt = ledger.attempts()[0]
            self.assertEqual((attempt['state'], attempt['cost_status']), ('unknown', 'unknown'))
            self.assertEqual(Decimal(str(response['cost'])), Decimal(str(attempt['reserved'])))
            self.assertEqual(ledger.requests()[0]['outcome'], '200')
            self.assertEqual(len(provider.calls), 1)
            self.assertTrue(ledger.barrier())

    async def test_lost_commit_acknowledgements_never_double_charge_or_dispatch(self):
        with self.storage() as ledger, TemporaryDirectory() as directory:
            original = ledger.reserve
            count = [0]
            def reserve(*args):
                original(*args)
                count[0] += 1
                if count[0] == 1:
                    raise TransientContention()
            dispatch = ledger.dispatch
            dispatched = [0]
            def dispatch_then_fail(*args):
                dispatch(*args)
                dispatched[0] += 1
                if dispatched[0] == 1:
                    raise TransientContention()
            settle = ledger.settle
            settled = [0]
            def commit_then_fail(*args, **kwargs):
                result = settle(*args, **kwargs)
                settled[0] += 1
                if settled[0] == 1:
                    raise StorageUnavailable()
                return result
            provider = MockProvider()
            router = Router(ledger, provider=provider,
                            settlement_journal=SettlementJournal(Path(directory, 'responses.sqlite')))
            with patch.object(ledger, 'reserve', side_effect=reserve), patch.object(ledger, 'settle', side_effect=commit_then_fail), patch.object(ledger, 'dispatch', side_effect=dispatch_then_fail):
                response = await router.route(request(), use_cache=False)
            self.assertEqual(response['settlement_status'], 'settlement_retried')
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(len(ledger.attempts()), 1)
            self.assertEqual(Decimal(str(ledger.requests()[0]['inference_cost'])),
                             Decimal(str(ledger.attempts()[0]['cost'])))
            with self.assertRaises(ValueError):
                ledger.reserve(ledger.attempts()[0]['attempt_id'], response['request_id'], 'different',
                               'mock', 'inference', 'v', '1')

    async def test_request_deadline_remains_504_after_settlement_retry(self):
        with self.storage() as ledger, TemporaryDirectory() as directory:
            now = [0]
            original = ledger.settle
            count = [0]
            def slow(*args, **kwargs):
                count[0] += 1
                now[0] = 2
                if count[0] == 1:
                    raise TransientContention()
                return original(*args, **kwargs)
            journal = SettlementJournal(Path(directory, 'response.sqlite'))
            provider = MockProvider()
            router = Router(ledger, provider=provider, clock=lambda: now[0], settlement_journal=journal)
            with patch.object(ledger, 'settle', side_effect=slow):
                with self.assertRaises(RouterError) as error:
                    await router.route(request(deadline_ms=1000), use_cache=False)
            self.assertEqual(error.exception.status, 504)
            self.assertEqual(ledger.attempts()[0]['state'], 'settled')
            journal.recover(ledger)
            self.assertEqual(ledger.requests()[0]['outcome'], '504')
            self.assertTrue(journal.entries()[0]['payload']['content'])


class SQLiteSettlementTests(SettlementContract, unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def storage(self):
        ledger = SQLiteLedger()
        try:
            yield ledger
        finally:
            ledger.close()

    async def test_benchmark_preserves_completed_unknown(self):
        with self.storage() as ledger, TemporaryDirectory() as directory:
            path = str(Path(directory, 'response.sqlite'))
            with patch.dict(os.environ, LEDGER_RESPONSE_STORE=path):
                router = Router(ledger)
                with patch.object(ledger, 'settle', side_effect=StorageUnavailable()):
                    run, records = await run_conditions(router, [dict(id='fixture', prompt='hello')])
                self.assertTrue(all(r['completed'] and r['status'] == 200 for r in records))
                self.assertTrue(all(r['settlement_status'] == 'unknown' for r in records))
                result = write_results(Path(directory, 'export.json'), ledger, run, {}, records)
                self.assertEqual(len(result['records']), 4)
                self.assertTrue(result['final'])
                self.assertTrue(all(r['settlement_status'] == 'unknown' for r in result['records']))
                self.assertTrue(all(Decimal(r['cost']) > 0 for r in result['records']))

    async def test_benchmark_restart_recovers_journal_content_without_provider_replay(self):
        from benchmark.recover import recover
        with TemporaryDirectory() as directory:
            path = Path(directory, 'run.sqlite')
            ledger = SQLiteLedger(path)
            ledger.save_run('run', {})
            ledger.save_record('run', dict(request_id='r', prompt_id='fixture', condition='A',
                                          completed=False, status='pending'))
            ledger.reserve('a', 'r', 'fake', 'mock', 'inference', 'v', '.1')
            ledger.dispatch('a')
            journal = SettlementJournal(Path(directory, 'response.sqlite'))
            journal.save('a', dict(resolved_model='snapshot', input_tokens=2, output_tokens=3,
                                  actual='.01', reserved='.1', rejected=False, error=None,
                                  content='durable paid answer', stop_reason='stop',
                                  response=dict(content='durable paid answer', model='fake',
                                                resolved_model='snapshot', tier='simple', request_id='r',
                                                stop_reason='stop')))
            ledger.close()
            with patch.dict(os.environ, LEDGER_RESPONSE_STORE=str(journal.path)):
                result = recover(path, run_id='run', storage='test-double')
            self.assertEqual(result['records'][0]['content'], 'durable paid answer')
            self.assertTrue(result['records'][0]['completed'])
            self.assertEqual(result['records'][0]['settlement_status'], 'settlement_retried')
            self.assertEqual(Decimal(result['records'][0]['cost']), Decimal('.01'))
            self.assertTrue(result['final'])

    async def test_empty_answer_fails_attempt_and_moves_to_next_model_without_same_model_retry(self):
        with self.storage() as ledger, TemporaryDirectory() as directory:
            config = Config()
            cheap, mid = (config.model(a)['id'] for a in ('cheap', 'mid'))
            provider = MockProvider({cheap: [Output('', 2, 3)], mid: [Output('fallback answer', 2, 3)]})
            router = Router(ledger, provider=provider,
                            settlement_journal=SettlementJournal(Path(directory, 'response.sqlite')))
            response = await router.route(request(), use_cache=False)
            self.assertEqual(provider.calls, [cheap, mid])
            self.assertEqual(response['content'], 'fallback answer')
            failed = ledger.attempts()[0]
            self.assertEqual(json.loads(failed['error'])['status'], 400)
            self.assertEqual(failed['cost_status'], 'measured')
            self.assertGreater(Decimal(str(failed['cost'])), 0)


@unittest.skipUnless(os.environ.get('ROUTER_TEST_POSTGRES_DSN'), 'Local Postgres DSN not supplied')
class PostgresSettlementTests(SettlementContract, unittest.IsolatedAsyncioTestCase):
    @contextmanager
    def storage(self):
        from test_services import isolated_postgres
        with isolated_postgres() as dsn:
            yield PostgresLedger(dsn)


@unittest.skipUnless(os.environ.get('ROUTER_TEST_POSTGRES_DSN'), 'Local Postgres DSN not supplied')
class PostgresStatementTests(unittest.TestCase):
    def test_n8_slow_statement_reproduces_57014_then_default_budget_passes(self):
        import psycopg
        from test_services import isolated_postgres
        with isolated_postgres() as dsn:
            legacy = PostgresLedger(dsn, statement_timeout_ms=200)
            legacy.begin('r')
            with psycopg.connect(dsn) as conn:
                conn.execute("CREATE FUNCTION delayed_insert() RETURNS trigger LANGUAGE plpgsql AS $$ "
                             "BEGIN PERFORM pg_sleep(0.25); RETURN NEW; END $$")
                conn.execute('CREATE TRIGGER delayed_insert BEFORE INSERT ON attempts FOR EACH ROW '
                             'EXECUTE FUNCTION delayed_insert()')
            provider = lambda: Output('fake paid answer', 1, 1)
            def cycle(ledger, i):
                attempt = 'a' + str(i)
                try:
                    ledger.reserve(attempt, 'r', 'fake', 'mock', 'inference', 'v', '.01')
                    ledger.dispatch(attempt)
                    output = provider()
                    ledger.settle(attempt, '.001', output.input_tokens, output.output_tokens)
                    return True
                except TransientContention:
                    return False
            with self.assertLogs('monitoring.ledger', level='ERROR') as logs:
                with ThreadPoolExecutor(max_workers=8) as pool:
                    self.assertEqual(sum(pool.map(lambda i: cycle(legacy, i), range(8))), 0)
            self.assertEqual(sum('QueryCanceled sqlstate=57014' in line for line in logs.output), 8)
            fixed = PostgresLedger(dsn)
            fixed.ping()
            with ThreadPoolExecutor(max_workers=8) as pool:
                self.assertEqual(sum(pool.map(lambda i: cycle(fixed, i), range(8))), 8)
            self.assertEqual(len(fixed.attempts()), 8)
            self.assertEqual(sum(Decimal(str(a['cost'])) for a in fixed.attempts()), Decimal('.008'))

    def test_real_serialization_failure_retries_same_reservation(self):
        import psycopg
        from test_services import isolated_postgres
        with isolated_postgres() as dsn:
            ledger = PostgresLedger(dsn)
            ledger.begin('r')
            with psycopg.connect(dsn) as conn:
                conn.execute('CREATE SEQUENCE inject_failure')
                conn.execute("CREATE FUNCTION fail_twice() RETURNS trigger LANGUAGE plpgsql AS $$ "
                             "BEGIN IF nextval('inject_failure') <= 2 THEN "
                             "RAISE EXCEPTION 'fixture' USING ERRCODE='40001'; END IF; RETURN NEW; END $$")
                conn.execute('CREATE TRIGGER fail_twice BEFORE INSERT ON attempts FOR EACH ROW '
                             'EXECUTE FUNCTION fail_twice()')
            with self.assertLogs('monitoring.ledger', level='ERROR') as logs:
                ledger.retry_admission(ledger.reserve, 'a', 'r', 'fake', 'mock', 'inference', 'v', '.01')
            self.assertEqual(sum('sqlstate=40001' in line for line in logs.output), 2)
            self.assertEqual(len(ledger.attempts()), 1)
            self.assertEqual(Decimal(str(ledger.attempts()[0]['cost'])), Decimal('.01'))

    def test_real_settlement_cancellation_is_retried_and_answer_is_returned(self):
        import psycopg
        from test_services import isolated_postgres
        with isolated_postgres() as dsn, TemporaryDirectory() as directory:
            ledger = PostgresLedger(dsn)
            ledger.ping()
            with psycopg.connect(dsn) as conn:
                conn.execute('CREATE SEQUENCE cancel_once')
                conn.execute("CREATE FUNCTION cancel_settlement() RETURNS trigger LANGUAGE plpgsql AS $$ "
                             "BEGIN IF NEW.state='settled' AND nextval('cancel_once')=1 THEN "
                             "RAISE EXCEPTION 'fixture' USING ERRCODE='57014'; END IF; RETURN NEW; END $$")
                conn.execute('CREATE TRIGGER cancel_settlement BEFORE UPDATE ON attempts FOR EACH ROW '
                             'EXECUTE FUNCTION cancel_settlement()')
            journal = SettlementJournal(Path(directory, 'response.sqlite'))
            provider = MockProvider()
            router = Router(ledger, provider=provider, settlement_journal=journal)
            with self.assertLogs('monitoring.ledger', level='ERROR') as logs:
                response = asyncio.run(router.route(request(), use_cache=False))
            self.assertEqual(response['settlement_status'], 'settlement_retried')
            self.assertTrue(response['content'])
            self.assertIn('QueryCanceled sqlstate=57014', str(logs.output))
            self.assertEqual(len(provider.calls), 1)
            self.assertEqual(ledger.attempts()[0]['state'], 'settled')
            self.assertEqual(journal.entries()[0]['payload']['content'], response['content'])

    def test_ordinary_read_has_statement_budget_even_with_expired_request(self):
        from test_services import isolated_postgres
        with isolated_postgres() as dsn:
            ledger = PostgresLedger(dsn)
            with ledger.deadline(0, clock=lambda: 10):
                with ledger.transaction() as db:
                    db.execute('SELECT pg_sleep(0.25)')
