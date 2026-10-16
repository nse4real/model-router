from decimal import Decimal
import json
from pathlib import Path
import tempfile
import unittest
from benchmark.dataset import load_dataset, split
from benchmark.load_test import execute, http_json
from benchmark.preflight import estimate
from benchmark.results import analyse, totals, write_results, reproducibility
from benchmark.run import run_conditions
from benchmark.statistics import bootstrap_ci, non_inferiority
from fallback.providers import MockProvider, ProviderError
from monitoring.ledger import SQLiteLedger
from monitoring.metrics import prometheus, snapshot
from routing.config import Config
from routing.service import Router
from tests.test_stage2 import req


class DatasetTests(unittest.TestCase):
    def test_frozen_split_and_dedup(self):
        rows, manifest = load_dataset()
        self.assertEqual(len(rows), 153)
        self.assertEqual(manifest["counts"], {"dev":43,"calibration":50,"held-out":60})
        self.assertEqual(len(set(manifest["human_subset"])), 60)
        self.assertEqual(len(set(manifest["reproducibility_subset"])), 30)
        held = {r["id"] for r in rows if r["split"] == "held-out"}
        self.assertTrue(set(manifest["human_subset"]) <= held)
        self.assertTrue(set(manifest["reproducibility_subset"]) <= held)

    def test_checksum_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            from benchmark.dataset import DATA
            for name in ("manifest.json", "prompts.jsonl"):
                Path(tmp,name).write_bytes(Path(DATA,name).read_bytes())
            with Path(tmp,"prompts.jsonl").open("ab") as f:
                f.write(b" ")
            with self.assertRaises(ValueError):
                load_dataset(tmp)

    def test_preflight(self):
        result = estimate([1,1,1], 60, allocation="12", safety_factor="1")
        self.assertEqual(result["admitted"], 12)
        self.assertEqual(result["reduction"], 48)
        with self.assertRaises(ValueError):
            estimate([],60)
        with self.assertRaises(ValueError):
            estimate([-1],60)


class StatisticsTests(unittest.TestCase):
    def test_non_inferiority_and_failures(self):
        self.assertTrue(non_inferiority([5]*10,[5]*10,resamples=100)["supported"])
        self.assertFalse(non_inferiority([5]*10,[4]*10,resamples=100)["supported"])
        result = non_inferiority([5]*10,[5]*10,completed_b=[False]*10,resamples=100)
        self.assertEqual(result["mean_difference"], -4)
        self.assertEqual(result["sensitivity_completed_only"]["n"], 0)
        self.assertEqual(bootstrap_ci([1,2,3], seed=1,resamples=100),
                         bootstrap_ci([1,2,3],seed=1,resamples=100))

    def test_incomplete_denominator_and_withheld_c(self):
        records = [dict(prompt_id=str(i),condition=c,cost="2",completed=True,quality=5)
                   for i in range(60) for c in ("A","B","C-cold","C-warm")]
        result = analyse(records,60,quality_source="human",resamples=100)
        self.assertTrue(result["quality"]["B"]["supported"])
        self.assertFalse(result["quality"]["C-cold"]["supported"])
        partial = analyse(records[:-1],60,quality_source="human",resamples=100)
        self.assertFalse(partial["coverage_complete"])
        self.assertFalse(partial["headline_claim_supported"])
        self.assertEqual(partial["conditions"]["C-warm"]["cost_per_prompt"],118/60)

    def test_reproducibility_fixed_subset(self):
        records = [dict(prompt_id=str(i),condition=c,cost="2",completed=True,quality=5)
                   for i in range(30) for c in ("A","B","C-cold","C-warm")]
        result = reproducibility(records, records, [str(i) for i in range(30)], resamples=100)
        self.assertTrue(all(r["supported"] for r in result.values()))


