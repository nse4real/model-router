from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
from statistics import mean
from monitoring.security import redact
from monitoring.settlement import SettlementJournal
from benchmark.statistics import bootstrap_ci, non_inferiority, paired_savings

CONDITIONS = ("A", "B", "C-cold", "C-warm")


def totals(ledger, run_id=None):
    requests = ledger.requests(run_id)
    ids = {r["request_id"] for r in requests}
    attempts = [a for a in ledger.attempts() if a["parent_request_id"] in ids]
    inf = sum((Decimal(str(a["cost"])) for a in attempts if a["purpose"] == "inference"), Decimal(0))
    evaluation = sum((Decimal(str(a["cost"])) for a in attempts if a["purpose"] == "evaluation"), Decimal(0))
    request_total = sum((Decimal(str(r["inference_cost"])) + Decimal(str(r["evaluation_cost"]))
                         for r in requests), Decimal(0))
    if inf + evaluation != request_total:
        raise ValueError("Ledger reconciliation failed")
    return dict(inference_cost=str(inf), evaluation_cost=str(evaluation), total_cost=str(inf+evaluation))


def analyse(records, n, seed=13, judge_validated=False, quality_source="human pending", resamples=10000, data_source="mock"):
    human = quality_source == "human"
    validated_judge = quality_source == "validated_judge" and judge_validated is True
    if "judge" in quality_source and judge_validated is not True:
        quality_source = "judge withheld: not validated"
    elif quality_source == "pending":
        quality_source = "human pending"

    def source_reason(condition):
        if human:
            return None if condition in {"A", "B"} else "Human ratings unavailable for this condition"
        if validated_judge:
            return None
        if quality_source in {"human pending", "judge withheld: not validated"}:
            return quality_source
        return "Quality source unavailable"

    report = {}
    full = all(len([r for r in records if r["condition"] == c]) == n for c in CONDITIONS)
    for condition in CONDITIONS:
        rows = [r for r in records if r["condition"] == condition]
        costs = [float(r["cost"]) for r in rows] + [0.] * (n-len(rows))
        accepted_reason = source_reason(condition)
        if accepted_reason is None and any(r.get("completed", False) and r.get("quality") is None for r in rows):
            accepted_reason = "Missing quality scores on completed outputs"
        accepted = (None if accepted_reason is not None
                    else sum(r.get("completed", False) and (r.get("quality") or 0) >= 4 for r in rows))
        report[condition] = dict(prompts=n, observed=len(rows),
            completion_rate=sum(r.get("completed", False) for r in rows)/n,
            total_cost=sum(costs), cost_per_prompt=sum(costs)/n,
            cost_per_prompt_ci=bootstrap_ci(costs, seed, resamples),
            accepted=accepted, accepted_reason=accepted_reason,
            accepted_denominator="completed outputs with quality >=4",
            cost_per_accepted_response=sum(costs)/accepted if accepted else None)
    quality = {}
    by = {(r["prompt_id"], r["condition"]): r for r in records}
    prompts = sorted({r["prompt_id"] for r in records})
    for condition in ("B", "C-cold", "C-warm"):
        reason = source_reason(condition)
        if reason is not None or not full:
            quality[condition] = {"supported": False, "reason": reason or "Incomplete coverage"}
            continue
        pairs = [(by.get((p,"A")), by.get((p,condition))) for p in prompts]
        # A missing score on a completed answer is unknown, never silently assigned a pass.
        pairs = [(a,b) for a,b in pairs if a and b and
                 (not human or (a.get("quality") is not None and b.get("quality") is not None)) and
                 (not a["completed"] or a.get("quality") is not None) and
                 (not b["completed"] or b.get("quality") is not None)]
        if human and len(pairs) != 60:
            quality[condition] = {"supported": False, "reason": "Need predeclared 60 human pairs"}
            continue
        if validated_judge and len(pairs) != n:
            quality[condition] = {"supported": False, "reason": "Missing judge scores"}
            continue
        quality[condition] = non_inferiority([a.get("quality",1) for a,b in pairs],
            [b.get("quality",1) for a,b in pairs], [a["completed"] for a,b in pairs],
            [b["completed"] for a,b in pairs], seed, resamples)
    savings = {}
    for condition in ("B", "C-cold", "C-warm"):
        if full:
            savings[condition] = paired_savings(
                [float(by[p,"A"]["cost"]) for p in prompts],
                [float(by[p,condition]["cost"]) for p in prompts], seed, resamples)
    return dict(coverage_complete=full, headline_claim_supported=False,
                **{"savings_mock" if data_source == "mock" else "savings_measured": savings},
                quality_source=quality_source, conditions=report, quality=quality,
                note=("Mock data cannot support measured cost savings or answer quality claims"
                      if data_source == "mock" else
                      "Real provider results; quality claims require the predeclared validation and ratings"))


