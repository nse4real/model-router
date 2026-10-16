# Decisions and deviations

The source files are unchanged. PLAN.md has a stale "Version 3" heading but
contains the v4 revision and its four fixes; those v4 rules take precedence.
The user's instructions override the plan's checkpoint commits, real-provider
verification, human-rating completion and held-out execution at delivery.
No commit, push or branch operation was performed by this implementation.

## Runtime and contracts

- Python 3.11+ standard-library core; FastAPI is a thin HTTP adapter. Tests use
  unittest, so the offline core is runnable without downloading dependencies.
- JSON configuration avoids an additional YAML dependency in runtime code.
  The model aliases, chains, prices, thresholds, judge and SLO are configurable.
- POST rejects unknown fields, invalid roles, nonfinite numbers, empty messages,
  token limits outside 1..2048, deadline limits outside 1..60000 and input over
  32,768 UTF-8 bytes. Errors include a request id. Temperature defaults to 0.
- Response tier is the classifier's tier; model identifies the actual selected
  model after budget, health, quality pressure and fallback. Cost includes all
  incurred inference attempts for the request, including conservative unknowns.
  Cache hits cost zero and retain the cached usage as provenance.
- Stop reason and data_source are additive response fields. Normal completion
  means stop_reason == stop; truncated outputs are never cached. Empty or
  content-policy outputs cause fallback rather than an empty 200 response.
- Only max_tokens and temperature are supported generation parameters. Unknown
  generation parameters are rejected; every supported parameter is in the key.
- The default API and every benchmark/load entry point accept mock providers
  only. The unverified LiteLLM adapter can be explicitly injected in application
  code, using configurable OpenAI/Anthropic/Ollama model settings. It is never
  instantiated by tests or these commands. No provider key is read by mock code.

## Spend, persistence and outages

- Compose uses Postgres. Every reservation transaction locks the singleton
  spend_lock row and includes all prior charged or outstanding reservations.
  USD 25 global and USD 3 evaluation ceilings cannot be increased by configuration.
  All workers/judges/benchmarks must use the same Postgres database and schema.
- A cheap read-only admission check implements global-cap precedence before
  request-budget selection. A second atomic admission transaction commits the
  actual attempt reservation immediately before dispatch. Selection itself does
  not create an abandoned reservation for an unhealthy or rate-limited model.
- A UTF-8-byte bound plus per-message framing and 64 tokens of overhead reserves
  input tokens; configured max_tokens reserves output. Real tokenizer bounds and
  prices remain unverified. Unexpected usage above the bound fails settlement,
  withholds success and conservatively retains the reservation.
- Provider failures with no usage retain the full reservation until recovery.
  Even an apparently rejected call is not inferred free. Explicit actual usage
  can settle it, including zero usage. Recovery after 30 seconds plus a 30 second
  grace period marks reserved/dispatched rows unknown; it never dispatches them.
- Settlement failure logs redacted content with attempt id, returns 503 and leaves
  the attempt dispatched. Requests refresh both cost scopes after settlement.
- SQLiteLedger is a transactional contract test double, including file-backed
  restart tests, not evidence of Postgres correctness. Separate test-double
  files have separate simulated caps. The normal benchmark backend is Postgres;
  --storage test-double is an explicit mock-only diagnostic option.
- Postgres initialization is lazy so a service can still serve Redis cache hits
  during a database outage. Recording a cache hit or rejected request during a
  total database outage is best effort: the database cannot durably record rows
  while unavailable. This is a physical limitation of the exactly-one-row goal.
- Redis cache bypasses on outage; model request limits fail open. Known-open
  latency circuits remain blocked without Redis. Other models may proceed under
  durable spend admission, using local latency observations. Recovery probes
  never run without the shared Redis lease. A worker cannot enforce previously
  unseen remote cooldown state while Redis is unreachable; this is a limitation.
- Benchmark checkpoints are atomic JSON replacements plus durable ledger writes.
  Recovery reports all spend, marks abandoned judge jobs unknown, and never
  replays inference. There is deliberately no automatic resume of missing calls.
  A deliberate rerun is a new run with new reservations. Final means the ledger
  barrier is satisfied; full dataset coverage is a separate required flag.

## Latency and quality

- P95 is nearest-rank, over the last 50 provider calls per model, active after
  20 samples; the SLO is 2,000 ms by default. Cooldown is 60 seconds. Redis TIME
  is used for distributed cooldown transitions. A probe timeout is 30 seconds,
  lease TTL 45 seconds, renewal every 10 seconds, with token-checked release.
- Retry backoff is a bounded 10 ms deterministic delay. At most two attempts per
  model and three configured models per request. All retries are application-owned.