class BenchmarkAndMetricsTests(unittest.IsolatedAsyncioTestCase):
    async def test_dev_mini_conditions_and_mock_guard(self):
        # Deliberately use only two DEV prompts. Never execute held-out in tests.
        prompts, _ = split("dev")
        ledger = SQLiteLedger()
        router = Router(ledger)
        run_id, records = await run_conditions(router, prompts[:2])
        self.assertEqual(len(records),8)
        report = analyse(records,2,resamples=100)
        self.assertIsNone(report["conditions"]["A"]["accepted"])
        self.assertFalse(report["headline_claim_supported"])
        self.assertTrue(all(r["cache_hit"] for r in records if r["condition"] == "C-warm"))
        self.assertTrue(all(not r["cache_hit"] for r in records if r["condition"] != "C-warm"))
        self.assertTrue(all(r["model"] == router.config.model("frontier")["id"]
                            for r in records if r["condition"] == "A"))
        self.assertTrue(ledger.barrier(run_id))
        self.assertEqual(Decimal(totals(ledger,run_id)["total_cost"]),
                         sum(Decimal(r["cost"]) for r in records))
        router.provider.mode = "real-unverified"
        with self.assertRaises(ValueError):
            await run_conditions(router,prompts[:1])

    async def test_failure_fixture_reconciliation_and_panels(self):
        clock = [0]
        ledger = SQLiteLedger(clock=lambda:clock[0])
        cfg = Config()
        p = MockProvider({cfg.model("cheap")["id"]: [ProviderError(400)]})
        router = Router(ledger,provider=p)
        await router.route(req("fallback fixture"))
        await router.route(req("fallback fixture"))  # cache hit after a failed primary
        ledger.begin("unknown")
        ledger.reserve("u","unknown","m","mock","inference","v1",Decimal(".01"))
        ledger.dispatch("u")
        ledger.finish("unknown",outcome="504")
        ledger.begin("judge")
        ledger.reserve("j","judge","m","mock","evaluation","v1",Decimal(".02"))
        ledger.dispatch("j")
        ledger.settle("j",Decimal(".005"),1,1)
        ledger.finish("judge",outcome="200")
        clock[0] = 61
        ledger.recover()
        data = snapshot(ledger)
        self.assertEqual(data["requests"],4)
        self.assertEqual(data["failures"],1)
        self.assertEqual(data["cache_hits"],1)
        self.assertEqual(data["retries"],1)
        self.assertEqual(data["attempts"],4)
        self.assertEqual(data["unknown"],2)
        self.assertEqual(data["evaluation_attempts"],1)
        self.assertEqual(data["evaluation_cost"],.005)
        total = Decimal(totals(ledger)["total_cost"])
        self.assertEqual(Decimal(str(data["total_cost"])),total)
        self.assertAlmostEqual(data["cost_per_1000"], float(total)/4*1000)
        self.assertEqual(sum(data["model_distribution"].values()),3)
        exported = prometheus(ledger)
        self.assertIn('router_failures{source="mock"} 1',exported)
        self.assertNotIn("router_matched_savings_ratio",exported)
        dashboard = json.loads(Path("monitoring/grafana/dashboards/router.json").read_text(encoding="utf8"))
        self.assertEqual(len(dashboard["panels"]),10)
        self.assertTrue(all("mock" in p["title"].lower() for p in dashboard["panels"]))

    async def test_finalisation_barrier_and_redaction(self):
        ledger = SQLiteLedger(clock=lambda:0)
        ledger.begin("r","run")
        ledger.reserve("a","r","m","mock","inference","v1",Decimal("1"))
        ledger.dispatch("a")
        ledger.job("j","r")
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp,"result.json")
            result = write_results(output,ledger,"run",{"seed":13},[
                {"content":"Bearer secret sk-abcdef", "Authorization":"private"}])
            self.assertFalse(result["final"])
            self.assertNotIn("totals", result)
            self.assertNotIn("secret",output.read_text())
            self.assertNotIn("private",output.read_text())
            ledger.clock = lambda:61
            ledger.recover()
            self.assertTrue(ledger.barrier("run"))
            ledger.job("j2","r")
            self.assertFalse(ledger.barrier("run"))
            ledger.job("j2","r","quality_unknown")
            self.assertTrue(ledger.barrier("run"))
            result = write_results(output,ledger,"run",{},[])
            self.assertEqual(Decimal(result["totals"]["total_cost"]),1)

    async def test_mock_local_load_generator(self):
        result = await execute(n=20,concurrency=4,duration=.01,threshold_ms=1000,
                               transport="in-process")
        self.assertEqual(result["errors"],0)
        self.assertTrue(result["passed"])
        self.assertEqual(result["data_source"],"mock")
        with self.assertRaises(ValueError):
            await http_json("https://example.com","GET","/")