def reproducibility(first_records, second_records, subset, seed=13, resamples=10000):
    report = {}
    for condition in CONDITIONS:
        a = [r for r in first_records if r["prompt_id"] in subset and r["condition"] == condition]
        b = [r for r in second_records if r["prompt_id"] in subset and r["condition"] == condition]
        if len(a) != len(subset) or len(b) != len(subset):
            report[condition] = {"supported": False, "reason": "Incomplete fixed subset"}
            continue
        cost_ci = bootstrap_ci([float(r["cost"]) for r in a], seed, resamples)
        second_mean = mean(float(r["cost"]) for r in b)
        quality_known = all(not r["completed"] or r.get("quality") is not None for r in a+b)
        quality_ci = bootstrap_ci([r.get("quality", 1) if r["completed"] else 1 for r in a],
                                  seed, resamples) if quality_known else [None, None]
        second_quality = mean(r.get("quality",1) if r["completed"] else 1 for r in b) if quality_known else None
        report[condition] = dict(cost_ci=cost_ci, second_cost_per_prompt=second_mean,
            quality_ci=quality_ci, second_quality=second_quality,
            supported=quality_known and cost_ci[0] <= second_mean <= cost_ci[1] and
                      quality_ci[0] <= second_quality <= quality_ci[1])
    return report


def write_results(path, ledger, run_id, metadata, records):
    final = ledger.barrier(run_id)
    request_ids = {r["request_id"] for r in ledger.requests(run_id)}
    attempts = [a for a in ledger.attempts() if a["parent_request_id"] in request_ids]
    journal = {e["attempt_id"]: e for e in SettlementJournal().entries()}
    by_request = {}
    resolutions = {}
    for attempt in attempts:
        identity = (attempt["model"], attempt["resolved_model"], attempt["purpose"])
        resolutions[identity] = resolutions.get(identity, 0) + 1
        by_request.setdefault(attempt["parent_request_id"], []).append(dict(
            attempt_id=attempt["attempt_id"], requested_model=attempt["model"],
            resolved_model=attempt["resolved_model"], purpose=attempt["purpose"],
            state=attempt["state"], cost=str(attempt["cost"]), cost_status=attempt["cost_status"],
            latency_ms=float(attempt["latency_ms"]) if attempt["latency_ms"] is not None else None,
            error=json.loads(attempt["error"]) if attempt["error"] else None,
            settlement_status=(journal[attempt["attempt_id"]]["status"]
                               if attempt["attempt_id"] in journal else None)))
    exported = []
    for record in records:
        record_attempts = by_request.get(record.get("request_id"), [])
        statuses = {a["settlement_status"] for a in record_attempts if a["settlement_status"]}
        if statuses:
            record = dict(record, settlement_status="unknown" if "unknown" in statuses else "settlement_retried")
        # Cache hits retain the originating response identity without inventing
        # a paid call. Recovery/judging refresh all attempt identities from SQL.
        exported.append(dict(record, requested_model=record.get("requested_model", record.get("model")),
                             resolved_model=record.get("resolved_model"), attempts=record_attempts,
                             # Refresh full-completion timings from SQL, including judges.
                             attempt_latencies=[dict(attempt_id=a["attempt_id"], latency_ms=a["latency_ms"])
                                                for a in record_attempts]))
    payload = dict(metadata, run_id=run_id, data_source=metadata.get("data_source", "mock"), final=final,
                   timestamp=datetime.now(timezone.utc).isoformat(), records=exported,
                   resolved_model_ids=sorted({a["resolved_model"] for a in attempts
                                              if a["resolved_model"] is not None}),
                   model_resolutions=[dict(requested_model=requested, resolved_model=resolved,
                                           purpose=purpose, calls=count)
                                      for (requested, resolved, purpose), count in sorted(
                                          resolutions.items(), key=lambda item: (
                                              item[0][0], item[0][1] or "", item[0][2]))])
    if final:
        payload["totals"] = totals(ledger, run_id)
    else:
        payload.pop("totals", None)
        payload.pop("analysis", None)
        payload["status"] = "incomplete_pending_recovery_or_judging"
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic replacement allows a restart to inspect a complete prior artifact.
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(redact(payload), indent=2, allow_nan=False), encoding="utf8")
    temporary.replace(path)
    return payload
