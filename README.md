# Model Routing System

A mock-first FastAPI router implementing all four development milestones in
PLAN.md, including the v4 recovery rules. It classifies chat requests, chooses a
configured model within budget and deadline, reserves spend before each call,
caches exact responses, falls back on failures, and exposes ledger metrics for
Grafana.

**Status:** mock implementation with offline verification. Real-provider paths are
unverified. Human calibration and the held-out benchmark have not been run.
Savings, real answer quality and real provider latency results are pending a real
benchmark run. No measured savings or quality result is claimed.

## Architecture

~~~mermaid
flowchart LR
  C[Client] --> A[FastAPI validation]
  A --> H[Pure classifier and configured policy]
  H --> R[Exact Redis cache]
  R -->|miss| S[Postgres durable spend reservation]
  S --> F[Deadline-bound fallback]
  F --> M[Mock provider / unverified LiteLLM adapter]
  M --> L[Settlement and attempt ledger]
  L --> P[Prometheus and Grafana]
  L --> Q[Bounded async judge queue]
  Q --> S
~~~

Postgres row locking serializes admission across processes. Unknown outcomes
retain the full reservation; recovery never replays them. Judge spend is linked
to its parent request and uses both global and evaluation caps. Quality routing
is gated by calibration and disabled for benchmarks. Exact cache hits can serve
during spend-store outages.

Folders follow the spec: api/, classifier/, routing/, fallback/, cache/,
monitoring/, tests/, plus config/, benchmark/, scripts/ and docs/.

## Quickstart

Python 3.11+ is required. No provider credentials or .env file are needed.
Core tests and lint run without third-party dependencies:

~~~text
python scripts/manage.py test
python scripts/manage.py lint
~~~

For the HTTP service, install dependencies in your virtual environment and start
Docker Desktop:

~~~text
python -m venv .venv
.venv/Scripts/python.exe scripts/manage.py install
.venv/Scripts/python.exe scripts/manage.py up
.venv/Scripts/python.exe -m scripts.seed_mock
~~~

On macOS/Linux use .venv/bin/python. The seed command checks mock mode before
posting four loopback requests. Compose exposes API :8000, Redis :6379, Postgres
:5432, Prometheus :9090 and Grafana :3000 on loopback only. Grafana development
login: admin / mock-admin. The provisioned Model Router dashboard labels all data
mock. Matched savings stays empty until an eligible artifact is configured.

~~~http
POST /v1/route
Content-Type: application/json

{"messages":[{"role":"user","content":"Explain a queue"}],"max_tokens":512,"max_cost_usd":0.01,"deadline_ms":10000}
~~~

Response: model, tier, content, usage, cost, cache_hit, fallback_count, request_id;
additive stop_reason and data_source identify completion and mock data. Validation
returns 400; unknown model/missing price 422; request budget rejection 402;
unavailable spend storage/cap/eligible models or chain failure 503; deadline 504.
Errors include request_id. Input limit is 32 KB. max_tokens defaults to 512, max
2048; deadline defaults to 10 seconds, max 60 seconds.

Every Make target calls a Python script:

| Target | Action |
|---|---|
| install | Install project and development dependencies |
| test | unittest discovery, with optional local integrations |
| lint | Offline AST syntax, whitespace and conflict-marker checker |
| up / down | Docker compose startup / shutdown, retaining data volume |
| benchmark-mock | Dev mock benchmark using shared Postgres admission |
| load-test-mock | Fixed workload against the loopback HTTP mock API |

Example: make PYTHON=.venv/Scripts/python.exe test. Ruff is optional and was not
run here; reported lint is the supplied offline checker.

Set ROUTER_TEST_POSTGRES_DSN and/or ROUTER_TEST_REDIS_URL for optional dedicated
local infrastructure tests. Postgres tests create/drop a unique test schema;
Redis tests use unique keys. No provider calls occur.

## Mock benchmark tooling

Default dev-only command, sharing the API Postgres spend cap:

~~~text
python -m benchmark.run --split dev --output results/dev-mock.json
~~~

Explicit test-double diagnostics for machines without local infrastructure:

~~~text
python -m benchmark.run --split dev --storage test-double --output results/dev-double-mock.json
python -m benchmark.judge results/dev-double-mock.json
python -m benchmark.diagnostics
python -m benchmark.load_test --transport in-process --n 100 --concurrency 10 --duration 1
~~~

Existing run artifacts cannot be overwritten. A new run needs a new output path.
After a crash and the timeout/grace interval:

~~~text
python -m benchmark.recover results/dev-double-mock.json
~~~

Recovery finalizes unknown charges without provider calls or replay. Missing work
leaves coverage incomplete. Full incurred spend is reported after the finalisation
barrier. The fixed mock judge is diagnostic and cannot validate real quality.
In-process load measures local core overhead, not HTTP performance or provider
latency.

