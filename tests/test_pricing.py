"""User-supplied tier prices, exercised without provider/network access."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal
import json
import os
from tempfile import TemporaryDirectory
from threading import Barrier
import unittest
from unittest.mock import patch

from api.contracts import Request, RouterError
from fallback.providers import MockProvider, Output, ProviderError
from monitoring.ledger import PostgresLedger, SQLiteLedger, SpendExceeded, StorageUnavailable
from routing.config import Config
from routing.service import Router


def estimated_request(tokens, **kwargs):
    # Synthetic internal requests bypass the public API's 32 KB limit solely to
    # exercise pricing boundaries. One user message adds 4 + 16 + 64 tokens.
    return Request(messages=({"role": "user", "content": "x" * (tokens - 84)},),
                   max_tokens=100, **kwargs)


class PricingTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_replacement_model_and_dated_version(self):
        self.assertEqual(self.config.model("cheap")["id"],
                         "anthropic/claude-haiku-5-5")
        self.assertEqual(self.config.data["price_version"], "user-supplied-2026-10-17-v2")
        self.assertIsInstance(self.config.version, str)
        self.assertTrue(self.config.version.startswith(self.config.data["price_version"] + ":"))

    def test_exactly_100000_uses_up_to_tier(self):
        self.assertEqual(self.config.cost("cheap", 100000, 100), Decimal("0.01005"))

    def test_100001_uses_above_tier_for_entire_request(self):
        self.assertEqual(self.config.cost("cheap", 100001, 100), Decimal("0.0502505"))

    def test_input_alone_selects_tier(self):
        self.assertEqual(self.config.cost("cheap", 1, 100001), Decimal("0.0500006"))
        self.assertEqual(self.config.cost("cheap", 0, 0), Decimal(0))

    def test_tier_is_per_request_not_cumulative(self):
        for _ in range(2):
            self.assertEqual(self.config.cost("cheap", 60000, 100), Decimal(".00605"))

    def test_reservation_10_percent_guard_and_boundaries(self):
        for tokens, input_rate, output_rate in (
                (89999, ".10", ".50"), (90000, ".50", "2.50"),
                (99999, ".50", "2.50"), (100000, ".50", "2.50"),
                (100001, ".50", "2.50")):
            with self.subTest(tokens=tokens):
                expected = (tokens * Decimal(input_rate) + 100 * Decimal(output_rate)) / 1000000
                self.assertEqual(self.config.reserve_cost("cheap", estimated_request(tokens)), expected)
                self.assertGreaterEqual(expected, self.config.cost("cheap", tokens, 100))

    def test_reservation_uses_utf8_framing_and_output_limit(self):
        request = Request(messages=({"role": "system", "content": "\u00e9"},
                                    {"role": "user", "content": "hello"}), max_tokens=2048)
        # 2 + 6 + 16 + 5 + 4 + 16 + 64 input tokens.
        self.assertEqual(self.config.reserve_cost("cheap", request),
                         (Decimal(113) * Decimal(".10") + 2048 * Decimal(".50")) / 1000000)

    def test_flat_models_unchanged(self):
        for alias, expected in (("mid", ".04016"), ("frontier", ".251"), ("local", "0")):
            with self.subTest(alias=alias):
                self.assertEqual(self.config.cost(alias, 100000, 100), Decimal(expected))
                self.assertEqual(self.config.reserve_cost(alias, estimated_request(100000)),
                                 Decimal(expected))

    def test_flat_prices_still_reject_missing_or_invalid_values(self):
        for field in ("input_per_million", "output_per_million"):
            for value in (None, "", "NaN", "Infinity", "-1", [], True):
                with self.subTest(field=field, value=value):
                    data = deepcopy(self.config.data)
                    data["models"]["mid"][field] = value
                    with self.assertRaises(RouterError) as caught:
                        Config(data=data)
                    self.assertEqual(caught.exception.status, 422)
            data = deepcopy(self.config.data)
            del data["models"]["mid"][field]
            with self.assertRaises(RouterError) as caught:
                Config(data=data)
            self.assertEqual(caught.exception.status, 422)

    def test_malformed_tiers_raise_same_422_price_error(self):
        low, high = deepcopy(self.config.models["cheap"]["tiers"])
        malformed = [None, [], {}, "tiers", [None], [low],
                     [high, low], [high, high], [low, low, high]]
        malformed += [[{**low, "max_input_tokens": value}, high]
                      for value in (0, -1, True, "100000", 100000.0)]
        missing_boundary = dict(low)
        del missing_boundary["max_input_tokens"]
        malformed.append([missing_boundary, high])
        for field in ("input_per_million", "output_per_million"):
            for index in (0, 1):
                missing = [deepcopy(low), deepcopy(high)]
                del missing[index][field]
                malformed.append(missing)
                for value in (None, "bad", "NaN", "Infinity", "-1", [], True):
                    tiers = [deepcopy(low), deepcopy(high)]
                    tiers[index][field] = value
                    malformed.append(tiers)
        for tiers in malformed:
            with self.subTest(tiers=tiers):
                data = deepcopy(self.config.data)
                data["models"]["cheap"]["tiers"] = tiers
                # A bad tier definition cannot silently fall back to flat prices.
                data["models"]["cheap"].update(input_per_million=".10", output_per_million=".50")
                with self.assertRaises(RouterError) as caught:
                    Config(data=data)
                self.assertEqual(caught.exception.status, 422)
                self.assertEqual(caught.exception.message, "Missing or invalid model price")

    def test_missing_all_prices_raises_422(self):
        data = deepcopy(self.config.data)
        del data["models"]["cheap"]["tiers"]
        with self.assertRaises(RouterError) as caught:
            Config(data=data)
        self.assertEqual(caught.exception.status, 422)

    def test_price_changes_invalidate_cache_even_without_manual_version_bump(self):
        request = estimated_request(100000)
        router = Router(SQLiteLedger(), config=self.config)
        original = router.cache_key(request, self.config.model("cheap"))
        version = self.config.version
        self.assertEqual(Config(data=self.config.data).version, version)
        for index, field, value in ((0, "max_input_tokens", 99999),
                                    (0, "input_per_million", ".11"),
                                    (1, "output_per_million", "2.60")):
            with self.subTest(field=field):
                cfg = Config(data=self.config.data)
                cfg.models["cheap"]["tiers"][index][field] = value
                self.assertNotEqual(cfg.version, version)
                router.config = cfg
                self.assertNotEqual(router.cache_key(request, cfg.model("cheap")), original)
        cfg = Config(data=self.config.data)
        cfg.models["mid"]["input_per_million"] = ".41"
        self.assertNotEqual(cfg.version, version)
        cfg = Config(data=self.config.data)
        cfg.data["price_version"] = "new-dated-version"
        self.assertNotEqual(cfg.version, version)


class PricingSettlementTests(unittest.IsolatedAsyncioTestCase):
    async def test_settlement_uses_actual_tier_and_records_effective_version(self):
        # High-rate reservation, with actual usage on either side of the boundary.
        for estimate, actual, expected in ((99999, 100000 - 1, ".0100499"),
                                           (110000, 100000, ".01005"),
                                           (110000, 100001, ".0502505")):
            with self.subTest(estimate=estimate, actual=actual):
                cfg = Config()
                model = cfg.model("cheap")["id"]
                provider = MockProvider({model: [Output("fixture", actual, 100)]})
                ledger = SQLiteLedger()
                router = Router(ledger, config=cfg, provider=provider)
                result = await router.route(estimated_request(estimate), force_model="cheap",
                                            use_cache=False)
                attempt = ledger.attempts()[0]
                self.assertEqual(Decimal(str(attempt["cost"])), Decimal(expected))
                self.assertGreater(Decimal(str(attempt["reserved"])), Decimal(expected))
                self.assertEqual(attempt["state"], "settled")
                self.assertEqual(attempt["input_tokens"], actual)
                self.assertEqual(attempt["price_version"], cfg.version)
                self.assertEqual(Decimal(str(result["cost"])), Decimal(expected))
                self.assertEqual(Decimal(str(ledger.requests()[0]["inference_cost"])), Decimal(expected))
                self.assertEqual(provider.calls, [model])

    async def test_error_usage_settles_actual_tier(self):
        cfg = Config()
        model = cfg.model("cheap")["id"]
        provider = MockProvider({model: [ProviderError(403, usage=(100000, 100))]})
        ledger = SQLiteLedger()
        with self.assertRaises(RouterError) as caught:
            await Router(ledger, config=cfg, provider=provider).route(
                estimated_request(110000), force_model="cheap", use_cache=False)
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(Decimal(str(ledger.attempts()[0]["cost"])), Decimal(".01005"))
        self.assertEqual(ledger.attempts()[0]["state"], "settled")

    async def test_guarded_reservation_controls_request_budget_before_dispatch(self):
        cfg = Config()
        request = estimated_request(99999, max_cost_usd=Decimal(".02"))
        provider = MockProvider()
        with self.assertRaises(RouterError) as caught:
            await Router(SQLiteLedger(), config=cfg, provider=provider).route(
                request, force_model="cheap", use_cache=False)
        self.assertEqual(caught.exception.status, 402)
        self.assertEqual(provider.calls, [])

    async def test_price_error_before_dispatch(self):
        cfg = Config()
        cfg.models["cheap"]["tiers"] = []
        provider = MockProvider()
        with self.assertRaises(RouterError) as caught:
            await Router(SQLiteLedger(), config=cfg, provider=provider).route(
                estimated_request(1000), force_model="cheap", use_cache=False)
        self.assertEqual(caught.exception.status, 422)
        self.assertEqual(provider.calls, [])


class RejectionRoutingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.config = Config()
        self.model = self.config.model("cheap")["id"]
        self.request = Request.parse({"messages": [{"role": "user", "content": "hello"}],
                                      "max_tokens": 8})

    def ledger(self, **kwargs):
        ledger = SQLiteLedger(**kwargs)
        self.addCleanup(ledger.close)
        return ledger

    async def test_http_400_settles_zero_and_restores_cap_headroom(self):
        worst = self.config.reserve_cost("cheap", self.request)
        for usage in (None, (2, 3)):
            with self.subTest(usage=usage):
                ledger = self.ledger(cap=worst)
                provider = MockProvider({self.model: [ProviderError(
                    400, "bad request api_key=fixture-secret", usage=usage)]})
                with self.assertRaises(RouterError) as caught:
                    await Router(ledger, config=self.config, provider=provider).route(
                        self.request, use_cache=False, force_model="cheap")
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(provider.calls, [self.model])
                attempt = ledger.attempts()[0]
                self.assertEqual(attempt["state"], "settled")
                self.assertEqual(attempt["cost_status"], "rejected")
                self.assertEqual(Decimal(str(attempt["cost"])), 0)
                self.assertEqual((attempt["input_tokens"], attempt["output_tokens"]), (0, 0))
                self.assertEqual(attempt["model"], self.model)
                self.assertIsNone(attempt["resolved_model"])
                error = json.loads(attempt["error"])
                self.assertEqual(error["status"], 400)
                self.assertIn("bad request", error["raw"])
                self.assertNotIn("fixture-secret", error["raw"])
                self.assertEqual(Decimal(str(ledger.requests()[0]["inference_cost"])), 0)
                self.assertTrue(ledger.barrier())
                ledger.reserve("replacement", self.request.request_id, self.model, "mock",
                               "inference", self.config.version, worst)
                with self.assertRaises(SpendExceeded):
                    ledger.reserve("over-cap", self.request.request_id, self.model, "mock",
                                   "inference", self.config.version, worst)

    async def test_mixed_400_and_200_reconcile_and_reuse_request_budget(self):
        # Equal fixture prices make each fallback reservation fit the same budget.
        for model in self.config.models.values():
            model.pop("tiers", None)
            model.update(input_per_million="1", output_per_million="1")
        self.config.chains["simple"] = ["cheap", "mid"]
        worst = self.config.reserve_cost("cheap", self.request)
        self.request = replace(self.request, max_cost_usd=worst)
        ledger = self.ledger(cap=worst)
        mid = self.config.model("mid")["id"]
        provider = MockProvider({self.model: [ProviderError(400)],
                                 mid: [Output("answer", 2, 3, resolved_model="served-snapshot")]})
        response = await Router(ledger, config=self.config, provider=provider).route(
            self.request, use_cache=False, run_id="mixed")
        attempts = ledger.attempts()
        actual = self.config.cost("mid", 2, 3)
        self.assertEqual(provider.calls, [self.model, mid])
        self.assertEqual([a["cost_status"] for a in attempts], ["rejected", "measured"])
        self.assertEqual(attempts[1]["resolved_model"], "served-snapshot")
        self.assertEqual(sum(Decimal(str(a["cost"])) for a in attempts), actual)
        self.assertEqual(Decimal(str(response["cost"])), actual)
        self.assertEqual(response["fallback_count"], 1)
        request = ledger.requests()[0]
        self.assertEqual(Decimal(str(request["inference_cost"])) +
                         Decimal(str(request["evaluation_cost"])), actual)
        self.assertTrue(ledger.barrier("mixed"))

    async def test_retryable_and_unknown_failures_keep_full_reservations(self):
        worst = self.config.reserve_cost("cheap", self.request)
        for error in (ProviderError(429), ProviderError(500), TimeoutError("fixture timeout"),
                      ConnectionError("fixture connection"), RuntimeError("unknown outcome")):
            with self.subTest(error=repr(error)):
                ledger = self.ledger(clock=lambda: 0)
                provider = MockProvider({self.model: [error, error]})
                with self.assertRaises(RouterError) as caught:
                    await Router(ledger, config=self.config, provider=provider).route(
                        self.request, use_cache=False, force_model="cheap")
                self.assertEqual(caught.exception.status, 503)
                self.assertEqual(provider.calls, [self.model, self.model])
                for attempt in ledger.attempts():
                    self.assertEqual(attempt["state"], "dispatched")
                    self.assertEqual(attempt["cost_status"], "estimated")
                    self.assertEqual(Decimal(str(attempt["cost"])), worst)
                ledger.clock = lambda: 120
                self.assertEqual(ledger.recover(), 2)
                for attempt in ledger.attempts():
                    self.assertEqual(attempt["state"], "unknown")
                    self.assertEqual(attempt["cost_status"], "unknown")
                    self.assertEqual(Decimal(str(attempt["cost"])), worst)
                self.assertEqual(Decimal(str(ledger.requests()[0]["inference_cost"])), 2 * worst)
                self.assertTrue(ledger.barrier())

    async def test_successful_response_rejected_locally_retains_measured_usage(self):
        for output in (Output("", 2, 3, resolved_model="snapshot"),
                       Output("filtered", 2, 3, stop_reason="content_filter", resolved_model="snapshot")):
            with self.subTest(output=output):
                ledger = self.ledger()
                provider = MockProvider({self.model: [output]})
                with self.assertRaises(RouterError) as caught:
                    await Router(ledger, config=self.config, provider=provider).route(
                        self.request, use_cache=False, force_model="cheap")
                self.assertEqual(caught.exception.status, 503)
                attempt = ledger.attempts()[0]
                self.assertEqual(attempt["cost_status"], "measured")
                self.assertEqual(Decimal(str(attempt["cost"])), self.config.cost("cheap", 2, 3))
                self.assertEqual((attempt["input_tokens"], attempt["output_tokens"]), (2, 3))
                self.assertEqual(attempt["resolved_model"], "snapshot")

    async def test_failed_rejection_settlement_retains_reservation(self):
        ledger = self.ledger()
        provider = MockProvider({self.model: [ProviderError(400)]})
        with patch.object(ledger, "settle", side_effect=StorageUnavailable("fixture unavailable")):
            with self.assertRaises(RouterError) as caught:
                await Router(ledger, config=self.config, provider=provider).route(
                    self.request, use_cache=False, force_model="cheap")
        self.assertEqual(caught.exception.status, 503)
        attempt = ledger.attempts()[0]
        self.assertEqual(attempt["state"], "dispatched")
        self.assertEqual(Decimal(str(attempt["cost"])), Decimal(str(attempt["reserved"])))

    async def test_probe_lease_loss_preserves_definitive_rejection(self):
        ledger = self.ledger()
        provider = MockProvider({self.model: [ProviderError(400, "fixture rejection")]})
        router = Router(ledger, config=self.config, provider=provider)
        with patch.object(router.health, "observe", return_value=False):
            with self.assertRaises(RouterError) as caught:
                await router.route(self.request, use_cache=False, force_model="cheap")
        self.assertEqual(caught.exception.status, 503)
        attempt = ledger.attempts()[0]
        self.assertEqual(attempt["cost_status"], "rejected")
        self.assertEqual(Decimal(str(attempt["cost"])), 0)
        self.assertEqual(json.loads(attempt["error"])["status"], 400)


class RejectionLedgerTests(unittest.TestCase):
    def test_rejection_preserves_identities_and_terminal_historical_charges(self):
        ledger = SQLiteLedger(clock=lambda: 0)
        self.addCleanup(ledger.close)
        ledger.begin("r")
        for attempt in ("rejected", "historical", "unknown"):
            ledger.reserve(attempt, "r", "requested-alias", "mock", "inference", "v", "1")
            ledger.dispatch(attempt)
            ledger.resolve(attempt, "served-snapshot")
        ledger.settle("historical", ".5", 2, 3)
        before = ledger.attempts()[1]
        ledger.settle("rejected", "0", 0, 0, error={"status": 400}, rejected=True)
        ledger.clock = lambda: 120
        self.assertEqual(ledger.recover(), 1)
        unknown = ledger.attempts()[2]
        for attempt in ("historical", "unknown"):
            ledger.settle(attempt, "0", 0, 0, error={"status": 400}, rejected=True)
        ledger.settle("rejected", "1", 1, 1)  # Repeated settlement stays idempotent.
        rejected, historical, recovered = ledger.attempts()
        self.assertEqual(historical, before)
        self.assertEqual(recovered, unknown)
        self.assertEqual(rejected["model"], "requested-alias")
        self.assertEqual(rejected["resolved_model"], "served-snapshot")
        self.assertEqual(rejected["cost_status"], "rejected")
        self.assertEqual(Decimal(str(rejected["cost"])), 0)
        self.assertEqual(Decimal(str(ledger.requests()[0]["inference_cost"])), Decimal("1.5"))

    def test_rejection_marker_requires_zero_cost_and_tokens(self):
        ledger = SQLiteLedger()
        self.addCleanup(ledger.close)
        ledger.begin("r")
        ledger.reserve("a", "r", "m", "mock", "inference", "v", "1")
        for cost, inputs, outputs in (("1", 0, 0), ("0", 1, 0), ("0", 0, 1)):
            with self.subTest(cost=cost, inputs=inputs, outputs=outputs):
                with self.assertRaises(ValueError):
                    ledger.settle("a", cost, inputs, outputs, rejected=True)
        self.assertEqual(ledger.attempts()[0]["state"], "reserved")

    def concurrent_rejections(self, a, b):
        # Two ledgers contend on one durable cap, just like the admission lock tests.
        a.begin("r")
        start = Barrier(8)
        def reject(i):
            if i < 8:
                start.wait(timeout=10)
            ledger = (a, b)[i % 2]
            try:
                ledger.reserve("reject-" + str(i), "r", "m", "mock", "inference", "v", "1")
            except SpendExceeded:
                return False
            ledger.dispatch("reject-" + str(i))
            ledger.settle("reject-" + str(i), "0", 0, 0, rejected=True, error={"status": 400})
            self.assertLessEqual(sum(Decimal(str(row["cost"])) for row in a.attempts()), 3)
            return True
        with ThreadPoolExecutor(max_workers=8) as pool:
            rejected = sum(pool.map(reject, range(20)))
        self.assertGreater(rejected, 0)
        def mix(i):
            ledger = (a, b)[i % 2]
            try:
                ledger.reserve("mix-" + str(i), "r", "m", "mock", "inference", "v", "1")
            except SpendExceeded:
                return False
            ledger.dispatch("mix-" + str(i))
            if i % 3 == 0:
                ledger.settle("mix-" + str(i), "0", 0, 0, rejected=True, error={"status": 400})
            else:
                ledger.settle("mix-" + str(i), "1", 2, 3)
            self.assertLessEqual(sum(Decimal(str(row["cost"])) for row in a.attempts()), 3)
            return True
        with ThreadPoolExecutor(max_workers=8) as pool:
            admitted = sum(pool.map(mix, range(20)))
        self.assertGreaterEqual(admitted, 3)
        attempts = a.attempts()
        total = sum(Decimal(str(row["cost"])) for row in attempts)
        self.assertGreater(total, 0)
        self.assertLessEqual(total, 3)
        # Concurrent cap rejections may precede a later zero-cost settlement.
        # Fill any freed slots deterministically instead of assuming scheduling.
        for i in range(3 - int(total)):
            attempt = "fill-" + str(i)
            a.reserve(attempt, "r", "m", "mock", "inference", "v", "1")
            a.dispatch(attempt)
            a.settle(attempt, "1", 2, 3)
        attempts = a.attempts()
        total = sum(Decimal(str(row["cost"])) for row in attempts)
        self.assertEqual(total, 3)
        self.assertGreater(len(attempts), 3)  # Rejections freed capacity for new calls.
        self.assertEqual(sum(row["cost_status"] == "measured" for row in attempts), 3)
        self.assertEqual(Decimal(str(a.requests()[0]["inference_cost"])), total)
        self.assertTrue(a.barrier())

    def test_concurrent_sqlite_rejections_reuse_headroom_without_exceeding_cap(self):
        with TemporaryDirectory() as directory:
            a = SQLiteLedger(directory + "/ledger.sqlite", cap="3")
            b = SQLiteLedger(directory + "/ledger.sqlite", cap="3")
            try:
                self.concurrent_rejections(a, b)
            finally:
                a.close()
                b.close()


@unittest.skipUnless(os.environ.get("ROUTER_TEST_POSTGRES_DSN"), "Local Postgres DSN not supplied")
class RejectionPostgresTests(unittest.TestCase):
    concurrent_rejections = RejectionLedgerTests.concurrent_rejections
    def test_concurrent_postgres_rejections_reuse_headroom_without_exceeding_cap(self):
        from test_services import isolated_postgres
        with isolated_postgres() as dsn:
            self.concurrent_rejections(PostgresLedger(dsn, cap="3"), PostgresLedger(dsn, cap="3"))
