# Model Routing System

A small FastAPI service that sends each chat request to the cheapest model likely to
answer it well. It classifies the request, picks a model within the caller's budget and
deadline, reserves the spend before the call, caches exact repeats, falls back to another
model on failure, and records every cost in a ledger that Grafana charts.

It was built mock-first. Development and tests use mock providers. A few small real
runs measured cost and, with one human rater, answer quality. The numbers are below,
with their limits.

**Status:** working prototype, not production ready. Real-provider paths (LiteLLM with
OpenAI and Anthropic) were exercised only in the benchmark runs below. Ollama is
configurable but was never run.

## What the measurements show

All figures come from real provider calls. Total spend for the measured runs was well
under the USD 15 cap (see `docs/BENCHMARK.md`). Prices were supplied by the project
owner and were not checked against provider billing.

Conditions: **A** always uses the frontier model (the naive baseline), **B** routes
without a cache, **C-cold** routes with an empty cache, **C-warm** routes with a cache
that already holds every answer.

### Cost: savings run (60 prompts, 4 conditions, USD 0.26)

| Condition | Cost per prompt | Saving vs A (95% interval) | Completed |
|---|---|---|---|
| A, always frontier | $0.00308 | n/a | 59 of 60 |
| B, routed | $0.00052 | 83.0% (79.5% to 86.6%) | 50 of 60 |
| C-cold | $0.00054 | 82.6% (79.1% to 86.1%) | 51 of 60 |
| C-warm | $0.00020 | 93.6% (89.6% to 97.5%) | 53 of 60 |

The routed conditions complete fewer requests than A. Part of the cheaper cost is
requests that did not finish. The warm-cache figure assumes every request is a repeat,
which is a scenario, not a production repetition rate.

### Answer quality: one human rater, descriptive only

Ratings are 1 to 5, given blind by the project author, who is the only rater. Prompts
where either output did not complete are left out of the quality comparison.

| Run | Pairs rated | Mean A | Mean B | Difference (B minus A) | B higher / tied / lower | Sign test p |
|---|---|---|---|---|---|---|
| Held-out | 54 of 60 | 4.31 | 4.52 | +0.20 (95% interval 0.00 to +0.43) | 11 / 39 / 4 | 0.12 |
| Savings | 50 of 60 | 4.46 | 4.72 | +0.26 (95% interval +0.12 to +0.42) | 10 / 40 / 0 | 0.002 |

**No quality claim is made.** The rule required 60 complete rated pairs and neither run
reached it. No pass or fail rule was written down before the savings run. Most scores
are 5, so the scale cannot separate the models well. Read the table as "no sign that
routing hurt quality on these prompts", not as proof of equal quality.

### Other results

- **Classifier:** 20 of 43 dev prompts match the author's tier label (46.5%). It is a
  low-accuracy heuristic, and the tier labels are the author's own.
- **Automated judge:** an LLM judge was tried and failed calibration against human
  ratings, so it was dropped. All quality results are human ratings.
- **Latency overhead (mock provider, HTTP, 100 requests, 10 at a time):** p95 router
  overhead was 48.3, 42.4 and 43.1 ms against a 50 ms target. Before ledger connection
  pooling it was 1,902 ms. The margin is thin. This measures the router, not provider
  speed.

## Architecture

~~~mermaid
flowchart LR
  C[Client] --> A[FastAPI validation]
  A --> H[Classifier and routing policy]
  H --> R[Exact Redis cache]
  R -->|miss| S[Postgres spend reservation]
  S --> F[Deadline-bound fallback]
  F --> M[Mock provider / LiteLLM adapter]
  M --> L[Settlement and attempt ledger]
  L --> P[Prometheus and Grafana]
~~~

Postgres row locking serialises spend admission across processes. If a call's outcome is
unknown, the full reservation stays charged and is never replayed. Exact cache hits can
still be served when the spend store is down. Folders: `api/`, `classifier/`, `routing/`,
`fallback/`, `cache/`, `monitoring/`, `config/`, `benchmark/`, `scripts/`, `tests/`, `docs/`.

## Quickstart

Python 3.11 or newer. No provider keys are needed for mock mode.

~~~text
python -m venv .venv
.venv/Scripts/python.exe scripts/manage.py install
.venv/Scripts/python.exe scripts/manage.py up
.venv/Scripts/python.exe -m scripts.seed_mock
~~~

On macOS or Linux use `.venv/bin/python`. `up` needs Docker Desktop. It starts the API on
:8000, Redis :6379, Postgres :5432, Prometheus :9090 and Grafana :3000, all on loopback.
Local development logins are the defaults in `docker-compose.yml`; change them before
exposing anything.

