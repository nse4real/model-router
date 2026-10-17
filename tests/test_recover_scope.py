"""Run-isolation regressions using local ledgers and synthetic journal responses."""
from decimal import Decimal
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
import unittest
from unittest.mock import patch

from benchmark.recover import recover
from monitoring.ledger import PostgresLedger, SQLiteLedger
from monitoring.settlement import SettlementJournal


class RecoverScopeContract:
    def setUp(self):
        directory = self.enterContext(TemporaryDirectory())
        self.path = Path(directory, 'run-a.json')
        self.enterContext(patch.dict(os.environ, LEDGER_RESPONSE_STORE=str(
            Path(directory, 'journal.sqlite'))))
        self.journal = SettlementJournal()
        self.ledger = self.create_ledger()

    def add_run(self, run, state='dispatched'):
        self.ledger.save_run(run, {'storage': self.storage})
        self.ledger.begin(run, run)
        self.ledger.save_record(run, dict(request_id=run, condition='A', prompt_id='fixture',
                                         status='pending', completed=False, cost='.1'))
        self.add_attempt(run, run, state=state)

    def add_attempt(self, attempt, parent, purpose='inference', state='dispatched'):
        self.ledger.reserve(attempt, parent, 'fixture-model', 'mock', purpose, 'v1', '.1')
        if state == 'dispatched':
            self.ledger.dispatch(attempt)

    def recover_a(self):
        self.ledger.clock = time.time
        with patch('benchmark.recover.PostgresLedger', return_value=self.ledger):
            return recover(self.path, run_id='run-a', storage=self.storage)

    def foreign_rows(self):
        with self.ledger.transaction() as db:
            jobs = [dict(row) for row in db.execute(
                "SELECT * FROM judge_jobs WHERE parent_request_id='run-b'").fetchall()]
        return self.ledger.attempts('run-b'), self.ledger.requests('run-b'), jobs

    def test_recover_run_a_leaves_run_b_dispatched_and_cost_unchanged(self):
        self.add_run('run-a')
        self.add_run('run-b')
        self.add_attempt('reserved-a', 'run-a', state='reserved')
        self.add_attempt('reserved-b', 'run-b', state='reserved')
        before = self.foreign_rows()
        result = self.recover_a()
        self.assertEqual(self.foreign_rows(), before)
        foreign = self.ledger.attempts('run-b')[0]
        self.assertEqual(foreign['state'], 'dispatched')
        self.assertEqual(Decimal(str(foreign['cost'])), Decimal('.1'))
        self.assertTrue(result['final'])
        self.assertEqual(Decimal(result['totals']['total_cost']), Decimal('.2'))
        for attempt in self.ledger.attempts('run-a'):
            self.assertEqual((attempt['state'], attempt['cost_status']), ('unknown', 'unknown'))
            self.assertEqual(attempt['cost'], attempt['reserved'])
        self.assertFalse(self.ledger.barrier('run-b'))
        self.assertEqual(len(self.ledger.attempts()), 4)
        self.assertTrue(self.recover_a()['final'])
        self.assertEqual(self.foreign_rows(), before)

    def test_judge_recovery_only_marks_target_run_quality_unknown(self):
        for run in ('run-a', 'run-b'):
            self.add_run(run)
            self.ledger.job('judge-' + run, run)
            self.assertTrue(self.ledger.claim_and_reserve(
                'judge-' + run, run, 'fixture-model', 'mock', 'v1', '.1'))
            self.ledger.dispatch('judge-' + run)
            self.ledger.job('queued-' + run, run)
        # A fresh queued job also exercises the benchmark's final job cleanup.
        self.ledger.clock = time.time
        self.ledger.job('fresh-b', 'run-b')
        before = self.foreign_rows()
        self.ledger.job('fresh-a', 'run-a')
        result = self.recover_a()
        self.assertEqual(self.foreign_rows(), before)
        with self.ledger.transaction() as db:
            states = [r['state'] for r in db.execute(
                "SELECT state FROM judge_jobs WHERE parent_request_id='run-a'").fetchall()]
        self.assertEqual(states, ['quality_unknown'] * 3)
        self.assertTrue(result['final'])
        self.assertEqual(Decimal(result['totals']['total_cost']), Decimal('.2'))

    def test_journal_replay_is_scoped_and_preserves_unknown_charges(self):
        for run in ('run-a', 'run-b'):
            self.add_run(run)
            self.add_attempt('unknown-' + run, run)
            for attempt, status in ((run, 'retrying'), ('unknown-' + run, 'unknown')):
                response = dict(request_id=run, content='fixture answer', model='fixture-model',
                                resolved_model='fixture-snapshot', tier='simple', stop_reason='stop')
                self.journal.save(attempt, dict(resolved_model='fixture-snapshot', actual='.01',
                                               input_tokens=1, output_tokens=1, error=None,
                                               rejected=False, response=response), status)
        before = self.foreign_rows()
        journal_before = {e['attempt_id']: e for e in self.journal.entries()}
        result = self.recover_a()
        self.assertEqual(self.foreign_rows(), before)
        entries = {e['attempt_id']: e for e in self.journal.entries()}
        for attempt in ('run-b', 'unknown-run-b'):
            self.assertEqual(entries[attempt], journal_before[attempt])
        self.assertTrue(entries['run-a']['synced'])
        self.assertTrue(entries['unknown-run-a']['synced'])
        attempts = {a['attempt_id']: a for a in self.ledger.attempts('run-a')}
        self.assertEqual(attempts['run-a']['state'], 'settled')
        self.assertEqual(attempts['unknown-run-a']['state'], 'unknown')
        self.assertEqual(Decimal(str(attempts['unknown-run-a']['cost'])), Decimal('.1'))
        self.assertEqual(Decimal(result['totals']['total_cost']), Decimal('.11'))
        self.assertEqual(result['records'][0]['content'], 'fixture answer')
        self.assertTrue(result['final'])
        self.assertEqual(len(self.ledger.attempts()), 4)
        self.assertEqual(Decimal(self.recover_a()['totals']['total_cost']), Decimal('.11'))
        self.assertEqual(self.foreign_rows(), before)

    def test_journal_payload_cannot_claim_another_runs_request(self):
        self.add_run('run-a')
        self.add_run('run-b')
        for attempt, parent in (('run-a', 'run-b'), ('run-b', 'run-a'), ('absent', 'run-a')):
            response = dict(request_id=parent, content='foreign fixture', model='fixture-model',
                            resolved_model='fixture-snapshot', tier='simple', stop_reason='stop')
            self.journal.save(attempt, dict(resolved_model='fixture-snapshot', actual='.01',
                                           input_tokens=1, output_tokens=1, error=None,
                                           rejected=False, response=response))
        before = self.foreign_rows()
        entries = self.journal.entries()
        result = self.recover_a()
        self.assertEqual(self.foreign_rows(), before)
        self.assertEqual(self.journal.entries(), entries)
        self.assertEqual(self.ledger.attempts('run-a')[0]['state'], 'unknown')
        self.assertEqual(Decimal(result['totals']['total_cost']), Decimal('.1'))
        self.assertNotIn('content', result['records'][0])

    def test_live_target_attempt_keeps_final_barrier_closed(self):
        self.add_run('run-b')
        self.ledger.clock = time.time
        self.add_run('run-a')
        self.ledger.job('run-a', 'run-a', 'running')
        before = self.foreign_rows()
        result = self.recover_a()
        self.assertFalse(result['final'])
        self.assertNotIn('totals', result)
        self.assertEqual(self.ledger.attempts('run-a')[0]['state'], 'dispatched')
        with self.ledger.transaction() as db:
            self.assertEqual(db.execute(
                "SELECT state FROM judge_jobs WHERE attempt_id='run-a'").fetchone()['state'], 'running')
        self.assertEqual(self.foreign_rows(), before)


class SQLiteRecoverScopeTests(RecoverScopeContract, unittest.TestCase):
    storage = 'test-double'

    def create_ledger(self):
        ledger = SQLiteLedger(self.path.with_suffix('.sqlite'), clock=lambda: 0)
        self.addCleanup(ledger.close)
        return ledger


@unittest.skipUnless(os.environ.get('ROUTER_TEST_POSTGRES_DSN'), 'Local Postgres DSN not supplied')
class PostgresRecoverScopeTests(RecoverScopeContract, unittest.TestCase):
    storage = 'postgres'

    def create_ledger(self):
        from tests.test_services import isolated_postgres
        dsn = self.enterContext(isolated_postgres())
        return PostgresLedger(dsn, clock=lambda: 0)
