# Model Routing System

## Latency SLO

The live API, benchmark generation and judges use one **20000 ms SLO**, configured
by `slo_ms`. It measures **full-completion latency**, from provider dispatch to the
final response, excluding settlement/storage time. Every benchmark generation or
standalone judge run starts fresh health for all its models; the API keeps health
across requests. This decision follows 120 of 200 HTTP 503 responses caused by
cooldown in `results/calibration-real-v3.json`. See [decisions](docs/DECISIONS.md).

## Matched temperature correction (2026-10-17)

Matched generation uses **temperature 1 for all conditions and models** because
`claude-haiku-5-5` accepts only temperature 1. API and internal request defaults
also use 1; provider adapters forward the request temperature unchanged, and the
API still accepts the range 0..2. Outputs are sampled and are **not identical
across runs**. Reproducibility is distributional, assessed with per-prompt
bootstrap confidence intervals on the fixed repeat subset, not exact-output
identity. The benchmark seed fixes ordering and resampling, not provider outputs.

The earlier temperature-0 real calibration run returned 115 Anthropic HTTP 400
rejections and 98 dependent benchmark-condition HTTP 503 responses. Its output
files, including `results/calibration-real.json`, are kept unchanged for audit;
that failed run does not establish matched quality or savings. This change has
not rerun calibration or held-out evaluation and makes no new real-provider claim.
The 115 rejected calls remain recorded as dispatched/estimated. Whether to revise
that accounting is an open decision; ledger accounting is unchanged in this round.

A mock-first FastAPI router implementing all four development milestones in
PLAN.md, including the v4 recovery rules. It classifies chat requests, chooses a
configured model within budget and deadline, reserves spend before each call,
caches exact responses, falls back on failures, and exposes ledger metrics for
Grafana.

**Status:** mock implementation with offline verification. Real-provider paths are
unverified after the failed real calibration above. Human judge calibration and
the held-out benchmark remain incomplete.
Savings, real answer quality and real provider latency results are pending a real
benchmark run. No measured savings or quality result is claimed.

## Configured models and pricing

The cheap model uses the listed provider alias `anthropic/claude-haiku-5-5`
(Claude Haiku 5.5). Per the supplied provider model-list finding, the dated ID
`claude-haiku-5-5-20261001` was not listed. Each provider response's model ID is
recorded as `resolved_model`; the requested alias stays in `model` in the ledger
and API, and `requested_model` in result records and their attempt lists.
Result metadata includes `resolved_model_ids` and counts in `model_resolutions`,
including inference and judge calls. Missing response IDs remain null; cache
hits retain the originating snapshot without adding an attempt. Prices remain
user supplied and unverified against provider billing. Mid/frontier remain
GPT-4.1 mini/GPT-4o with flat mock prices.

| Cheap model input tokens per request | Input USD / million | Output USD / million |
|---|---|---|
| Up to and including 100,000 | $0.10 | $0.50 |
| Above 100,000 | $0.50 | $2.50 |

Input usage selects one tier for all input and output tokens in a request;
this per-request assumption is pending user confirmation. Reservation uses
estimated input with the higher rates from 90,000 through 100,000 tokens;
settlement uses actual provider input usage. The public input cap remains 32 KB.
Models may use ordered `tiers` ending in an unbounded (null) tier, or flat
`input_per_million`/`output_per_million` prices. Invalid prices return 422.

The dated `price_version` is `user-supplied-2026-10-17-v2`. Bump it when models,
prices or tiers change. Effective cache/ledger versions also include a price
content hash, so tier edits invalidate cached results automatically.
See [pricing decisions](docs/DECISIONS.md) and [verification](docs/VERIFICATION.md).

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
additive stop_reason, data_source and resolved_model identify completion, mock
data and the provider-returned model snapshot. Validation
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

For the local Compose services, run the complete suite in PowerShell:

~~~powershell
$env:ROUTER_TEST_POSTGRES_DSN='postgresql://router:router@127.0.0.1:5432/router'
$env:ROUTER_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
./.venv/Scripts/python.exe scripts/manage.py test
./.venv/Scripts/python.exe scripts/manage.py lint
~~~

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
execution requires an explicit opt-in flag and final dev costs. It has now been run once: results/held-out-real-1.json holds 240 of 240 records with HTTP 200 and USD 0.2115 spend, and its human analysis is in the deviation note above and docs/BENCHMARK.md.

Dataset: 153 prompts, split 43 dev / 50 calibration / 60 held-out. Seed, checksum,
licence, human subset and repeat subset are frozen. See [provenance](docs/DATASET.md).
The classifier was reverted to the original frozen heuristic rules, with thresholds
[2,5] unchanged. Dev agreement is low: 20/43 (46.51%). No held-out data was used.
Answer quality is the outcome that matters; this diagnostic does not measure it
or establish generalisation. See docs/DECISIONS.md.