~~~http
POST /v1/route
Content-Type: application/json

{"messages":[{"role":"user","content":"Explain a queue"}],"max_tokens":512,"max_cost_usd":0.01,"deadline_ms":10000}
~~~

The response has `model`, `tier`, `content`, `usage`, `cost`, `cache_hit`,
`fallback_count` and `request_id`. Errors: 400 invalid input, 402 over the request
budget, 422 unknown model or missing price, 503 spend store, cap or all models
unavailable, 504 deadline. Input limit 32 KB; `max_tokens` default 512, max 2048;
deadline default 10 s, max 60 s.

### Tests

The full suite needs Postgres and Redis running (the `up` command above starts both):

~~~powershell
$env:ROUTER_TEST_POSTGRES_DSN='postgresql://router:router@127.0.0.1:5432/router'
$env:ROUTER_TEST_REDIS_URL='redis://127.0.0.1:6379/0'
./.venv/Scripts/python.exe scripts/manage.py test
./.venv/Scripts/python.exe scripts/manage.py lint
~~~

Without those variables the Postgres and Redis tests are skipped. No test calls a
provider. Make targets (`install`, `test`, `lint`, `up`, `down`, `benchmark-mock`,
`load-test-mock`) wrap the same scripts, for example
`make PYTHON=.venv/Scripts/python.exe test`.

## Benchmark tooling

~~~text
python -m benchmark.run --split dev --output results/dev-mock.json
python -m benchmark.recover results/dev-mock.json
python -m benchmark.ratings --artifact <run.json> --mode human-complete-pairs --output <new-folder>
python -m benchmark.evaluate <run.json> <ratings.json> <mapping.json> --output <file>
~~~

Run artifacts cannot be overwritten, so every run needs a new output path. `recover`
finalises unknown charges without calling a provider. `evaluate` applies blinded human
ratings to a held-out or savings run and reports cost, completion and the descriptive
quality numbers above. Real runs need `--real`, the environment variable
`ROUTER_CONFIRM_REAL_SPEND=YES` and shared Postgres, and they stop at the configured
USD 15 global cap. The runner, its safeguards and the deviations from the plan are in
[docs/BENCHMARK.md](docs/BENCHMARK.md).

## Data

Dataset v1 has 153 prompts (43 dev, 50 calibration, 60 held-out). Dataset v2 has 60
separate prompts for the savings run. Both are frozen with SHA-256 checksums. Most
prompts were written by an AI assistant and disclosed as such. Three are questions from
the public GSM8K test set, used under its MIT licence; the licence is in
`benchmark/data/GSM8K_LICENSE.txt` and the sources are in
[docs/DATASET.md](docs/DATASET.md). Prompts contain no personal data. Raw run outputs
and ratings are not stored in this repository.

## Status against the plan

| Item | Status |
|---|---|
| Classifier, routing, fallback, ledger, cache, metrics | Implemented and tested |
| Benchmark runner, recovery, blinded ratings export | Implemented; real runs completed |
| Docker Compose stack | Starts; Prometheus scrapes the API |
| Grafana dashboard | 9 of 10 panels return data (checked through the APIs, not by eye); the matched-savings panel is empty by design. Panels read the shared Postgres ledger, which includes real-run spend. No screenshots yet. |
| Measured savings | Reported above, with the limits stated |
| Quality conclusion | Not made (60-pair rule not met) |
| Ollama | Unverified |

More detail: [test matrix](docs/TEST_MATRIX.md), [verification log](docs/VERIFICATION.md),
[decisions](docs/DECISIONS.md), [dashboard formulas](docs/dashboard.md).

## Limitations

- One rater, who is also the author. Ratings cluster at 5.
- Prompts are mostly assistant-authored and small in number. They do not show how the
  router behaves on real traffic.
- Routed models completed fewer requests than the frontier baseline in the savings run.
- The classifier is a rule-based heuristic with 46.5% agreement on dev prompts.
- Prices are owner-supplied and unverified against provider billing. Mid and frontier
  models are GPT-4.1 mini and GPT-4o; the cheap model is Claude Haiku 5.5.
- Outputs are sampled at temperature 1, so reruns differ. Cost and quality figures are
  one run each, not repeated.
- The spec's 40 to 60 percent savings figure was a hypothesis. The measured saving is
  higher, but with the completion and quality caveats above.
- SQLite is only a test double for the ledger; real concurrency needs Postgres.
- Judge queues are in memory and bounded; a large backlog can drop samples.
- Near-duplicate caching is off; exact-match caching only.
- No authentication, multi-tenancy, streaming or cloud deployment. Compose credentials
  are local development defaults.
