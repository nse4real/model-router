"""Ledger-derived gauges; each scrape reconciles a single durable snapshot."""
from collections import Counter
from decimal import Decimal
import json
from pathlib import Path


def snapshot(ledger, data_source="mock"):
    # One transaction prevents a scrape from mixing pre- and post-settlement totals.
    with ledger.transaction(snapshot=True) as db:
        requests = [dict(r) for r in db.execute("SELECT * FROM requests").fetchall()]
        attempts = [dict(a) for a in db.execute("SELECT * FROM attempts").fetchall()]
    inference = sum((Decimal(str(a["cost"])) for a in attempts if a["purpose"] == "inference"), Decimal(0))
    evaluation = sum((Decimal(str(a["cost"])) for a in attempts if a["purpose"] == "evaluation"), Decimal(0))
    n = len(requests)
    return dict(requests=n, failures=sum(r["outcome"] not in ("200", "pending") for r in requests),
                cache_hits=sum(r["cache_hit"] for r in requests),
                retries=sum(r["fallback_count"] for r in requests),
                attempts=len(attempts), unknown=sum(a["state"] == "unknown" for a in attempts),
                evaluation_attempts=sum(a["purpose"] == "evaluation" for a in attempts),
                inference_cost=float(inference), evaluation_cost=float(evaluation),
                total_cost=float(inference+evaluation),
                cost_per_1000=float(inference+evaluation)/n*1000 if n else 0,
                model_distribution=dict(Counter(r["final_model"] or "unknown" for r in requests if r["outcome"] == "200")),
                latency_ms=sorted(float(r["latency_ms"]) for r in requests), data_source=data_source)


def prometheus(ledger, matched_results=None, data_source="mock"):
    s = snapshot(ledger, data_source)
    lines = []
    for name in ("requests", "failures", "cache_hits", "retries", "attempts", "unknown",
                 "evaluation_attempts", "inference_cost", "evaluation_cost", "total_cost"):
        metric = "router_" + name
        lines += [f"# TYPE {metric} gauge", f'{metric}{{source={json.dumps(data_source)}}} {s[name]}']
    lines.append("# TYPE router_model_requests gauge")
    for model, count in s["model_distribution"].items():
        lines.append(f'router_model_requests{{source={json.dumps(data_source)},model={json.dumps(model)}}} {count}')
    # No live counterfactual is presented as savings. This series exists only for
    # a final matched artifact with full condition coverage.
    if matched_results and Path(matched_results).exists():
        result = json.loads(Path(matched_results).read_text())
        analysis = result.get("analysis", {})
        if result.get("data_source") == data_source and result.get("final") and analysis.get("coverage_complete"):
            conditions = analysis["conditions"]
            baseline = conditions["A"]["total_cost"]
            if baseline:
                for c in ("B", "C-cold", "C-warm"):
                    savings = 1 - conditions[c]["total_cost"] / baseline
                    lines.append(f'router_matched_savings_ratio{{source={json.dumps(data_source)},condition="{c}"}} {savings}')
    return "\n".join(lines) + "\n"
