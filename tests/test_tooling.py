import json
from pathlib import Path
import tempfile
import unittest
from benchmark.dataset import split
from benchmark.judge import judge
from benchmark.ratings import export_ratings
from benchmark.results import analyse, write_results
from benchmark.run import run_conditions
from benchmark.statistics import paired_savings
from monitoring.ledger import SQLiteLedger
from routing.service import Router


class ToolingTests(unittest.IsolatedAsyncioTestCase):
    async def test_durable_judging_idempotent_dev_fixture(self):
        # Only DEV prompts are dispatched; no held-out execution.
        prompts, manifest = split("dev")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp,"run.json")
            ledger = SQLiteLedger(path.with_suffix(".sqlite"))
            run_id, records = await run_conditions(Router(ledger),prompts[:1])
            write_results(path,ledger,run_id,dict(storage="test-double",seed=13,
                dataset_checksum=manifest["sha256"],analysis=analyse(records,1,resamples=100)),records)
            result = await judge(path)
            self.assertTrue(result["final"])
            self.assertFalse(result["judge_validated"])
            self.assertEqual(len(ledger.attempts()),7)  # 3 inference + 4 judge
            first = result["totals"]
            result = await judge(path)
            self.assertEqual(first,result["totals"])
            self.assertEqual(len(ledger.attempts()),7)
            self.assertTrue(all(not q["supported"] for q in result["analysis"]["quality"].values()))
            ledger.close()

    async def test_blinded_calibration_export_without_dispatch(self):
        prompts, _ = split("calibration")
        fixture = {"records":[dict(prompt_id=r["id"],condition=c,content="synthetic fixture",
                                   completed=True,status=200)
                              for r in prompts for c in ("A","B")]}
        with tempfile.TemporaryDirectory() as tmp:
            output = Path(tmp,"ratings")
            result = export_ratings(fixture,output,mode="calibration")
            self.assertEqual(result["individual_ratings"],50)
            blinded = json.loads((output/"ratings-blinded.json").read_text())
            self.assertEqual(len(blinded),25)
            self.assertTrue(all("left_condition" not in r for r in blinded))
            mapping = json.loads((output/"mapping-private.json").read_text())
            self.assertEqual(len({r["prompt_id"] for r in mapping}),25)
            self.assertTrue({r["tier"] for r in mapping} >= {"simple","medium","complex"})
            self.assertTrue({r["task_type"] for r in mapping} >= {"general","reasoning","code","maths"})

    async def test_paired_savings_interval(self):
        result = paired_savings([2]*10,[1]*10,resamples=100)
        self.assertEqual(result["ratio"],.5)
        self.assertEqual(result["ci"],[.5,.5])