Blinded exports and calibration validation are in benchmark/ratings.py. Fifty
individual calibration ratings are 25 paired A/B examples across tier/task strata.
Calibration human ratings are still pending; [calibration status](monitoring/calibration_status.json)
records that. The held-out human subset has been rated by one blinded rater: 54 complete
pairs, not the predeclared 60. Six prompts with an incomplete A or B output are excluded
from all human analysis. The B quality claim stays withheld by the 60-pair gate. The
54-pair result is descriptive only: mean rating A 4.32, B 4.52, difference +0.20, with no
95% bootstrap interval 0.00 to +0.41 and a two-sided sign test p = 0.12 (B higher on 11 of 15 non-tied pairs). The interval touches zero, so no clear quality difference is shown. See docs/BENCHMARK.md for the full deviation.
The human-subset analyzer is benchmark/evaluate.py.
See [benchmark workflow](docs/BENCHMARK.md) for formats and safeguards.

## Explicit real-provider entry points (not run here)

Install the optional real dependencies only when preparing a separately authorised
real run. Configure verified model IDs and prices in a versioned JSON file; the
cheap model prices are user-supplied; the other flat prices remain illustrative. All real entry points require both
`--real` and environment variable `ROUTER_CONFIRM_REAL_SPEND=YES`; they require
shared Postgres storage and enforce the configured USD 15 global / USD 3 evaluation caps.
The USD 15 global cap is the user-approved real-spend limit.
Use the same DATABASE_URL and REDIS_URL as the API so spend, RPM and model health
are shared. Credentials are supplied through provider environment variables.

- API: `python -m api.app --real` (optional `ROUTER_CONFIG` selects configuration).
- Dev/calibration: `python -m benchmark.run --real --config <verified-config.json> --split dev --output <new-output.json>`.
- Judge: `python -m benchmark.judge <real-artifact.json> --real`.

Held-out additionally requires `--allow-held-out` and matching real dev results for
preflight. No held-out command or real-provider call was executed in this review.
The LiteLLM path was tested only with an in-memory stub and remains labelled
`real-unverified`. Quality calibration and measured claims remain pending.

## Spec traceability (PLAN section 8)

| Spec item | Honest delivery status |
|---|---|
| Complexity classifier | Implemented; feature/boundary tests ran; dev diagnostic reported |
| Cost-aware routing (GPT-3.5 / Haiku / GPT-4o) | Configured Claude Haiku 5.5 / GPT-4.1 mini / GPT-4o chains; mock only; IDs/prices unverified, GPT-3.5 not probed |
| Latency P95 and reroute | Per-model cooldown/probe leases implemented; clock tests ran; actual Redis/provider latency unverified |
| Quality scoring (LLM-as-judge) | Async queue, rubric, caps, gate and alerts tested with fixtures; human calibration pending; operational routing gate closed |
| Fallback | Mock retry/failure/deadline matrix ran |
| Redis caching | Exact adapter supplied; adversarial/contract tests ran; real Redis integration skipped |
| Redis rate tracking | Atomic per-model/minute cap, fail open; offline tests ran; real Redis integration skipped |
| Cost ledger | Postgres reservation/settlement/recovery implemented; SQLite contract double tested/reconciled; real Postgres unverified |
| Grafana dashboard | JSON and provisioning load; Prometheus scrapes the API. Nine of ten panels return data (checked via the Grafana and Prometheus APIs, not visually). The savings panel is empty by design (pending). Panels labelled "mock" read the shared Postgres ledger, so their cost values include earlier real-run spend. |
| Ollama | Optional configurable LiteLLM path exists, unverified and uncalled |
| Benchmark and load test | Dev mock run, local in-process load diagnostic (p95 0.5 ms, passes), and one real held-out benchmark run (240 of 240 records, USD 0.2115) ran. HTTP load test on the Docker stack, mock provider (N=100, C=10): 0 errors and p95 local overhead of 42 to 48 ms across three runs, against a 50 ms threshold. Before the connection pool, p95 was 1,902 ms; the cause was a new Postgres connection for each of 9 ledger operations per request. The margin to the threshold is small. |
| Docker | Compose/Dockerfile supplied; startup unverified: daemon/compose unavailable |

[Acceptance coverage](docs/TEST_MATRIX.md) distinguishes offline, partial and unmet
criteria. [Verification](docs/VERIFICATION.md) records commands and actual results.
[Decisions](docs/DECISIONS.md) records interpretations/deviations.
[Dashboard definitions](docs/dashboard.md) document formulas and denominators.

## Limitations

- FastAPI/httpx, Postgres and Redis integration tests were skipped here. Dependency
  installation, Docker daemon and compose were unavailable. Core tests do not establish
  deployment correctness. Live PromQL checks and Grafana screenshots are pending.
- LiteLLM/OpenAI/Anthropic/Ollama paths are unverified. Cheap model prices were
  supplied by the user and not independently verified against the provider page;
  other flat prices remain illustrative mock prices. Explicit real entry points
  require --real, ROUTER_CONFIRM_REAL_SPEND=YES and shared Postgres admission.
  Adapter/composition tests used stubbed LiteLLM only; no key was read.
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
