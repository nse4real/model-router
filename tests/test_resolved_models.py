"""Provider response identities, exercised with stubs and local storage only."""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from api.contracts import Request, RouterError
from benchmark.recover import recover
from benchmark.results import write_results
from benchmark.run import run_conditions
from fallback.providers import LiteLLMProvider, MockProvider, Output, ProviderError
from monitoring.ledger import SQLiteLedger
from monitoring.quality import Quality
from routing.config import Config
from routing.service import Router


def request():
    return Request.parse({"messages": [{"role": "user", "content": "Hello"}]})


class ResolvedModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_matched_temperature_reaches_both_provider_adapters_unchanged(self):
        config = Config()
        req = Request.parse({"messages": [{"role": "user", "content": "Hello"}],
                             "temperature": 1})
        for alias in ("cheap", "mid"):
            with self.subTest(alias=alias):
                calls = []

                async def completion(**kwargs):
                    calls.append(kwargs)
                    return SimpleNamespace(model=kwargs["model"],
                        choices=[SimpleNamespace(message=SimpleNamespace(content="stub"), finish_reason="stop")],
                        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3))

                model = dict(config.model(alias))
                # Request settings must override conflicting provider configuration.
                model["litellm"] = dict(model.get("litellm", {}), temperature=0)
                with patch.dict("os.environ", {"ROUTER_CONFIRM_REAL_SPEND": "YES"}), patch.dict(
                        "sys.modules", {"litellm": SimpleNamespace(acompletion=completion)}):
                    result = await LiteLLMProvider(enabled=True).complete(model, req, 1)
                self.assertEqual(len(calls), 1)
                self.assertEqual(calls[0]["model"], model["id"])
                self.assertEqual(calls[0]["temperature"], req.temperature)
                self.assertEqual(calls[0]["temperature"], 1)
                self.assertEqual(result.content, "stub")
                self.assertEqual((result.input_tokens, result.output_tokens), (2, 3))

    async def test_provider_response_snapshot_keeps_requested_alias_and_prices(self):
        config = Config()
        for alias, snapshot in (("cheap", "claude-haiku-5-5-stub-snapshot"),
                                ("mid", "gpt-4.1-mini-stub-snapshot")):
            with self.subTest(alias=alias):
                calls = []

                async def completion(**kwargs):
                    calls.append(kwargs["model"])
                    return SimpleNamespace(model=snapshot,
                        choices=[SimpleNamespace(message=SimpleNamespace(content="stub"), finish_reason="stop")],
                        usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3))

                ledger = SQLiteLedger()
                self.addCleanup(ledger.close)
                with patch.dict("os.environ", {"ROUTER_CONFIRM_REAL_SPEND": "YES"}), patch.dict(
                        "sys.modules", {"litellm": SimpleNamespace(acompletion=completion)}):
                    router = Router(ledger, config=config, provider=LiteLLMProvider(enabled=True))
                    response = await router.route(request(), use_cache=False, force_model=alias, run_id="run")
                attempt = ledger.attempts()[0]
                self.assertEqual(calls, [config.model(alias)["id"]])
                self.assertEqual(attempt["model"], config.model(alias)["id"])
                self.assertEqual(attempt["resolved_model"], snapshot)
                self.assertEqual(response["model"], attempt["model"])
                self.assertEqual(response["resolved_model"], snapshot)
                self.assertAlmostEqual(float(attempt["cost"]), float(config.cost(alias, 2, 3)))
                with TemporaryDirectory() as directory:
                    result = write_results(Path(directory) / "stub.json", ledger, "run", {}, [response])
                record = result["records"][0]
                self.assertEqual(record["requested_model"], config.model(alias)["id"])
                self.assertEqual(record["resolved_model"], snapshot)
                self.assertEqual(record["attempts"][0]["resolved_model"], snapshot)
                self.assertEqual(result["resolved_model_ids"], [snapshot])

    async def test_snapshot_survives_usage_settlement_failure(self):
        config = Config()
        ledger = SQLiteLedger()
        self.addCleanup(ledger.close)
        provider = MockProvider({config.model("cheap")["id"]: [
            Output("answer", 2, 3, resolved_model="served-snapshot")]})
        with TemporaryDirectory() as directory:
            from monitoring.settlement import SettlementJournal
            router = Router(ledger, config=config, provider=provider,
                            settlement_journal=SettlementJournal(directory + "/responses.sqlite"))
            with patch.object(ledger, "settle", side_effect=RuntimeError("storage write failed")):
                response = await router.route(request(), use_cache=False)
        self.assertEqual(response["settlement_status"], "unknown")
        self.assertEqual(response["content"], "answer")
        attempt = ledger.attempts()[0]
        self.assertEqual(attempt["resolved_model"], "served-snapshot")
        self.assertEqual(attempt["state"], "unknown")

    async def test_missing_response_model_is_unknown(self):
        async def completion(**kwargs):
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="stub"),
                                                            finish_reason="stop")],
                                   usage=SimpleNamespace(prompt_tokens=2, completion_tokens=3))

        ledger = SQLiteLedger()
        self.addCleanup(ledger.close)
        with patch.dict("os.environ", {"ROUTER_CONFIRM_REAL_SPEND": "YES"}), patch.dict(
                "sys.modules", {"litellm": SimpleNamespace(acompletion=completion)}):
            response = await Router(ledger, provider=LiteLLMProvider(enabled=True)).route(
                request(), use_cache=False)
        self.assertIsNone(response["resolved_model"])
        self.assertIsNone(ledger.attempts()[0]["resolved_model"])

    async def test_rejected_response_and_no_response_are_distinct(self):
        config = Config()
        for first in (Output("", 2, 1, resolved_model="rejected-snapshot"), ProviderError(400)):
            with self.subTest(first=type(first).__name__):
                ledger = SQLiteLedger()
                self.addCleanup(ledger.close)
                provider = MockProvider({config.model("cheap")["id"]: [first]})
                await Router(ledger, config=config, provider=provider).route(request(), use_cache=False)
                attempt = ledger.attempts()[0]
                self.assertEqual(attempt["model"], config.model("cheap")["id"])
                self.assertEqual(attempt["resolved_model"],
                                 "rejected-snapshot" if isinstance(first, Output) else None)

    async def test_judge_snapshot_survives_invalid_score(self):
        config = Config()
        for content, state in (("{\"score\":4}", "completed"), ("invalid-json", "quality_unknown")):
            with self.subTest(content=content):
                ledger = SQLiteLedger()
                self.addCleanup(ledger.close)
                ledger.begin("parent")
                ledger.job("judge", "parent")
                provider = MockProvider({config.model("mid")["id"]: [
                    Output(content, 2, 3, resolved_model="judge-snapshot")]})
                quality = Quality(ledger, config, provider)
                await quality.process(("judge", "parent", "simple", request(), Output("answer", 1, 1)))
                attempt = ledger.attempts()[0]
                self.assertEqual(attempt["model"], config.model("mid")["id"])
                self.assertEqual(attempt["resolved_model"], "judge-snapshot")
                self.assertEqual(attempt["state"], "settled")
                with ledger.transaction() as db:
                    self.assertEqual(db.execute("SELECT state FROM judge_jobs").fetchone()["state"], state)

    async def test_results_cache_judge_summary_and_recovery(self):
        config = Config()
        with TemporaryDirectory() as directory:
            path = Path(directory) / "run.json"
            ledger = SQLiteLedger(path.with_suffix(".sqlite"))
            provider = MockProvider({m["id"]: [Output("answer", 2, 3, resolved_model=alias + "-snapshot")
                                              for _ in range(4)] for alias, m in config.models.items()})
            router = Router(ledger, config=config, provider=provider)
            run_id, records = await run_conditions(router, [{"id": "fixture", "prompt": "Hello"}],
                                                   metadata={"storage": "test-double"})
            # Another run must not contaminate this run's model summary.
            ledger.begin("other", "other-run")
            ledger.reserve("other-attempt", "other", "other-alias", "mock", "inference", "v", "0")
            ledger.resolve("other-attempt", "other-snapshot")
            ledger.settle("other-attempt", "0", 0, 0)
            parent = records[0]["request_id"]
            ledger.job("judge", parent)
            quality = Quality(ledger, config, MockProvider({config.model("mid")["id"]: [
                Output('{"score":4}', 2, 3, resolved_model="judge-snapshot")]}))
            await quality.process(("judge", parent, "simple", request(), Output("answer", 1, 1)))
            result = write_results(path, ledger, run_id, {"storage": "test-double"}, records)
            self.assertTrue(result["final"])
            self.assertEqual(result["resolved_model_ids"], ["cheap-snapshot", "frontier-snapshot", "judge-snapshot"])
            self.assertEqual(sum(r["calls"] for r in result["model_resolutions"]), 4)
            self.assertEqual(json.loads(path.read_text())["model_resolutions"], result["model_resolutions"])
            warm = next(r for r in result["records"] if r["condition"] == "C-warm")
            self.assertTrue(warm["cache_hit"])
            self.assertEqual(warm["requested_model"], config.model("cheap")["id"])
            self.assertEqual(warm["resolved_model"], "cheap-snapshot")
            self.assertEqual(warm["attempts"], [])
            judged = next(r for r in result["records"] if r["request_id"] == parent)
            self.assertEqual({a["resolved_model"] for a in judged["attempts"]},
                             {judged["resolved_model"], "judge-snapshot"})
            ledger.close()
            recovered = recover(path)
            self.assertEqual(recovered["resolved_model_ids"], result["resolved_model_ids"])
            self.assertEqual(recovered["model_resolutions"], result["model_resolutions"])
            for original, restored in zip(result["records"], recovered["records"]):
                for field in ("requested_model", "resolved_model", "attempts"):
                    self.assertEqual(restored[field], original[field])

    async def test_export_retains_all_fallback_attempts(self):
        config = Config()
        ledger = SQLiteLedger()
        self.addCleanup(ledger.close)
        provider = MockProvider({config.model("cheap")["id"]: [
            Output("", 2, 1, resolved_model="rejected-snapshot")]})
        req = request()
        response = await Router(ledger, config=config, provider=provider).route(req, use_cache=False, run_id="r")
        with TemporaryDirectory() as directory:
            result = write_results(Path(directory) / "run.json", ledger, "r", {}, [
                {"request_id": req.request_id, "model": response["model"],
                 "resolved_model": response["resolved_model"]}])
        attempts = result["records"][0]["attempts"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["requested_model"], config.model("cheap")["id"])
        self.assertEqual(attempts[0]["resolved_model"], "rejected-snapshot")
        self.assertEqual(attempts[1]["requested_model"], response["model"])


class SnapshotMigrationTests(unittest.TestCase):
    def test_existing_sqlite_ledger_migrates_without_inventing_snapshot(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite"
            schema = Path("monitoring/schema.sql").read_text().replace("resolved_model TEXT, ", "")
            with closing(sqlite3.connect(path)) as db:
                db.executescript(schema)
                db.execute("INSERT INTO requests(request_id,created_at) VALUES('r',0)")
                db.execute("INSERT INTO attempts(attempt_id,parent_request_id,model,provider,purpose,"
                           "price_version,reserved,cost,state,cost_status,created_at) "
                           "VALUES('a','r','old-alias','mock','inference','v',0,0,'settled','measured',0)")
                db.commit()
            for index in range(2):
                ledger = SQLiteLedger(path)
                try:
                    attempt = ledger.attempts()[0]
                    self.assertEqual(attempt["model"], "old-alias")
                    self.assertEqual(attempt["state"], "settled")
                    self.assertEqual(attempt["price_version"], "v")
                    self.assertEqual(attempt["resolved_model"], None if index == 0 else "stub-snapshot")
                    ledger.resolve("a", "stub-snapshot")
                finally:
                    ledger.close()