- Quality sampling includes cache hits. Live sampling is 10%, queue capacity 100,
  judge timeout 5 seconds, separate evaluation ceiling USD 3. Jobs are claimed
  once and terminal updates are idempotent. Stale queued/running jobs expire to
  quality_unknown after the 30-second timeout plus 30-second grace. Unknown scores
  never pass a gate. Queue expiration can discard samples during a long backlog.
- The mock judge always emits a fixture score of 3. It is diagnostic only and
  cannot validate itself. Quality routing is disabled until actual calibration
  passes and an operator explicitly enables it. No live request is regenerated
  in response to a quality score. Benchmark quality routing is always disabled.
- Fifty individual calibration ratings means 25 A/B pairs (two output ratings
  per pair), selected across all three tiers and four task types. The separate
  calibration split contains 50 available prompts. Small/degenerate breakdowns
  fail the gate rather than silently dropping a stratum.
- Each gate checks within-one agreement >=0.80, paired-difference bias and its
  95% bootstrap interval inside [-0.2,+0.2], and positive Spearman paired-difference
  agreement with its interval excluding zero, overall and by tier/task.
- Alert: last 30 scores have mean <3.5. Recovery uses a fresh batch of 30 scores
  with mean >=3.5, repeating batches while an alert remains. Budget takes precedence.
- An accepted response must be normally completed and have a known quality score
  >=4, the smallest integer meeting the operational quality floor of 3.5.
  Unknown quality yields a null accepted count/cost, not a measured zero.

## Dataset, benchmark and reporting

- 150 original authored fixture prompts plus three MIT-licensed GSM8K questions.
  These are assistant-authored rather than human-authored prompts; they have not
  been human validated. The tiny public base is a scope limitation, not a
  representative benchmark. Provenance is in DATASET.md; tiers are diagnostic labels.
- The frozen seed is 13. Splits are 43 dev / 50 calibration / 60 held-out; text is
  deduplicated case-insensitively after whitespace normalization. Assignment is
  saved per record. The manifest fixes the 60 human pairs and 30 repeat prompts.
- Thresholds [2,5] are frozen without tuning. The dev accuracy diagnostic is
  reported honestly; it is not a quality or savings result.
- A uses only the first configured complex model (no cheap baseline fallback).
  B routes without cache. C-cold starts from an empty isolated cache; C-warm runs
  after the complete cold pass. A/B/C-cold order is seeded and interleaved per
  prompt; warm order is independently shuffled after cache population. All share
  system prompt, max_tokens 512 and temperature 0. Cold workload repetition is 0;
  warm repeats 100% of it. This is a cache scenario, not a production repetition estimate.
- Preflight uses the mean complete dev per-prompt cost across all four conditions,
  including retries/failures/evaluation, times a 1.25 safety factor. It reduces
  sample size before execution against the USD 12 held-out allocation. This is
  an estimate; the durable global cap remains the hard guarantee.
- Statistical routines default to 10,000 seeded bootstrap prompt resamples.
  Non-inferiority uses lower two-sided 95% percentile bound > -0.3. Non-completed
  outputs score 1; completed-only sensitivity and completion rates are separate.
  With an unvalidated judge, only the predeclared 60 human B/A pairs can support
  that comparison; C quality is withheld. Mock results never support a headline.
- The load target defaults to the loopback HTTP API and verifies mock mode. An
  explicit in-process diagnostic is also provided. Local overhead excludes client
  scheduling before a concurrency slot is acquired; no real provider latency is
  reported. Default N=100, C=10, D=5 s, error rate <1%, P95 <50 ms.

## Environment-driven deviations

- The unified shell helper failed before process creation. Commands were executed
  with Node child_process.execFile in the same workspace instead; no shell
  interpolation, privilege escalation or provider networking was used.
- Installed Python was 3.15.0. An isolated 3.13 download was blocked by network
  permissions; package installation found no available FastAPI distribution.
- Docker daemon unavailable and the accessible CLI has no compose subcommand.
  Real Postgres/Redis and compose startup, HTTP API,
  Grafana rendering/screenshots and HTTP load performance could not be verified.
  Optional local integration tests are supplied and skipped unless configured.
- lint uses the supplied offline AST/syntax/whitespace/conflict-marker checker.
  Ruff remains optional; it was not installed or run. This is narrower than a
  full static-analysis/type-checking result and is labelled accordingly.
- No human ratings, real-provider smoke calls, real price/model validation,
  exchange-rate confirmation, held-out run or real reproducibility run occurred.
  Savings, real quality and real latency results are pending.
- No checkpoint commits were made, per the user's instruction.

Quality sample windows/alerts are worker-local; the plan specifies distributed
coordination explicitly for spend and latency probes, but not for quality windows.
Scaling quality alerts across workers would require shared aggregation.
