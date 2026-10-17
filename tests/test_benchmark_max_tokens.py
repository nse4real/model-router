"""Mock-only output-limit tests using synthetic prompts and in-memory ledgers."""
from contextlib import closing
from dataclasses import replace
from decimal import Decimal
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from api.contracts import Request
from benchmark.run import run_conditions
from fallback.providers import MockProvider, ProviderError
from monitoring.ledger import SQLiteLedger
from routing.config import Config
from routing.service import Router


class BenchmarkMaxTokensTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        environment = patch.dict(os.environ, LEDGER_RESPONSE_STORE=str(Path(temporary.name) / 'journal.sqlite'))
        environment.start()
        self.addCleanup(environment.stop)
        guard = patch('benchmark.run.real_provider', side_effect=AssertionError('Real provider forbidden'))
        guard.start()
        self.addCleanup(guard.stop)

    async def check_conditions(self, config, expected):
        with closing(SQLiteLedger()) as ledger:
            provider = MockProvider()
            router = Router(ledger, config=config, provider=provider)
            with patch.object(router, 'route', wraps=router.route) as route, patch.object(
                    provider, 'complete', wraps=provider.complete) as complete, patch.object(
                    ledger, 'save_run', wraps=ledger.save_run) as save_run:
                _, records = await run_conditions(router, [dict(id='fixture', prompt='Synthetic output limit')])
            self.assertEqual({r['condition'] for r in records}, {'A', 'B', 'C-cold', 'C-warm'})
            self.assertTrue(all(r['status'] == 200 for r in records))
            self.assertEqual(route.await_count, 4)
            self.assertTrue(all(c.args[0].max_tokens == expected for c in route.await_args_list))
            self.assertEqual(complete.await_count, 3)
            self.assertTrue(all(c.args[1].max_tokens == expected for c in complete.await_args_list))
            metadata = save_run.call_args.args[1]
            self.assertEqual(metadata['benchmark_max_tokens'], expected)
            self.assertEqual(metadata['config']['benchmark_max_tokens'], expected)
            warm = next(r for r in records if r['condition'] == 'C-warm')
            self.assertTrue(warm['cache_hit'])
            self.assertEqual(ledger.attempts(warm['request_id']), [])

    async def test_benchmark_requests_use_1024_for_all_conditions(self):
        await self.check_conditions(Config(), 1024)

    async def test_configured_limit_reaches_every_condition_and_provider(self):
        await self.check_conditions(Config(data=dict(Config().data, benchmark_max_tokens=777)), 777)

    async def test_live_default_remains_512_with_benchmark_config(self):
        request = Request.parse(dict(messages=[dict(role='user', content='Synthetic live request')]))
        self.assertEqual(request.max_tokens, 512)
        self.assertEqual(Request(messages=request.messages).max_tokens, 512)
        with closing(SQLiteLedger()) as ledger:
            provider = MockProvider()
            router = Router(ledger, config=Config(), provider=provider)
            with patch.object(provider, 'complete', wraps=provider.complete) as complete:
                await router.route(request, use_cache=False)
            self.assertEqual(complete.await_count, 1)
            self.assertEqual(complete.await_args.args[1].max_tokens, 512)
            await router.drain_background()

    def test_config_default_and_valid_boundaries(self):
        data = Config().data
        self.assertEqual(data.pop('benchmark_max_tokens'), 1024)
        self.assertEqual(Config(data=data).benchmark_max_tokens, 1024)
        for value in (1, 2048):
            with self.subTest(value=value):
                self.assertEqual(Config(data=dict(data, benchmark_max_tokens=value)).benchmark_max_tokens, value)

    def test_config_rejects_out_of_range_and_noninteger_limits(self):
        for value in (-1, 0, 2049, True, False, '1024', 1024.0, 1.5, None):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, 'benchmark_max_tokens'):
                Config(data=dict(Config().data, benchmark_max_tokens=value))

    async def test_reservation_matches_provider_limit_including_fallback(self):
        config = Config()
        provider = MockProvider({config.model('cheap')['id']: [ProviderError(400)]})
        original = provider.complete
        observed = []
        with closing(SQLiteLedger()) as ledger:
            async def complete(model, request, timeout):
                attempts = [a for a in ledger.attempts(request.request_id) if a['state'] == 'dispatched']
                self.assertEqual(len(attempts), 1)
                attempt = attempts[0]
                self.assertEqual(attempt['model'], model['id'])
                self.assertEqual(request.max_tokens, 1024)
                alias = next(a for a, m in config.models.items() if m['id'] == model['id'])
                input_bound = sum(len(m['content'].encode()) + len(m['role']) + 16
                                  for m in request.messages) + 64
                expected = config.cost(alias, input_bound, request.max_tokens)
                self.assertEqual(Decimal(str(attempt['reserved'])), expected)
                smaller = config.reserve_cost(alias, replace(request, max_tokens=512))
                self.assertEqual(expected - smaller, config.cost(alias, 0, 512))
                observed.append(attempt['attempt_id'])
                return await original(model, request, timeout)

            router = Router(ledger, config=config, provider=provider)
            with patch.object(provider, 'complete', side_effect=complete):
                _, records = await run_conditions(router, [dict(id='fixture', prompt='Synthetic fallback')])
            self.assertTrue(all(r['status'] == 200 for r in records))
            self.assertEqual(len(observed), 4)  # Three cold calls plus one failed primary.
            self.assertEqual(len(observed), len(ledger.attempts()))
            self.assertTrue(any(len(ledger.attempts(r['request_id'])) == 2 for r in records))
