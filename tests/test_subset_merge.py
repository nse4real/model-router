"""Offline synthetic fixtures only; no calibration prompts or real providers run."""
from contextlib import closing, redirect_stdout
from copy import deepcopy
from decimal import Decimal
import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from api.contracts import Request, RouterError
from benchmark.dataset import split
from benchmark.merge import merge
from benchmark.run import build_parser, main, run_conditions, select_prompts
from fallback.providers import MockProvider, Output
from monitoring.ledger import SQLiteLedger
from routing.config import Config
from routing.service import Router


class TemporaryFixtures(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        environment = patch.dict(os.environ, LEDGER_RESPONSE_STORE=str(self.directory / 'journal.sqlite'))
        environment.start()
        self.addCleanup(environment.stop)

    def write(self, name, value):
        path = self.directory / name
        path.write_text(json.dumps(value), encoding='utf8')
        return path


class SubsetTests(TemporaryFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_subset_run_executes_only_requested_ids_all_conditions_same_seed_and_manifest(self):
        # Replace split() with synthetic inputs; never execute the calibration dataset.
        prompts = [dict(id=f'fixture-{i}', prompt=f'Synthetic fixture {i}') for i in range(3)]
        _, manifest = split('dev')
        original = deepcopy(manifest)
        ledgers, routers, outputs = [], [], []

        def ledger(*args, **kwargs):
            value = SQLiteLedger(*args, **kwargs)
            ledgers.append(value)
            return value

        def router(*args, **kwargs):
            value = Router(*args, **kwargs)
            value.route = unittest.mock.AsyncMock(wraps=value.route)
            routers.append(value)
            return value

        try:
            for index, ids in enumerate((['fixture-2', 'fixture-0'], ['fixture-0', 'fixture-2'])):
                ids_path = self.write(f'ids-{index}.json', ids)
                output = self.directory / f'run-{index}.json'
                args = build_parser().parse_args(['--split', 'calibration', '--prompt-ids-file', str(ids_path),
                    '--storage', 'test-double', '--seed', '47', '--output', str(output)])
                with patch('benchmark.run.split', return_value=(prompts, manifest)), patch(
                        'benchmark.run.SQLiteLedger', side_effect=ledger), patch(
                        'benchmark.run.Router', side_effect=router), patch(
                        'benchmark.run.real_provider', side_effect=AssertionError('Real provider forbidden')), redirect_stdout(io.StringIO()):
                    await main(args)
                result = json.loads(output.read_text(encoding='utf8'))
                outputs.append(result)
                self.assertEqual({r['prompt_id'] for r in result['records']}, set(ids))
                self.assertEqual(len(result['records']), 8)
                for prompt_id in ids:
                    self.assertEqual({r['condition'] for r in result['records'] if r['prompt_id'] == prompt_id},
                                     {'A', 'B', 'C-cold', 'C-warm'})
                self.assertEqual(result['seed'], 47)
                self.assertEqual(result['prompt_ids'], ['fixture-0', 'fixture-2'])
                self.assertEqual(result['prompt_count'], 2)
                self.assertEqual(result['dataset_checksum'], manifest['sha256'])
                self.assertEqual(result['human_subset'], manifest['human_subset'])
                self.assertEqual(result['reproducibility_subset'], manifest['reproducibility_subset'])
                self.assertTrue(result['final'])
                self.assertEqual(routers[-1].route.await_count, 8)
                self.assertTrue(all(c.args[0].deadline_ms == 60000 for c in routers[-1].route.await_args_list))
            self.assertEqual([(r['prompt_id'], r['condition']) for r in outputs[0]['records']],
                             [(r['prompt_id'], r['condition']) for r in outputs[1]['records']])
            self.assertEqual(manifest, original)
        finally:
            for value in ledgers:
                value.close()

    async def test_subset_refuses_existing_output_before_storage_or_provider(self):
        output = self.write('existing.json', {'preserve': True})
        before = output.read_bytes()
        args = build_parser().parse_args(['--output', str(output)])
        with patch('benchmark.run.SQLiteLedger') as ledger, patch('benchmark.run.PostgresLedger') as postgres, patch(
                'benchmark.run.real_provider') as provider, self.assertRaises(SystemExit):
            await main(args)
        ledger.assert_not_called()
        postgres.assert_not_called()
        provider.assert_not_called()
        self.assertEqual(output.read_bytes(), before)

    async def test_subset_exclusive_creation_rejects_output_race(self):
        output = self.directory / 'raced.json'
        prompts, manifest = split('dev')

        def competing_writer(*args):
            output.write_text('concurrent owner', encoding='utf8')
            return Config()

        args = build_parser().parse_args(['--storage', 'test-double', '--output', str(output)])
        with patch('benchmark.run.split', return_value=(prompts, manifest)), patch(
                'benchmark.run.Config', side_effect=competing_writer), patch(
                'benchmark.run.SQLiteLedger') as ledger, self.assertRaises(SystemExit):
            await main(args)
        ledger.assert_not_called()
        self.assertEqual(output.read_text(encoding='utf8'), 'concurrent owner')

    def test_subset_rejects_invalid_ids_and_other_splits(self):
        prompts = [dict(id='fixture', prompt='Synthetic')]
        for value in ({'ids': ['fixture']}, [], ['fixture', 'fixture'], ['absent'], [42], ['']):
            with self.subTest(value=value), self.assertRaises(ValueError):
                select_prompts(prompts, self.write('ids.json', value), 'calibration')
        for split_name in ('dev', 'held-out'):
            with self.subTest(split=split_name), self.assertRaises(ValueError):
                select_prompts(prompts, self.write('ids.json', ['fixture']), split_name)


class BenchmarkDeadlineTests(TemporaryFixtures, unittest.IsolatedAsyncioTestCase):
    async def test_benchmark_uses_60_seconds_and_live_keeps_10_seconds(self):
        class Clock:
            now = 0

            def __call__(self):
                return self.now

        class SlowMock(MockProvider):
            async def complete(self, model, request, timeout):
                self.calls.append((request.deadline_ms, timeout))
                clock.now += 15
                return Output('Synthetic response', 1, 1, resolved_model=model['id'])

        clock = Clock()
        with closing(SQLiteLedger()) as ledger:
            provider = SlowMock()
            router = Router(ledger, clock=clock, provider=provider)
            with patch.object(router, 'route', wraps=router.route) as route:
                _, records = await run_conditions(router, [dict(id='fixture', prompt='Synthetic')])
            self.assertEqual(route.await_count, 4)
            self.assertTrue(all(c.args[0].deadline_ms == 60000 for c in route.await_args_list))
            self.assertTrue(all(r['status'] == 200 for r in records))
            self.assertTrue(all(deadline == 60000 and timeout == 30 for deadline, timeout in provider.calls))
            request = Request.parse(dict(messages=[dict(role='user', content='Synthetic live')]))
            self.assertEqual(request.deadline_ms, 10000)
            self.assertEqual(Request(messages=request.messages).deadline_ms, 10000)
            live = Router(ledger, clock=clock, provider=provider)
            with self.assertRaises(RouterError) as error:
                await live.route(request, use_cache=False, force_model='mid')
            self.assertEqual(error.exception.status, 504)
            self.assertEqual(provider.calls[-1], (10000, 10))

    async def test_configured_benchmark_deadline_applies_to_every_condition(self):
        config = Config()
        config.data['benchmark_deadline_ms'] = 12345
        with closing(SQLiteLedger()) as ledger:
            router = Router(ledger, config=config)
            with patch.object(router, 'route', wraps=router.route) as route:
                _, records = await run_conditions(router, [dict(id='fixture', prompt='Synthetic')])
            self.assertEqual(len(records), 4)
            self.assertTrue(all(c.args[0].deadline_ms == 12345 for c in route.await_args_list))

    def test_config_default_and_invalid_benchmark_deadlines(self):
        data = Config().data
        self.assertEqual(data.pop('benchmark_deadline_ms'), 60000)
        self.assertEqual(Config(data=data).benchmark_deadline_ms, 60000)
        for value in (True, 0, 60001, '60000', 1.5, None):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config(data=dict(data, benchmark_deadline_ms=value))


class MergeTests(TemporaryFixtures):
    def setUp(self):
        super().setUp()
        def record(prompt, condition, name, cost, state='settled', purpose='inference'):
            return dict(prompt_id=prompt, condition=condition, request_id=name, content=name,
                        completed=True, cost='999', attempts=[dict(attempt_id=name, cost=cost,
                        purpose=purpose, state=state, cost_status='unknown' if state == 'unknown' else 'measured')])
        self.base = dict(split='calibration', seed=13, final=True, prompt_count=2, run_id='base-run',
                         analysis={'stale': True}, totals={'total_cost': '999'}, records=[
            record('keep', 'A', 'keep-a', '.1'), record('keep', 'B', 'keep-b', '.2'),
            record('replace', 'A', 'old-a', '.3', state='unknown'),
            record('replace', 'B', 'old-b', '.4')])
        self.subset = dict(split='calibration', seed=13, final=True, run_id='subset-run', records=[
            record('replace', 'A', 'new-a', '.5'), record('replace', 'B', 'new-b', '.6', purpose='evaluation')])
        self.base_path = self.write('base.json', self.base)
        self.subset_path = self.write('subset.json', self.subset)
        self.output = self.directory / 'merged.json'

    def test_merge_replaces_only_subset_prompts_and_records_provenance(self):
        result = merge(self.base_path, self.subset_path, self.output)
        expected = [dict(row, source=str(self.base_path.resolve())) for row in self.base['records'][:2]]
        expected += [dict(row, source=str(self.subset_path.resolve())) for row in self.subset['records']]
        self.assertEqual(result['records'], expected)
        self.assertEqual(result['merge'], dict(base_path=str(self.base_path.resolve()),
            subset_path=str(self.subset_path.resolve()), replaced_prompt_ids=['replace']))
        self.assertTrue(result['merged'])
        self.assertEqual(result['prompt_count'], 2)
        self.assertNotEqual(result['run_id'], self.base['run_id'])
        self.assertNotIn('analysis', result)
        self.assertEqual(json.loads(self.output.read_text(encoding='utf8')), result)

    def test_merge_preserves_all_attempt_charges_including_superseded_unknown(self):
        result = merge(self.base_path, self.subset_path, self.output)
        self.assertEqual(result['totals'], dict(inference_cost='1.5', evaluation_cost='0.6', total_cost='2.1'))
        self.assertEqual(result['superseded_records'], [dict(row, source=str(self.base_path.resolve()))
                                                      for row in self.base['records'][2:]])
        all_rows = result['records'] + result['superseded_records']
        self.assertEqual(sum(Decimal(a['cost']) for r in all_rows for a in r['attempts']), Decimal('2.1'))
        self.assertEqual(next(a for r in all_rows for a in r['attempts'] if a['attempt_id'] == 'old-a')['state'], 'unknown')

    def test_merge_never_changes_either_input(self):
        before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.base_path, self.subset_path)]
        merge(self.base_path, self.subset_path, self.output)
        after = [(path.read_bytes(), path.stat().st_mtime_ns) for path in (self.base_path, self.subset_path)]
        self.assertEqual(before, after)

    def test_merge_refuses_existing_output_including_input_paths(self):
        self.output.write_bytes(b'preserve existing output')
        for path in (self.output, self.base_path, self.subset_path):
            before = path.read_bytes()
            with self.subTest(path=path), self.assertRaises(FileExistsError):
                merge(self.base_path, self.subset_path, path)
            self.assertEqual(path.read_bytes(), before)

    def test_merge_rejects_incompatible_metadata_and_missing_attempts(self):
        for field, value in (('seed', 99), ('split', 'held-out'), ('data_source', 'real-unverified')):
            changed = dict(self.subset, **{field: value})
            with self.subTest(field=field), self.assertRaises(ValueError):
                merge(self.base_path, self.write('bad.json', changed), self.output)
            self.assertFalse(self.output.exists())
        changed = deepcopy(self.subset)
        del changed['records'][0]['attempts']
        with self.assertRaises(ValueError):
            merge(self.base_path, self.write('bad.json', changed), self.output)
        self.assertFalse(self.output.exists())

    def test_merge_rejects_empty_unknown_prompts_and_duplicate_charges(self):
        for rows in ([], [dict(self.subset['records'][0], prompt_id='absent')], self.base['records'][2:]):
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                merge(self.base_path, self.write('bad.json', dict(self.subset, records=rows)), self.output)
            self.assertFalse(self.output.exists())


if __name__ == '__main__':
    unittest.main()
