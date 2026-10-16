# Dashboard definitions

Data source: Prometheus uid prometheus, scraping API /metrics every five seconds.
All current exported series and panels carry source=mock. No measured or
counterfactual series is fabricated. Grafana provisioning and formulas are supplied;
Grafana/Prometheus execution and screenshots remain unverified in this environment.

| Panel | Formula | Denominator and scope |
|---|---|---|
| Cost per 1,000 API requests (mock) | 1000 * router_total_cost / clamp_min(router_requests,1) | All API request rows, including failures and cache hits; inference + evaluation |
| Model distribution (mock) | router_model_requests by model | Successful request rows only; cache hits included; panel counts sum to successful requests |
| Inference cost (mock) | router_inference_cost | All inference attempts; actual settled usage or full reserved/unknown charge |
| Evaluation cost (mock) | router_evaluation_cost | Judge attempts with parent request attribution; separate from inference |
| Total incurred or reserved cost (mock) | router_total_cost | Inference + evaluation; outstanding reservations included conservatively |
| API error fraction (mock) | router_failures / clamp_min(router_requests,1) | Request outcomes other than 200/pending over all request rows |
| Cache hit fraction (mock) | router_cache_hits / clamp_min(router_requests,1) | All request rows |
| Additional attempts (mock) | router_retries | Sum max(0, inference attempts - 1) per request; includes retries and cross-model fallback |
| Unknown outcomes (mock) | router_unknown | Attempts in unknown state after recovery; dispatched pending rows are not yet counted as unknown |
| Matched benchmark savings (mock; pending) | router_matched_savings_ratio by condition | 1 - condition incurred cost / A incurred cost, on the identical prompt set only |

No denominator drops failed prompts. An empty denominator renders zero for the
operational panels. The model panel is counts, with Grafana optionally displaying
shares of their sum. A request whose ledger write is impossible during a database
outage is absent; this limitation is not disguised as a zero-error observation.

The savings series is absent until MATCHED_RESULTS_FILE refers to a final artifact
with complete A/B/C-cold/C-warm coverage. It never uses a live token-count
counterfactual. The current artifacts and series are mock, even if their price
formulas resemble provider price tables. Any future live counterfactual must be
labelled counterfactual and must not replace matched benchmark savings.

Monitoring values come from the ledger in one scrape transaction, rather than
incrementing independent counters that could diverge after a crash. Cost gauges
can decrease when a worst-case reservation settles at actual usage. PromQL uses
these gauges directly, never rate() on cost reservations. Multiple API worker
scrapes of the same shared ledger must not be summed; provisioned scrape is one
service target. For multiple replicas choose one target for these global gauges.

Fixture verification in test_stage4 asserts requests=4, failures=1, hits=1,
additional attempts=1, attempts=4, unknown=2, evaluation attempts=1 and evaluation
cost=0.005 simulated USD, with inference derived from the configured usage prices.
It checks attempt total = request total = results total = dashboard snapshot total.
Grafana titles, expressions and source labels are checked as repository artifacts;
actual PromQL evaluation/rendering requires the unavailable local services.