The runner implements A always frontier, B without cache, C-cold and C-warm;
matched generation settings; seeded interleaving; dev-cost preflight and sample
reduction; durable caps; failure-inclusive denominators; atomic results; paired
savings intervals; paired non-inferiority with margin -0.3 and 10,000 bootstrap
resamples; and a fixed 30-prompt reproducibility comparison. Non-completed outputs
score 1; completion rate and completed-only sensitivity remain separate. Unknown
quality never passes. C quality is withheld without a validated judge. Held-out
execution requires an explicit opt-in flag and final dev costs; it was not run.

Dataset: 153 prompts, split 43 dev / 50 calibration / 60 held-out. Seed, checksum,
licence, human subset and repeat subset are frozen. See [provenance](docs/DATASET.md).
Classifier dev agreement is 20/43 tier labels (46.51%), not an answer-quality result.

Blinded exports and calibration validation are in benchmark/ratings.py. Fifty
individual calibration ratings are 25 paired A/B examples across tier/task strata.
No human ratings were collected; [calibration status](monitoring/calibration_status.json)
is pending. The human-subset analyzer is benchmark/evaluate.py.
See [benchmark workflow](docs/BENCHMARK.md) for formats and safeguards.

## Spec traceability (PLAN section 8)

| Spec item | Honest delivery status |
|---|---|
| Complexity classifier | Implemented; feature/boundary tests ran; dev diagnostic reported |
| Cost-aware routing (GPT-3.5 / Haiku / GPT-4o) | Configured Haiku / GPT-4.1 mini / GPT-4o chains; mock only; IDs/prices unverified, GPT-3.5 not probed |
| Latency P95 and reroute | Per-model cooldown/probe leases implemented; clock tests ran; actual Redis/provider latency unverified |
| Quality scoring (LLM-as-judge) | Async queue, rubric, caps, gate and alerts tested with fixtures; human calibration pending; operational routing gate closed |
| Fallback | Mock retry/failure/deadline matrix ran |
| Redis caching | Exact adapter supplied; adversarial/contract tests ran; real Redis integration skipped |
| Redis rate tracking | Atomic per-model/minute cap, fail open; offline tests ran; real Redis integration skipped |
| Cost ledger | Postgres reservation/settlement/recovery implemented; SQLite contract double tested/reconciled; real Postgres unverified |
| Grafana dashboard | JSON, provisioning and formulas supplied; fixture values tested; rendering/screenshots unavailable |
| Ollama | Optional configurable LiteLLM path exists, unverified and uncalled |
| Benchmark and load test | Dev mock run and local in-process load diagnostic ran; held-out/real benchmark and HTTP load not run |
| Docker | Compose/Dockerfile supplied; startup unverified: daemon/compose unavailable |

[Acceptance coverage](docs/TEST_MATRIX.md) distinguishes offline, partial and unmet
criteria. [Verification](docs/VERIFICATION.md) records commands and actual results.
[Decisions](docs/DECISIONS.md) records interpretations/deviations.
[Dashboard definitions](docs/dashboard.md) document formulas and denominators.

## Limitations

- FastAPI/httpx, Postgres and Redis integration tests were skipped here. Dependency
  installation, Docker daemon and compose were unavailable. Core tests do not establish
  deployment correctness. Live PromQL checks and Grafana screenshots are pending.
- LiteLLM/OpenAI/Anthropic/Ollama paths are unverified. Prices are illustrative
  versioned mock prices, not verified current quotes. The normal API rejects real
  mode; an explicitly enabled adapter can be separately injected. No key was read.
- Human calibration, held-out ratings, held-out/real benchmark, quality/savings
  claims and real reproducibility remain pending. The spec's 40–60% savings figure
  is an untested hypothesis.
- The small assistant-authored dataset has only three public-base examples and
  has not been human validated. It does not establish representativeness.
- SQLite is a contract double, not proof of Postgres concurrency. Separate double
  files have separate simulated caps; normal operation requires shared Postgres.
- During total database outages, cache-hit/rejection ledger writes are best effort.
  During Redis outages, previously unseen remote cooldown state is unknowable;
  known-open models stay blocked and no unlocked recovery probe is admitted.
- Judge queues are bounded and in memory. Stale queued/running jobs become
  quality_unknown after 60 seconds; a large backlog can therefore lose samples.
  Recovery never replays them. Unknown provider outcomes retain worst-case charges.
- Unique cold / fully repeated warm workloads are explicit cache scenarios,
  not a production repetition-rate estimate.
- Near-duplicate caching is disabled; validation would require 300 human assessed
  hits with zero errors for the stated rule-of-three bound.
- Authentication, multi-tenancy, streaming, cloud deployment and a web UI beyond
  Grafana are outside scope. Compose credentials are local development defaults.
