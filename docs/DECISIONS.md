# Decisions and deviations

## One full-completion latency SLO and per-run health (2026-10-17)

- User decision: **ONE latency SLO = 20000 ms**, configured by `slo_ms` in
  `config/models.json`, for the live API, benchmark generation and judging.
  `Config`, `Health` and shared admission default to the same value. No separate
  benchmark SLO or mode-dependent SLO selection remains.
- The metric is **full-completion latency**, measured with a monotonic clock
  from provider dispatch to the final response (or failure/cancellation), before
  settlement/storage work. It is not time to first token. The per-model P95 uses
  the last 50 attempts and acts after 20 samples. Cooldown remains 60 seconds;
  request deadlines, provider timeouts and renewable probe leases are unchanged.
- Reason: `results/calibration-real-v3.json` had **120 of 200 responses return
  HTTP 503 due to cooldown**. The user chose this full-completion SLO and a fresh
  health state for every benchmark run so prior runs cannot suppress its models.
- Every `benchmark.run.run_conditions` invocation starts isolated health for all
  chain models, including the frontier baseline and attached judge. Redis uses
  `router:health:benchmark:<run_id>:<model>` and matching probe keys; in-process
  runs receive a fresh memory store. Standalone `benchmark.judge` also creates a
  fresh health scope on every invocation, distinct from generation, live traffic
  and previous judge invocations. Redis observations refresh a 3600-second TTL.
- The API keeps persistent model health across requests. Benchmarks do not clear
  live keys. RPM admission and durable spend caps retain their shared behavior.
- Nullable `attempts.latency_ms` stores the same full-completion measurement for
  inference and judge attempts, including failures, retries and cancellation.
  Existing SQLite/Postgres schemas migrate idempotently; unavailable or historic
  measurements remain null. Timing-write failure preserves settlement behavior.
  The common result exporter refreshes both `attempts[].latency_ms` and
  `attempt_latencies` from SQL, including standalone judging and recovery.
- Regression coverage retains the existing outcome assertions and adds the
  20000 ms boundary, persistent API cooldown, memory/Redis judge isolation,
  idempotent repeated judging and full-completion exports excluding settlement.

Verification for this correction is recorded in docs/VERIFICATION.md. No real
provider calls, calibration or held-out execution were performed. PLAN.md and
historical calibration artifacts were left unchanged. No commit, push or branch
change was made.

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

The source files are unchanged. PLAN.md has a stale "Version 3" heading but
contains the v4 revision and its four fixes; those v4 rules take precedence.
The user's instructions override the plan's checkpoint commits, real-provider
verification, human-rating completion and held-out execution at delivery.
No commit, push or branch operation was performed by this implementation.

## Provider alias, resolved snapshots and approved spend (2026-10-17)

- The supplied provider model-list finding says `claude-haiku-5-5-20261001`
  was not listed. Configure the listed alias `anthropic/claude-haiku-5-5`.
  This implementation does not make a real provider call to verify availability.
- Capture the provider response's `model` (Anthropic/OpenAI through LiteLLM)
  as `resolved_model` for every returned inference or judge response, including
  rejected content and invalid judge scores. The ledger's existing `model` is
  the requested ID; it is never replaced with the resolved ID. Record the
  snapshot before validating the answer or settling usage. No response ID means
  null, including migrated historical attempts; never infer a dated snapshot.
- Add a nullable attempt column with idempotent migrations for existing SQLite
  test doubles and Postgres ledgers. API/cache responses retain the snapshot;
  a cache hit creates no provider attempt. Result records include requested and
  resolved IDs plus an attempt list covering fallbacks and judging. Run metadata
  summarizes `resolved_model_ids` and `model_resolutions` with per-identity call
  counts. Exports and recovery derive attempts from the ledger for that run.
- The user-approved real-spend cap is **USD 15 global**, configured as
  `global_cap_usd: "15"`; `evaluation_cap_usd: "3"` stays **USD 3**.
  Snapshot tracking does not change pricing, reservation or settlement logic.
- PLAN.md and historical mock artifacts/descriptions remain unchanged. Tests use
  provider stubs and local services; no API key values, real provider calls,
  held-out/calibration real runs or git mutations are needed.

## Retired model replacement and tier pricing (2026-10-17)

- The user reports `anthropic/claude-3-haiku-20240307` retired. The cheap alias
  now uses the provider alias `anthropic/claude-haiku-5-5`, per the finding above.
  Live model availability and these prices were not independently verified against
  the provider page. No real provider call or API key was used.
- User-supplied USD prices per million tokens: input/output $0.10/$0.50 for
  input usage up to and including 100,000; $0.50/$2.50 above 100,000.
  **Pending user confirmation:** input usage selects one tier per request;
  that tier prices all input and output tokens in the request. This assumes
  neither marginal bands nor cumulative usage across requests.
- Optional `tiers` is an ordered, nonempty list of objects with
  `max_input_tokens`, `input_per_million` and `output_per_million`.
  Finite bounds must be positive, strictly increasing integers; the final bound
  is null (unbounded). Prices must be finite and nonnegative. When `tiers` is
  present it is authoritative; malformed tiers cannot fall back to flat prices.
  Missing or invalid tier/flat prices raise the same 422 price error.
  Mid, frontier and local models retain their flat-price configuration.
- Reservation selects a tier using the existing conservative input estimate:
  UTF-8 content bytes plus role length, 16 framing tokens per message and 64
  overhead tokens. Output reserves `max_tokens`. At estimates from 90,000 through
  100,000 inclusive, reserve the higher tier's input AND output rates on those
  estimated counts; above 100,000 the higher tier applies normally. For any
  finite boundary the same 10% guard applies. The guard never reduces a reserve.
  Settlement selects the tier from actual provider input usage, including usage
  on error responses; it does not reuse the reservation's tier.
- The supplied tier rates increase with context, so input/output upper bounds
  and the guard preserve conservative reservation. Real tokenizer bounds remain
  unverified; the existing ledger refuses actual costs above the committed
  reservation and withholds success. The guard changes rates, not token limits.
- `price_version` remains one dated string, now
  `user-supplied-2026-10-17-v2`. Bump it for model/price/tier changes. The effective
  `Config.version` also appends a SHA-256 digest of canonical flat/tier price
  contents, including thresholds and list order. Cache keys and attempt ledger
  rows use that effective string, so changing tiers invalidates keys even if a
  manual version bump is missed. Benchmark price metadata uses the same string.
- The API's 32 KB input cap is unchanged. Boundary fixtures use synthetic internal
  requests above that transport limit and mock provider responses; no larger
  public request support or real long-context verification is claimed.
- PLAN.md and historical ignored results/ artifacts remain unchanged. Existing
  uncommitted work was preserved; no commit, push, branch change or held-out
  benchmark was performed.

## Runtime and contracts

- Python 3.11+ standard-library core; FastAPI is a thin HTTP adapter. Tests use
  unittest, so the offline core is runnable without downloading dependencies.
- JSON configuration avoids an additional YAML dependency in runtime code.
  The model aliases, chains, prices, thresholds, judge and SLO are configurable.
- POST rejects unknown fields, invalid roles, nonfinite numbers, empty messages,
  token limits outside 1..2048, deadline limits outside 1..60000 and input over
  32,768 UTF-8 bytes. Errors include a request id. Temperature defaults to 1 (range 0..2).
- Response tier is the classifier's tier; model identifies the actual selected
  model after budget, health, quality pressure and fallback. Cost includes all
  incurred inference attempts for the request, including conservative unknowns.
  Cache hits cost zero and retain the cached usage as provenance.
- Stop reason and data_source are additive response fields. Normal completion
  means stop_reason == stop; truncated outputs are never cached. Empty or
  content-policy outputs cause fallback rather than an empty 200 response.
- Only max_tokens and temperature are supported generation parameters. Unknown
  generation parameters are rejected. The key now also includes the full provider
  parameter mapping (including seed, stop, top_p) and provider_config_version.
- API/benchmark/judge entry points default to mocks. Real entry points require
  --real, ROUTER_CONFIRM_REAL_SPEND=YES and shared Postgres admission. LiteLLM is
  imported only inside the opted-in adapter call. Tests inject an in-memory
  LiteLLM stub; no provider key or real provider endpoint is used. Load tests
  remain mock-only.

## Spend, persistence and outages

- Compose uses Postgres. Every reservation transaction locks the singleton
  spend_lock row and includes all prior charged or outstanding reservations.
  The configured user-approved caps are USD 15 global and USD 3 evaluation.
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

## Postgres lock contention fix (2026-10-17)

- `PostgresLedger(dsn, lock_timeout_ms=2000, ...)` gives spend-lock acquisition
  its own configurable positive integer budget in milliseconds. It does not
  inherit the request deadline or the normal 200 ms SQL statement budget.
  While acquiring the singleton row, transaction-local `statement_timeout` is
  disabled and `lock_timeout` enforces the independent budget, including when
  the connection has a shorter default statement timeout. No reservation check
  or write moves outside the locked transaction; admitted spend still includes
  all outstanding reservations and cannot exceed either cap.
- The request deadline continues to run during connection/setup/lock waiting.
  After acquisition, SQL lock/statement timeouts are recalculated from the
  remaining request time, at most 200 ms per operation, with the existing 1 ms
  cleanup allowance when expired. The router checks remaining time after
  storage work and before dispatch/return: a successful admission wait can lead
  to 504 if the deadline expired, but contention within the lock budget alone
  does not produce a storage 503. A committed reservation abandoned at that
  point remains reserved for conservative recovery. Measured usage still gets
  its settlement attempt before deadline rejection. Connection establishment
  retains its independent 2-second timeout. These bounds allow durable cleanup;
  they are not a hard wall-clock response guarantee at the request deadline.
- Lazy schema setup commits in its own transaction before request work. A
  schema-scoped PostgreSQL advisory transaction lock serializes bootstrap across
  workers; bootstrap takes the spend row before subsequent DDL to match normal
  spend transaction lock ordering. Bootstrap waits use the same independent
  lock budget and subsequent DDL statements are bounded by that budget. Its
  committed success is remembered even if admission then rejects on the cap.
  This removes the observed concurrent DDL lock upgrades/deadlocks and prevents
  a cap rejection from repeatedly running schema setup.
- Driver failures still expose only `StorageUnavailable("Spend storage
  unavailable")`. The ledger logger emits the psycopg class and SQLSTATE, and
  attaches the original exception as `LogRecord.storage_exception`. It does not
  format driver text or tracebacks, which may include DSNs, credentials or SQL
  values. Custom diagnostic handlers must keep that private field out of text
  logs and HTTP responses. No DSN, credential or prompt is included in the
  emitted message.
- Offline regressions cover independent budgets, deadline recalculation,
  deadline-before-dispatch, bootstrap commit ordering and safe failure logging.
  Opt-in real Postgres tests retain the original 8-thread, two-ledger admission
  specification (3 admitted / 17 SpendExceeded), hold the row for over 200 ms
  before a successful reservation, and exhaust a 50 ms configured budget to
  verify rollback and private SQLSTATE 55P03 diagnostics. The local Redis test
  exercises cache, rate limits and probe leases. Tests use only mock providers.
- Local Postgres and Redis integration now passes, superseding the earlier
  environment note about their unavailability. Full compose startup, Grafana
  rendering and real providers are outside this fix's verification.

## Latency and quality

- P95 is nearest-rank, over the last 50 provider calls per model, active after
  20 samples; the full-completion SLO is 20000 ms for all entry points. Cooldown
  is 60 seconds. Redis TIME is used for distributed cooldown transitions. A probe timeout is 30 seconds,
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
- Thresholds [2,5] are frozen without tuning. The original heuristic rules give
  low dev agreement of 20/43 (46.51%); no held-out data was used. Answer quality
  is the outcome that matters; this diagnostic is not a quality or savings result.
- A uses only the first configured complex model (no cheap baseline fallback).
  B routes without cache. C-cold starts from an empty isolated cache; C-warm runs
  after the complete cold pass. A/B/C-cold order is seeded and interleaved per
  prompt; warm order is independently shuffled after cache population. All share
  system prompt, max_tokens 512 and temperature 1. Cold workload repetition is 0;
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


## Round-two review fixes (2026-10-16)

All nine code findings are fixed with regressions in tests/test_review_round2.py.
External integration remains unverified as recorded in VERIFICATION.md.

1. Authorization redaction consumes the whole value through the end of the line,
   regardless of scheme; provider error strings are also scanned for key/bearer
   patterns. Recursive structured-field redaction remains in place.
2. Deadline checks surround cache/admission/storage operations, retry sleeps,
   measured settlement, request finalisation and success return. Provider usage
   settles before an expired request becomes 504. Cache writes and quality job
   submission use a bounded response background queue. Blocking cache I/O and
   job persistence run off the event loop; asyncio queue mutations stay on it.
   Saturation logs response_background_queue_full and drops that optional task;
   pending cache writes may therefore miss on an immediate subsequent request.
   Benchmarks explicitly drain the queue before checkpoints/warm passes/barriers.
   Postgres applies the remaining budget (maximum 200 ms) to SQL work after
   spend-lock acquisition; the later Postgres lock contention fix above gives
   acquisition its own budget. SQLite bounds mutex/busy waits by the deadline.
   Expired requests get a 1 ms storage cleanup allowance. These are per-operation
   limits, not a hard operating-system scheduling guarantee. Postgres connection
   establishment retains its separate 2-second connect timeout; live deadline
   timing against unavailable infrastructure has not been verified.
3. Judge claim and evaluation reservation commit in one spend-locked transaction.
   Recovery and the barrier use that same lock. Recovery leaves old jobs pending
   when they have a newer active reservation; terminal jobs cannot be reserved.
4. Cache keys include canonical model provider parameters and an explicit provider
   configuration version, in addition to messages/generation/price fields.
5. Losing a probe lease with a response retains measured tokens and cost while
   returning 503. The stale holder cannot update model health and is not retried
   as if its paid result had been free.
6. Metrics explicitly request REPEATABLE READ on Postgres and a SQLite snapshot.
   A driver-contract test reproduced the original mixed snapshot using read-only
   source from rollback commit 79131b2, then passed against the fixed code.
7. Inference and quality share the Admission object for health/RPM. Judge calls
   reserve durable spend and record provider latency in the model's health window.
   Production benchmark and judge commands also use the API's Redis model keys.
8. Durable benchmark_runs / benchmark_records tables retain metadata and pending
   condition attribution before the first reservation. Recover accepts an absent
   JSON path or a SQLite ledger directly; it never replays paid calls. It retains
   full charges and incomplete-output status, including a first-call crash.
9. Real CLI/API/judge composition exists behind the explicit flag plus environment
   confirmation and shared Postgres spend cap. Real mode rejects test-double CLI
   storage. Test-only dependency injection proves adapter/guard/cap behaviour with
   stubbed LiteLLM; it is not evidence of real model availability, prices or usage.
   Retry count and timeout cannot be overridden by provider configuration. Results
   and metrics preserve real-unverified attribution instead of labelling it mock.

The classifier was reverted to the original frozen rules from commit 79131b2.
Dev agreement is 20/43 (46.51%) via python -m benchmark.diagnostics on checksum
95833de696995a30777be087da8caea9670e566b160a44c106661dc00c3d71da.
Thresholds remain [2,5], without tuning. This heuristic has low dev agreement
on the small assistant-authored set. Answer quality is the outcome that matters;
dev tier-label agreement measures neither answer quality nor generalisation.
No held-out data was used. Existing dataset integrity tests only inspect split
assignments/IDs and checksums; no calibration/held-out threshold search ran.

Existing cache tests now drain background work before asserting a persisted hit.
The provider-timeout fixture uses a 20 ms deadline with cache disabled to avoid
thread-startup timing deciding whether dispatch starts; injected-clock deadline
regressions independently exercise settlement/finalisation overruns. The classifier
boundary fixture checks the original score-4/score-5 boundary using the frozen
keyword rules.

The terminal now starts successfully. The edit helper could not write two times;
PowerShell/Python performed workspace edits successfully. PLAN.md was not changed,
and no commit, push, branch change, real call or held-out benchmark was run.


## Windows timer resolution and environment parity (2026-10-17)

The two environment-specific failures came from test timing assumptions, not
LiteLLM import side effects. On this Windows host, Python 3.12.10 reports
GetTickCount64() for time.monotonic(), with resolution 0.015625 s; Python 3.15.0
reports QueryPerformanceCounter(), with resolution 0.0000001 s. The asyncio
loops use those same resolutions. Python 3.12's local asyncio/base_events.py
moves scheduled callbacks to the ready queue up to one clock resolution early.

Evidence collected before edits:

- The two original tests failed alone in .venv-real and passed alone in .venv.
  An import-finder guard that raises on any LiteLLM import reproduced both
  failures in .venv-real; litellm was absent from sys.modules afterwards.
  Test ordering and LiteLLM monkeypatches are therefore not prerequisites.
- Tracing asyncio.wait_for located the first failure in cache asyncio.to_thread,
  before provider dispatch. A warmed 5 ms wait timed out after 4.655 ms measured
  by perf_counter. A cold run timed out after 6.617 ms. The injected request
  clock was still zero: virtual elapsed time did not control the real timer.
- The renewal fixture raced 1 ms renewal against a 5 ms provider completion.
  These timers are below the 15.625 ms loop resolution and can become ready
  together; completion can cancel the renewal task before it checks the store.
- In an isolated Python 3.12 process, warming the executor import and replacing
  only the test loop's time function with perf_counter and _clock_resolution
  with its reported resolution made both original tests pass. Changing only
  that loop without warming still left the cache-startup failure. This was an
  experiment, not a production clock or event-loop policy change.

The tests themselves were wrong to require sub-resolution timer ordering and
to assume an injected clock also controls real asyncio/cache scheduling. The
brief explicitly permits correcting erroneous tests. No product deadline or
lease defect requiring a code change was established by these failures.

- tests/test_regressions.py keeps the cache path, exactly two provider calls,
  and status 504. A 1 s budget with 0.6 s injected per call leaves scheduling
  margin while crossing the virtual deadline on the second call. An added
  assertion requires the injected clock to end at 1.2 s; no real sleep of that
  length is introduced. Real provider timeout coverage remains in test_stage2.
- tests/test_stage3.py holds provider completion on an asyncio.Event until the
  failing renewal actually runs, bounded by an outer 1 s watchdog. It retains
  invalid-permit, rejected-result and still-open-circuit assertions, and adds
  confirmation that renewal was attempted. It no longer infers a renewal from
  the relative durations of two tiny sleeps.
- In-memory mutation probes confirm the corrected tests still fail if routing
  ignores the injected clock or renewal falsely reports success. Neither probe
  edits product files.
- scripts/lint.py excludes .venv-real alongside .venv. The previous checker
  traversed installed dependencies and reported third-party whitespace and
  documentation separators as source errors. All 51 project Python files are
  still checked.

Both full suites pass with the local Postgres and Redis services enabled;
VERIFICATION.md records exact commands and results. Product code and PLAN.md
were left unchanged for this brief. Existing unrelated working-tree edits were
preserved. No real calls, key inspection, calibration/held-out runs, commits,
pushes or branch changes were performed.


## Definitive HTTP 400 rejections settle at zero cost (2026-10-17)

User-approved settlement policy: a provider HTTP 400 is a definitive rejection
under PLAN 4.3. Its attempt becomes settled with cost_status rejected, cost 0,
and input/output tokens 0. The original reservation remains available for audit;
the requested model, any resolved model, and redacted provider error are retained.
Settlement refreshes request totals under the same spend lock as admission, so
released global cap headroom can be reused safely. Routing also releases the
attempt's charge from the request budget before moving to the next model; HTTP
400 does not retry the same model. Losing a probe lease cannot erase a definitive
rejection, although the request still fails with 503 in that case.

This is a narrow exception to conservative unknown-outcome charging in PLAN 5.1.
Timeouts, HTTP 429/5xx, connection errors and unknown outcomes retain the reserved
charge until existing recovery makes them unknown. Successful HTTP responses
that are rejected locally for empty/content-filtered output still settle measured
usage. Existing usage-based settlement for other provider errors is unchanged.
If recording a rejection fails, its reservation remains charged and routing fails
closed. Terminal settled/unknown attempts remain idempotent and are never
retroactively reclassified or refunded.

No historical ledger rows or result artifacts are rewritten. The separately
reported historical total in results/calibration-real.json remains USD 0.1104572
(USD 0.0933274 on status-200 records and USD 0.0171298 on status-503 records).
These are the original recorded charges, without inferred retroactive refunds.

Mock-only regressions in tests/test_pricing.py cover zero settlement and error
redaction, global/request budget reuse, 400/200 reconciliation, conservative
retry/recovery charges, paid content-filtered responses, failed settlement,
probe lease loss, terminal history preservation, and two-ledger concurrent cap
admission with rejections on SQLite and opt-in local PostgreSQL. The existing
error-usage pricing fixture now uses HTTP 403 because HTTP 400 has this explicit
zero-charge policy. No schema change or provider-adapter change is required.


Verification with .venv/Scripts/python.exe and the specified local Postgres/Redis
URLs: scripts/manage.py test ran 132 tests, with 131 passing and 1 failure.
All 10 new rejection tests passed, including local PostgreSQL concurrency.
The remaining failure is tests/test_stage4.py:131: its old fixture expects two
unknown attempts after a provider HTTP 400 plus an explicit unknown attempt.
The approved policy correctly leaves one unknown and one settled rejection.
The necessary expected-count change is 2 to 1; that file is outside this brief's
allowed edit list and was left unchanged pending explicit scope authorization.
The full suite is therefore not reported as passing. scripts/manage.py lint
passed syntax/hygiene checks for all 51 Python files. No real provider calls
or API key inspection were performed.


## Follow-up: rejection fixture and concurrency-test scheduling (2026-10-17)

The stage-4 reconciliation fixture now expects one unknown attempt and explicitly
checks that the primary HTTP 400 attempt has cost_status rejected and cost zero.
Every other existing assertion is retained.

The reproduced concurrency failure is a test scheduling assumption. Admission
can correctly refuse a contender while a rejection's reservation is still live;
settling that rejection later frees headroom without replaying refused work.
Consequently, exhausting a finite set of contenders need not spend the entire cap.
The shared SQLite/Postgres helper must make fresh paid admissions after all mixed
rejections have settled before requiring its final exact charge. Its expected
admitted-count assertion and exact three measured attempts / total cost 3 remain.
Cap checks during the concurrent wave and request/attempt reconciliation remain.

Evidence gathered with .venv/Scripts/python.exe and local Postgres/Redis enabled:

- The initial five attempts were interrupted by concurrent workspace edits that
  introduced an IndentationError at monitoring/ledger.py:335 (20 discovered test
  entries, 11 errors each). These were not valid reproductions of the target race.
- A subsequent five full runs each ran 132 tests. Run 1 had 2 failures and 9 errors
  during concurrent settlement/export edits (including UnboundLocalError for
  journal in benchmark/results.py:111); runs 2 and 5 passed. Runs 3 and 4 failed
  the shared Postgres rejection helper at tests/test_pricing.py:419 with
  AssertionError: Decimal('2') != 3. None reported SQLite lock/storage errors.
  Full exception logs are under the temporary directory
  C:/Users/nsena/AppData/Local/Temp/sol-followup-repro-wo7bcahf (run-1.log through
  run-5.log). The earlier import-error logs are in sol-followup-repro-z_mch4q3.
- A controlled SQLite schedule using the original 200 ms storage budget reserved
  one rejection and two paid attempts, let all remaining contenders be refused,
  then settled the rejection. The original helper deterministically failed with
  Decimal('2') != 3; the corrected helper admitted fresh work and finished at 3.
  No StorageUnavailable or SQLite busy error occurred. Twenty additional isolated
  runs with the original 200 ms budget also passed without storage exceptions.
- Three subsequent full suites passed with 149, 149, and 152 tests respectively.
  Source hashing detected concurrent edits in runs 2 and 3; these runs therefore
  establish passing executions, not validation of one unchanged tree. Their logs
  are in C:/Users/nsena/AppData/Local/Temp/sol-followup-verify-203iytep.

This follow-up did not change monitoring/ledger.py or its busy-timeout/connection
strategy. Another editor concurrently changed storage and settlement behavior,
added tests, and independently added the shared helper's refill. A duplicate
refill from this follow-up was removed while preserving that editor's work.
Only the fixture update and this decision entry are attributable to this
follow-up in the final tree. No PLAN.md or historical result artifact edits,
real provider calls, API key inspection, calibration/held-out runs, commits,
pushes, or branch changes were performed by this follow-up.

## Ledger statement contention and durable paid responses (2026-10-17)

The ledger timeout brief authorizes this change to the settlement-write-failure
behavior described in PLAN 5.1. PLAN.md remains unchanged. A valid paid response
is now returned after bounded settlement recovery, with explicit accounting
status, while the existing whole-request deadline still requires 504 if expired.

### Root cause and reproduction

PostgresLedger used transaction_timeout() for ordinary statements: 200 ms or the
remaining request time, whichever was smaller. Its independent 2000 ms spend-lock
budget only covered SELECT ... FOR UPDATE on spend_lock. A reservation insert,
usage update, model-resolution write or ordinary read could therefore be canceled
under load even after the spend lock had been acquired. Every psycopg failure
became StorageUnavailable, with no distinction between contention and outage.
Admission consequently failed closed before a provider call; a paid successful
response whose settlement failed was logged and then discarded behind a 503.

Read-only inspection of calibration-real-v2.json found 200 records: 68 HTTP 200,
131 HTTP 503 and one HTTP 504. Its local Postgres rows contain 56 attempts: 55
settled (USD 0.0372393), one dispatched (reserved USD 0.00564), total USD 0.0428793.
The full benchmark failure rate is historical evidence, not a new benchmark.

An unmodified-code reproduction with eight concurrent workers completed 160/160
reserve/dispatch/fake-provider/settle cycles on idle local Postgres. To make
statement pressure repeatable, an isolated test schema adds a 250 ms insert
trigger. At N=8 and the former 200 ms statement budget, all eight transactions
roll back with QueryCanceled, SQLSTATE 57014. The new default completes all eight
cycles, with exactly eight attempts and USD 0.008 total measured fixture cost.
This proves the timeout mechanism under controlled statement delay; it does not
claim to reproduce the original database's exact workload or resource pressure.

### Budgets, classification and idempotency

- Ledger statements have their own positive-integer budget, default 2000 ms,
  supplied by statement_timeout_ms or LEDGER_STATEMENT_TIMEOUT_MS. The existing
  independent Postgres spend-lock budget remains 2000 ms by default. SQLite's
  mutex/busy budget uses the statement budget as an equivalent test double.
- Request deadlines do not shorten storage operations. Existing checks before
  admission/dispatch and before returning content remain, and 504 still wins on
  expiry. Storage cleanup may finish after a deadline; this is necessary to
  preserve accounting. This is not a strict wall-clock cancellation of database
  work at the request deadline.
- Admission begin/ping/check/reserve/dispatch retries only TransientContention:
  SQLSTATE 57014 (QueryCanceled), 55P03 (LockNotAvailable), 40001 (serialization),
  40P01 (deadlock), and SQLite busy/locked. There are at most three executions;
  jitter waits are 10-30 ms then 20-60 ms. No retry is scheduled when that wait
  would exhaust the request deadline. Connection/schema/other storage failures
  remain StorageUnavailable and fail closed before any paid call.
- A matching attempt ID, request/model/provider/purpose/price version and reserved
  amount can confirm an existing reserved row, without a second reservation or
  cap debit. Mismatches and terminal replays remain errors. Dispatch can confirm
  an already-dispatched row after a lost acknowledgement; this does not replay
  the provider. Settled/unknown rows remain terminal and idempotent.
- Judge claim/reserve and dispatch use the same contention classification. An
  already-running judge claim is never replayed. Inference and judge responses
  both use the durable settlement helper.

### Exact settlement and recovery behavior

1. Attempt normal model-resolution and measured-usage settlement. On any failure,
   store the paid response, stop reason, usage, requested charge, conservative
   reservation, resolved model and available response/request metadata in a local
   SQLite journal keyed by attempt ID. Commit with synchronous=FULL before any
   settlement retry. Existing secret-pattern redaction applies; content is not
   written to application logs.
2. Retry resolution/settlement twice, with jitter of 10-30 ms and 20-60 ms,
   independently of the request deadline so cleanup can complete. A recovered
   measured settlement returns settlement_status=settlement_retried, with actual
   spend. There is no additional provider call.
3. On exhaustion, durably record unknown, then attempt to mark the SQL row unknown
   under the spend lock, charging its original reservation. A database outage
   leaves the existing reserved/dispatched charge in place and the local unknown
   decision unsynced until recovery. A lost commit acknowledgement that actually
   settled is recognized; it does not create an extra charge.
4. A valid answer is returned as HTTP 200 with its content, attempt_id and
   settlement_status (settled, settlement_retried or unknown). Unknown costs use
   reserved amounts and unknown answers are not cached. A paid response is not
   converted to 503 just because settlement or the request-summary write failed.
   Expired requests still return 504; their saved answer remains available for
   audit/recovery, and recovery preserves the recorded deadline outcome. Invalid
   empty/content-filtered answers remain failed outputs and cannot become a
   successful answer through this mechanism.
5. The API's five-second recovery worker processes the local journal before
   ordinary stale-attempt recovery. benchmark.recover also consumes it before
   exporting results. Recovery retries unfinished accounting, mirrors unknown
   decisions and restores saved benchmark content without provider replay.
   Terminal unknown rows are never automatically refunded to measured spend.
   Benchmark records retain HTTP/completion fields plus settlement_status;
   attempts also export cost, cost_status and their redacted error, so a measured
   charge cannot be mistaken for an acceptable output.

The default journal is results/settlement-recovery.sqlite; LEDGER_RESPONSE_STORE
can select another persistent local path. Compose mounts a named router-responses
volume at /var/lib/router-responses so an API container replacement retains the
journal. CLI recovery must use the same journal path/volume as its worker.
Journal entries are retained for audit, including successfully retried entries;
there is no automatic deletion. This deliberately stores response text on failed
writes, unlike the normal token-only ledger. If the database and local disk both
fail, durable capture cannot be guaranteed: the valid answer is still returned,
unknown spend stays reserved, a critical metadata-only diagnostic is emitted,
and recovery_persisted=false is exposed on the answer. This is an explicit
infrastructure failure, not an unnoticed discard of paid content.

### Four Haiku empty/content-policy attempts: report only

These are local validation failures after a successful provider response with
usage, not definitive provider HTTP 400 rejections. All four used
anthropic/claude-haiku-5-5 and are settled/measured, with 512 output tokens each.
The combined label does not establish whether the original cause was empty
content or content_filter. No historical result or ledger row was rewritten.

| Prompt / condition | Attempt ID | Input tokens | Measured USD | Final request / completed |
|---|---|---:|---:|---|
| complex-040 / B | 7b685534-e0bc-47f2-8867-ca6c285bfb92 | 51 | 0.0002611 | 503 / false |
| complex-043 / C-cold | 7c442a82-24c5-4d3c-b6c2-3e13d512040f | 49 | 0.0002609 | 503 / false |
| complex-031 / B | 58b00b98-17ca-4f0f-8d0e-75d82eabbe8c | 43 | 0.0002603 | 504 / false |
| complex-040 / C-cold | 207a0123-40d9-4754-bd8b-3301cca07d0e | 51 | 0.0002611 | 503 / false |

Their measured spend totals USD 0.0010434. They do not count as completed or
accepted responses; benchmark quality scoring assigns non-completed outputs 1.
The existing router skips a same-model retry and considers the next model,
subject to deadline, request/global budget, health and rate admission. The
complex-031 request next dispatched GPT-4o and ended with 504; the other three
have no subsequent attempt in the local ledger. The ledger does not establish
which eligibility gate prevented those fallbacks.

Open decisions (no policy change made): distinguish empty output, content_filter
and output-limit exhaustion in future provider diagnostics; decide whether an
actual content-policy rejection should terminate the entire chain or retain the
current next-model behavior; determine the cause of these Haiku outputs before
changing any output budget or routing setting. Model IDs, prices, temperature and
routing chains were left as found. Journal retention/access/backup policy remains
an operational decision; entries currently remain available indefinitely.

### Files changed for this brief

monitoring/ledger.py, monitoring/settlement.py (new), routing/service.py,
monitoring/quality.py, api/app.py, benchmark/run.py, benchmark/results.py,
benchmark/recover.py, .env.example, docker-compose.yml;
tests/test_ledger_timeouts.py (new), tests/test_ledger_lock.py,
tests/test_review_round2.py, tests/test_stage2.py, tests/test_resolved_models.py,
tests/test_pricing.py; docs/DECISIONS.md, docs/VERIFICATION.md,
docs/TEST_MATRIX.md. Existing working-tree changes were preserved.

The old timeout/deadline and paid-answer-503 assertions were updated to the new
contract. The concurrent-rejection test also had a scheduling assumption: all
headroom need not be filled if concurrent cap rejections precede a later refund.
It now asserts the concurrent total is positive and at most the cap, then fills
remaining slots deterministically and retains the exact cap, measured-count and
reconciliation assertions. This does not change routing or spending policy.

Final verification after removing the duplicate refill:

- ./.venv/Scripts/python.exe scripts/manage.py test, run 1: 152 tests, OK
  (24.356 s).
- Same command, run 2: 152 tests, OK (25.759 s).
- Same command, run 3: 152 tests, OK (24.382 s).
- Source hashing detected no Python source changes during any of these runs.
  Local Postgres and Redis were enabled using the URLs specified in the brief.
  Full logs: C:/Users/nsena/AppData/Local/Temp/sol-followup-final-kfiygql1.
- The controlled SQLite schedule was rerun against the final refill helper with
  the original 200 ms budget: original helper failed at Decimal('2') != 3;
  final helper passed at exactly 3, without storage errors.
- ./.venv/Scripts/python.exe scripts/manage.py lint: passed, 53 Python files.

## Run-scoped benchmark recovery

Benchmark recovery now supplies the resolved run ID to ledger recovery. The
optional keyword-only `run_id` scopes stale reservations and queued/running judge
jobs through `requests.run_id`, under the existing spend lock. Calling ledger
recovery without that keyword retains global maintenance behavior.

The benchmark uses a filtered settlement journal view for both reconciliation
and restored answers. Only attempt IDs whose durable parent request belongs to
the target run are eligible; a response payload must also name that same parent.
Foreign and orphan journal entries retain their existing sync state and
content. Journal reconciliation retries database settlement only; it never
invokes a provider.
Unknown outcomes retain their full reserved charge, including on repeated
recovery. Live in-scope attempts keep judge jobs pending and the final barrier
closed. Other runs retain their attempt state, cost, request fields and jobs.

`tests/test_recover_scope.py` exercises the same five scenarios against SQLite
and an isolated local Postgres schema: stale dispatched/reserved attempts,
stale and fresh judge jobs, journal reconciliation and unknown charges, payload
ownership/orphan entries, and a live target attempt. The required regression is
`test_recover_run_a_leaves_run_b_dispatched_and_cost_unchanged`. The original four
SQLite cases failed against the pre-fix implementation, demonstrating the bug.

Only benchmark/recover.py, monitoring/ledger.py, tests/test_recover_scope.py and
this decision log were changed for this brief. Existing working-tree edits were
preserved. Recovery was exercised only on temporary synthetic fixtures; no real
provider calls or recovery of real result artifacts were performed.


Verification for run-scoped recovery:

- Full suite, run 1: 179 tests, OK, no skips (43.368 s).
- Full suite, run 2: 179 tests, OK, no skips (41.149 s).
- Both used `./.venv/Scripts/python.exe scripts/manage.py test` with
  `ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router`
  and `ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0` exported. The default
  response journal was redirected to a temporary directory.
- `./.venv/Scripts/python.exe scripts/manage.py lint`: passed, 55 Python files.
- All ten new scope cases passed in both full-suite runs, covering both storage
  implementations. Hash comparison found no changes to tracked files outside
  the four permitted paths during verification.
- Logs: C:/Users/nsena/AppData/Local/Temp/sol-recover-scope-4l4w0oww/suite-1.log
  and suite-2.log in the same directory.
- All requested checks completed. Real providers and real-results recovery were
  intentionally not exercised, as required by the brief. No commits, pushes or
  branch changes were made.


## Benchmark deadline, calibration subsets and charge-preserving merge (2026-10-17)

The subset brief authorizes a benchmark generation deadline of 60000 ms, separate
from the live API default of 10000 ms. `benchmark_deadline_ms` is configurable in
1..60000 and defaults to 60000 even for older configuration files without the
key. `benchmark.run` passes it explicitly to every request and records it in
metadata. The 30-second per-attempt timeout, shared 20000 ms latency SLO, frozen
splits and live contract remain unchanged. PLAN.md was not edited.

The runner accepts `--prompt-ids-file` only for calibration: a nonempty JSON list
of unique in-split IDs. It filters without changing the frozen split/manifest or
seed and executes all four conditions for selected prompts. It saves the exact
selection in a new output and claims the path exclusively before storage or
calls. Existing outputs are refused before provider initialization; an atomic
exclusive open also handles competing output creation during setup.

`benchmark.merge.merge(base_path, subset_path, output_path)` is offline. It
replaces base answer records only for IDs found in the subset, annotates each
record with its source, and records both input paths and exact replaced IDs.
Base and subset metadata must agree on split, seed, dataset, model IDs, price
version, thresholds and provider mode; differing deadlines are expected.

Replacement does not refund a call. Superseded base records, including their
attempts, remain in `superseded_records`. Merged inference/evaluation/total spend
is recomputed from the costs of all attempts in both active and superseded
records, including unknown outcomes. Neither input's cached totals nor record
cost snapshots are trusted. Duplicate attempt IDs and missing attempt exports
are refused rather than silently double-counting or losing charges. Existing
superseded records survive subsequent merges with their original provenance.
Inputs are read only, and output creation is exclusive, including input aliases.

The merge has a fresh artifact ID and storage=merged-artifact. It is an audit and
ratings view, not a new ledger run; judging and recovery must target source runs.
Source metadata is preserved separately. Stale analysis is removed and no new
quality/savings claim is calculated, because active answers and all incurred
spend have different scopes after a rerun. Final requires both sources final.

Only config/models.json, routing/config.py, benchmark/run.py, benchmark/merge.py,
tests/test_subset_merge.py, docs/DECISIONS.md and docs/BENCHMARK.md were edited for
this brief. Existing working-tree changes were preserved. No real calls, actual
calibration/subset run, historical results edits, key inspection, commits,
pushes or branch changes were performed.

The new offline tests in tests/test_subset_merge.py are:

- test_subset_run_executes_only_requested_ids_all_conditions_same_seed_and_manifest
- test_subset_refuses_existing_output_before_storage_or_provider
- test_subset_exclusive_creation_rejects_output_race
- test_subset_rejects_invalid_ids_and_other_splits
- test_benchmark_uses_60_seconds_and_live_keeps_10_seconds
- test_configured_benchmark_deadline_applies_to_every_condition
- test_config_default_and_invalid_benchmark_deadlines
- test_merge_replaces_only_subset_prompts_and_records_provenance
- test_merge_preserves_all_attempt_charges_including_superseded_unknown
- test_merge_never_changes_either_input
- test_merge_refuses_existing_output_including_input_paths
- test_merge_rejects_incompatible_metadata_and_missing_attempts
- test_merge_rejects_empty_unknown_prompts_and_duplicate_charges


Verification for the subset/deadline/merge brief:

- `./.venv/Scripts/python.exe -m unittest tests.test_subset_merge -v`: 13 tests,
  OK (1.383 s), synthetic mock fixtures only.
- `./.venv/Scripts/python.exe scripts/manage.py test`, run 1: 192 tests, OK,
  no skips (46.200 s).
- Same full suite, run 2: 192 tests, OK, no skips (44.908 s). Python source
  hashes were unchanged throughout the second run.
- Both full suites exported
  `ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router`
  and `ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0`. The response journal was
  redirected to a temporary path; no real provider calls were made.
- `./.venv/Scripts/python.exe scripts/manage.py lint`: passed, 57 Python files.
- `git diff --check` passed for the permitted tracked paths. CLI help exposes
  `--prompt-ids-file` and the three-path merge command.
- Hash comparison against the starting workspace found exactly the seven allowed
  paths changed, with no other tracked/untracked source changes or deletions.
- All requested offline checks completed. Real-provider behavior and actual
  calibration/subset execution were intentionally not verified, as prohibited
  by the brief. Historical results, PLAN.md, keys and git history were untouched.


## Benchmark output limit: 1024 for all conditions (2026-10-17)

The user selected `benchmark_max_tokens: 1024` after **70 of 88 subset outputs
were truncated at 512 tokens**. Truncated answers are non-completed and score 1
under the predeclared quality rule, leaving too few completed A/B pairs. Raising
the matched limit to 1024 gives every condition the same additional output room;
it does not establish that truncation is eliminated or that quality passes.

The setting accepts integers in 1..2048 and defaults to 1024 for configurations
without the key. `benchmark.run` applies it to A, B, C-cold and C-warm and records
it in run metadata and the frozen configuration. The same request carries the
limit into worst-case spend reservation and provider dispatch, including fallback
attempts. The live API default remains 512. Models, temperature, prices, caps,
deadline and SLO are unchanged. PLAN.md and historical result files are unchanged.

The subsequent full rerun uses 1024 for every condition. This change performs
only mock tests and local service checks; no real calls or calibration/subset
execution are authorized by this brief.


Verification for the benchmark output-limit brief:

- New mock-only tests in `tests/test_benchmark_max_tokens.py`:
  `test_benchmark_requests_use_1024_for_all_conditions`,
  `test_configured_limit_reaches_every_condition_and_provider`,
  `test_live_default_remains_512_with_benchmark_config`,
  `test_config_default_and_valid_boundaries`,
  `test_config_rejects_out_of_range_and_noninteger_limits`, and
  `test_reservation_matches_provider_limit_including_fallback`.
- Targeted test module: 6 tests, OK (0.176 s).
- Full suite run 1: 198 tests, OK, no skips (26.869 s).
- Full suite run 2: 198 tests, OK, no skips (28.918 s).
- Both full runs used `./.venv/Scripts/python.exe scripts/manage.py test` with
  `ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router`
  and `ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0` exported. The response
  journal and suite logs were redirected to a temporary directory:
  `C:/Users/nsena/AppData/Local/Temp/sol-maxtokens-hympv9ci` (suite-1.log and
  suite-2.log).
- `./.venv/Scripts/python.exe scripts/manage.py lint`: passed, 58 Python files.
- Workspace hash comparison found exactly the six permitted paths changed;
  existing working-tree changes were preserved. No source files were deleted.
- All requested offline checks completed. Real-provider behavior and the full
  rerun were intentionally not verified. No real calls, actual calibration/subset
  run, result-file edits, key inspection, commits, pushes or branch changes occurred.


## Blinded calibration export: complete A/B pairs only (2026-10-17)

Calibration candidates now require both A and B records to have explicit
`completed=True` and HTTP `status=200`. Missing, truncated, failed and
completion/status-unknown sides exclude the pair before seeded selection.
The existing shuffled tier/task-type buckets and round-robin selection still
choose up to 25 pairs. Selection stops when candidates are exhausted, exports
all available complete pairs, and reports their actual `pairs` and
`individual_ratings` counts with the seed. Zero complete pairs raises a clear
error without creating an output directory.

The public JSON keeps its original fields and no condition or completion
metadata. Missing content uses an empty string instead of the old completion
label. The private mapping retains blind_id, prompt_id, left_condition, tier
and task_type and adds `left_completed` and `right_completed`, aligned with
the randomized sides. Held-out manifest selection is preserved. An existing
output directory is refused before dataset loading; exclusive directory
creation also refuses a competing creation before either file is written.

An offline comparison against the original exporter, using 60 fully complete
synthetic pairs and seed 13, confirmed identical public JSON bytes, selected
prompt IDs and left/right assignments; the private mapping differs only by
its new completion fields. Synthetic fixtures include no provider dispatch.

New regression tests in `tests/test_ratings_complete_pairs.py`:

- `test_incomplete_pair_is_never_selected`
- `test_both_sides_require_explicit_completion_and_http_200`
- `test_missing_side_and_other_splits_are_not_candidates`
- `test_private_mapping_records_completion_and_public_schema_stays_blinded`
- `test_missing_content_never_adds_a_public_completion_label`
- `test_existing_output_directory_is_refused_without_changes`
- `test_output_directory_creation_race_is_refused`
- `test_fewer_than_25_complete_pairs_export_all_and_report_actual_count`
- `test_no_complete_pairs_raise_clear_error_without_creating_output`
- `test_seed_is_repeatable_and_selection_is_stratified_and_capped`
- `test_held_out_keeps_manifest_selection_and_maps_each_sides_completion`

The existing `ToolingTests.test_blinded_calibration_export_without_dispatch`
in tests/test_tooling.py conflicts with this brief: it exports into an already
existing temporary directory and supplies neither completion flags nor HTTP
status. It must use a new child directory and explicit complete/successful
fixture records to satisfy the revised contract. That file is outside the
three permitted paths, so it was left unchanged; the implementation does not
weaken the new requirements to accommodate the stale fixture.

Only benchmark/ratings.py, tests/test_ratings_complete_pairs.py (new), and
this decision log were edited for this brief. Existing workspace edits were
preserved. No real calls, exports of real results, historical results edits,
API key inspection, git commits, pushes or branch changes were performed.
PLAN.md was not modified. Actual calibration export remains for the main chat.


Verification for complete-pair blinded exports (final source):

- Targeted module: 11 tests, OK (0.228 s), all synthetic and offline.
- Full suite, final run 1: 209 tests, 208 passed, 1 error, no skips (41.781 s).
- Full suite, final run 2: 209 tests, 208 passed, 1 error, no skips (42.399 s).
- Both full runs used ./.venv/Scripts/python.exe scripts/manage.py test with
  ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router
  and ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0 exported. Response
  journals were redirected to temporary paths; real-spend confirmation was NO.
- All 11 new tests passed in each final full run. Both errors are the same
  out-of-scope legacy export fixture documented above, at tests/test_tooling.py:41.
  Full-suite green status is therefore not achieved within the permitted scope.
- ./.venv/Scripts/python.exe scripts/manage.py lint: passed, 59 Python files.
- git diff --check passed for the permitted tracked paths. Benchmark/test
  Python source hashes stayed unchanged throughout both final suite runs.
  Workspace hash comparison found exactly the three permitted paths changed,
  with no tracked/untracked source deletions or other source modifications.
- Final logs: C:/Users/nsena/AppData/Local/Temp/sol-ratings-complete-4m1bxys1/suite-final-1.log
  and suite-final-2.log in the same directory.
- All requested commands were run. Real-provider behavior and actual exports
  were not exercised, as required. Correcting the legacy test requires a
  separate change to tests/test_tooling.py, which this brief does not authorize.


## Judge calibration CLI for run 5 (2026-10-17)

Added `benchmark/calibrate_judge.py` for the 25 rated pairs, with a default
zero-call dry run. Joining validates blind IDs, prompt/source content, completion,
strata and the frozen dataset checksum. A left-side condition B correctly maps
its rating to human_b. Exactly 25 valid pairs are required; unrated conditions
are never dispatched.

The CLI uses the existing guarded real-provider factory, shared Postgres ledger
and Redis admission, and Quality's frozen rubric/request construction and spend
settlement. The current configured judge must be mid; current global and
at-most-USD-3 evaluation caps apply. All calls carry purpose evaluation and the
original answer's parent request ID. A distinct stable calibration attempt ID
prevents paid replay when terminal jobs already exist.

A strict response adapter rejects any shape except one integer score in 1..5,
including duplicate keys and abnormal stop reasons. On malformed output it
preserves usage and supplies a missing-score object to Quality so settlement
completes before the job becomes quality_unknown. This introduces no substitute
rating. Reading a completed score from the ledger's REAL column converts only
an exact integer-valued 1..5 back to the validator's integer representation;
raw provider strings, floats and booleans are never coerced.

Pair and report paths are claimed exclusively before dispatch. Unknown scores
remain null in the pair list and receive pair/side/attempt identities in the
report. validate_calibration is always called after scoring; the CLI exits 1
for unknowns or a failed statistical gate. A passing judge is established only
by a future real calibration run, never by the synthetic tests in this change.

Only benchmark/calibrate_judge.py, tests/test_calibrate_judge.py, docs/DECISIONS.md
and docs/BENCHMARK.md were edited. Existing workspace edits were preserved.
PLAN.md, the frozen rubric and historical results/ratings/mapping were not
modified. No API key values were read, real calls made, commits/pushes performed
or branches changed.

New offline tests in tests/test_calibrate_judge.py:

- test_left_b_maps_human_ratings_by_condition_and_blind_id
- test_only_50_rated_outputs_and_no_condition_or_human_metadata_reach_judge
- test_strict_json_rejects_malformed_missing_and_coercible_scores
- test_malformed_response_is_unknown_and_usage_remains_charged
- test_evaluation_cap_blocks_dispatch_with_existing_evaluation_spend
- test_global_cap_blocks_dispatch
- test_dry_run_makes_zero_calls_and_creates_no_outputs
- test_existing_output_or_report_is_refused_before_provider_setup
- test_output_creation_race_refuses_before_calls_and_removes_partial_claim
- test_real_guard_requires_confirmation_before_storage_or_calls
- test_mocked_pipeline_writes_pairs_and_validator_report_using_configured_caps
- test_unknown_pair_writes_failed_report_with_pair_identity
- test_invalid_joins_ratings_and_changed_content_are_rejected
- test_cli_dry_run_and_failed_validation_exit_codes


Verification for the judge-calibration brief:

- Targeted offline module: 14 tests, OK (0.526 s).
- Full suite run 1: 223 tests, OK, no skips (31.702 s).
- Full suite run 2: 223 tests, OK, no skips (48.423 s).
- Both used `./.venv/Scripts/python.exe scripts/manage.py test`, with
  `ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router`
  and `ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0` exported. Real-spend
  confirmation was NO, bytecode writes disabled, and response journals isolated
  in temporary paths. Provider calls in the new tests are mocked.
- Docker compose inspection was denied by the environment's Docker pipe/config
  permissions. Direct Postgres connection and Redis ping both succeeded before
  test execution; all enabled local-infrastructure tests passed.
- `./.venv/Scripts/python.exe scripts/manage.py lint`: passed, 61 Python files.
- `./.venv/Scripts/python.exe -m benchmark.calibrate_judge` printed exactly
  `{"dry_run": true, "pairs": 25, "planned_calls": 50}` and created no outputs.
- Scoped `git diff --check` passed. Workspace source hashes showed exactly the
  four permitted files changed, with no deletions or other source changes.
- Logs: C:/Users/nsena/AppData/Local/Temp/sol-judgecal-_rr8j6l3/suite-1.log
  and suite-2.log in the same directory.
- All requested offline checks completed. Actual real-provider scoring and the
  resulting statistical calibration remain unverified, as required: the CLI
  was never run with --real and no real calls were made. No calibration result
  files were produced or overwritten.


### Judge cancellation ownership and unresolved-only reruns (2026-10-17)

The calibration timeout exposed a cancellation race at the direct SDK await in
`LiteLLMProvider.complete` (`fallback/providers.py:73`, now the owned-task creation).
The installed LiteLLM source in `.venv-real/Lib/site-packages/litellm/main.py:675`
awaits `run_in_executor` to obtain a provider coroutine, then awaits that coroutine
at line 681. Cancelling the outer call during the executor handoff can discard
its returned `OpenAIChatCompletion.acompletion` coroutine before it is awaited.
An offline fixture reproduces this executor/factory boundary without importing
LiteLLM or making a network call. The previous quality logger used `str(exc)`;
`TimeoutError()` and `CancelledError()` have empty messages, explaining the empty
reason. The calibration then sent missing scores to the ordinary rating validator,
which obscured the incomplete judge job as invalid ratings.

Changes are confined to these six allowed files:

- `fallback/providers.py`: own and shield the SDK task, drain its executor handoff
  and retrieve late exceptions before re-raising cancellation. The SDK timeout,
  retry prohibition, real-spend confirmation, and request bounds remain enforced.
  Cleanup can extend beyond the outer judge timeout while the SDK finishes; a late
  result is discarded on cancellation and cannot become a successful score.
- `monitoring/quality.py`: pass an owned provider task into the health wrapper,
  cancel/drain all owned tasks, and retain exception classes in nonempty redacted
  reasons. Durable reasons belong to jobs through a companion `judge_job_reasons`
  table keyed by attempt ID. Using companion metadata preserves unrelated runs'
  job rows and their schema during scoped recovery. Storage outages retain the
  failure in memory for the report when durable writes are unavailable.
- `benchmark/calibrate_judge.py`: report `judge incomplete (N calls unresolved)`
  whenever scoring leaves unknown outputs. Fully scored pairs still pass through
  the unchanged validator, including its invalid-rating behaviour. On cancellation,
  write partial pairs and reasons for current/unstarted jobs before re-raising.
  Dry runs remain read-only even when the default result files already exist.
  Add unresolved-only reruns, validate their previous pair/report identities,
  preserve all completed scores, and assign fresh UUIDs to retry calls. Reserve
  and dispatch still use Quality's original admission and spend controls.
- `benchmark/recover.py`: add a run-scoped recovery reason for incomplete jobs,
  without replacing existing reasons. Existing recovery still marks old paid
  attempts unknown at their entire reserved charge and never replays them.
- `tests/test_calibrate_judge.py`: mock-only regression coverage listed below.
- `docs/DECISIONS.md`: record the diagnosis, operator path and verification.

A rerun requires both a new pair output and a new report. All selected parents
are checked before any new reservation: any reserved/dispatched evaluation
attempt or active previous job blocks it and directs the operator to run-scoped
`benchmark.recover`. The stuck production attempt
`602d6cab-e945-55bb-b9c0-debb41709b2d` was not modified or recovered by this task;
its USD 0.0005764 reservation must remain charged when the operator recovers it.
The old pair/report files, ratings, mapping and PLAN.md remain untouched.

Operator example after the source run has been recovered (documented only;
neither recovery nor this real rerun was executed against production):

```powershell
./.venv/Scripts/python.exe -m benchmark.calibrate_judge --real --rerun-unresolved results/judge-calibration-run5.json --output results/judge-calibration-run5-rerun.json
```

The previous report defaults to the previous pair filename with `-report.json`;
use `--rerun-report` if it has a custom path. The new report is generated next to
the new output and records `rerun_of`. Legacy pair outputs without attempt fields
are supported using the previous report's unresolved attempt identities. An
existing output/report is never overwritten by a real run.

New regression tests (in addition to the original 14 calibration tests):

- `test_litellm_executor_handoff_is_drained_even_on_repeated_cancellation`
- `test_timeout_drains_every_judge_call_and_reports_persisted_reason`
- `test_probe_cancelled_before_awaiting_provider_leaves_no_coroutine`
- `test_cancelled_calibration_drains_call_and_writes_partial_report`
- `test_blank_exception_has_class_in_job_and_report`
- `test_rerun_only_unresolved_requires_recovery_and_keeps_reserved_charge`
- `test_rerun_dry_plan_accepts_legacy_outputs_and_refuses_overwrite`
- `test_genuinely_invalid_ratings_keep_validator_behavior`
- `test_dry_run_with_existing_outputs_leaves_them_untouched`
- `test_storage_outage_still_writes_reason_without_dispatch`

The rerun regression models 49 successful calls and one stuck dispatched attempt,
asserts zero retry calls before recovery, runs recovery only on a synthetic local
ledger, and then proves exactly one new attempt/call plus all 50 final scores.
It verifies unchanged original outputs, unchanged completed scores/attempt IDs,
51 total charged attempts, and retention of the stuck attempt's full reserve.
The coroutine tests assert completed/cancelled ownership, closed coroutine frames,
and absence of never-awaited warnings. Targeted calibration plus run-isolation
recovery tests: 34 tests passed, no skips (18.020 s).


Final verification for the cancellation/rerun fix:

- Full suite run 1: 233 tests, OK, no skips (59.912 s).
- Full suite run 2: 233 tests, OK, no skips (58.259 s).
- Both ran `./.venv/Scripts/python.exe scripts/manage.py test` with
  `ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router`
  and `ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0` exported. Real-spend
  confirmation was NO, bytecode writes disabled, and response journals were
  directed to temporary paths. Infrastructure tests use isolated test schemas;
  all new provider interactions are in-process mocks.
- `./.venv/Scripts/python.exe scripts/manage.py lint`: passed, 61 Python files.
- `./.venv/Scripts/python.exe -m benchmark.calibrate_judge` printed exactly
  `{"dry_run": true, "pairs": 25, "planned_calls": 50}`. Existing calibration
  outputs remained in place; no output files were created or overwritten.
- Scoped `git diff --check` passed. Only the six permitted source/documentation
  files listed above were edited; pre-existing workspace changes were retained.
- Full-suite logs: `C:/Users/nsena/AppData/Local/Temp/sol-judge-fix-final-suite-1.log`
  and `C:/Users/nsena/AppData/Local/Temp/sol-judge-fix-final-suite-2.log`.
- Not verified, as required by the brief: actual provider scoring, production
  recovery of the stuck attempt, and the paid unresolved-only rerun. The real
  CLI and production recovery were never invoked. Recovery/rerun behaviour was
  exercised only with synthetic ledgers and mock providers in tests. API key
  values were not read or printed. No commits, pushes or branch changes occurred.


### Calibration judge timeout (2026-10-17)

The calibration judge now uses `judge_calibration_timeout_s`, default 30 seconds,
configured in `config/models.json` and validated by `routing.config.Config` as an
integer from 1 through 60 (booleans and coercible strings/floats are rejected).
Missing settings in older configs also default to 30 seconds.

The reason for 30 seconds is two roughly 10-second timeouts for simple-034 side a
(attempts beginning `602d6cab` and `13827bc0`) on a 36-character answer while the
other 49 calls completed. This suggests the previous effective timeout was too
tight for the mid judge, `openai/gpt-4.1-mini`; it does not establish the provider's
root cause or guarantee success at 30 seconds.

Calibration passes this setting to the existing `Quality(timeout=...)` argument,
which controls both the provider timeout and the outer asyncio wait. No change
to `monitoring/quality.py` is needed: its existing timeout argument is sufficient.
Live and ordinary benchmark judge defaults remain 5 seconds. The real-call flag,
spend confirmation, evaluation cap, ledger admission/settlement, and unresolved
rerun guards are unchanged.

Offline regression tests added in `tests/test_calibrate_judge.py`:

- `test_calibration_timeout_defaults_to_30`
- `test_calibration_timeout_rejects_out_of_range_and_non_integer_values`
- `test_default_calibration_timeout_reaches_judge_call`
- `test_configured_calibration_timeout_reaches_provider_and_outer_wait`
- `test_live_and_benchmark_judge_timeouts_remain_5_seconds`

The existing cancellation test overrides the new explicit timeout with its short
mock-only timeout. All 29 calibration tests pass. Lint passes for 61 Python files.
The no-flag dry run prints
`{"dry_run": true, "pairs": 25, "planned_calls": 50}`.
No real provider calls, production recovery, or real reruns are performed.

Full-suite verification using `./.venv/Scripts/python.exe scripts/manage.py test`:

- Run 1: 238 tests passed, no skips (57.843 s).
- Run 2: 238 tests passed, no skips (31.228 s).
- Both runs exported
  `ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router` and
  `ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0`, disabled bytecode writes,
  set real-spend confirmation to NO, and used temporary response journals.
  Infrastructure tests use isolated test schemas; provider calls are mocks.
- Scoped `git diff --check` passed. Changes for this task are limited to
  `config/models.json`, `routing/config.py`, `benchmark/calibrate_judge.py`,
  `tests/test_calibrate_judge.py`, and `docs/DECISIONS.md`.
- Not verified: real-provider timing/scoring and paid reruns, as required by the
  brief. No API key values were read or printed; no commits, pushes or branch
  changes were made.


### Constant-5 investigation and opt-in rubric v2 (2026-10-17)

This is an offline diagnosis and implementation, not evidence that a revised
judge is calibrated. The existing `results/judge-calibration-run5-rerun2.json`
contains 25 pairs: all 50 judge scores are 5, while the individually recorded
human scores have values 1, 3, 4 and 5. No existing result, rating, mapping or
production ledger row was changed.

Diagnosis from the request path and frozen rubric:

- A fallback/default score of 5 is ruled out by the code. Calibration's
  `parse_score` requires exactly one integer score in 1..5; `StrictJudgeProvider`
  rejects malformed JSON, duplicate keys and non-normal stop reasons. Quality
  settles usage before reading `parsed["score"]`; missing/invalid scores become
  quality_unknown, never 5. The archived score rows record parsed scores, not
  archived raw provider text, so the exact original wording cannot be recovered.
- V1 anchoring/leniency is plausible, but not proven causal. Its examples for
  scores 1, 3 and 5 all concern 2+2, and its 5 example is a very short arithmetic
  explanation. Actual rated tasks include code plus cancellation analysis,
  distributed rate limiting plus atomicity/clock skew, and edit-distance
  recurrence plus path reconstruction. These simple examples may underemphasize
  checking every requested part. V1 already explicitly penalizes wrong, missing
  and truncated answers and asks for compliance with constraints; it would be
  incorrect to diagnose the complete absence of those instructions.
- JSON escaping as data loss is ruled out: Quality uses json.dumps on the whole
  conversation and answer, without slicing; json.loads recovers escaped quotes,
  newlines and Unicode. Escapes are representation, not double encoding of a
  missing answer. Reduced readability or model misinterpretation remains a
  plausible presentation issue; it is not established as the cause of 50 fives.
- Missing completion signals are a genuine limitation of the request shape:
  only messages and answer are supplied. Calibration rebuilds Output from the
  answer text with no original stop reason. However, load_pairs rejects records
  whose completed flag is not true, status is not 200 or content is not a string.
  All 50 selected source records are marked completed; none is empty/whitespace,
  and the source records have no stop_reason field. Thus actual empty answers
  and records flagged incomplete are ruled out for this run. A semantically
  unfinished answer that nevertheless has a normal completion flag remains
  possible and must be judged from its text and the task requirements. The
  completed flag alone does not prove that all requested work was delivered.
- max_tokens=32 limits the judge's output, not its supplied answer. It leaves
  little room for a written rationale, but the rubric requires JSON only and
  the score JSON fits. An exhausted/non-normal judge completion becomes unknown
  through the strict adapter rather than a default 5. A reasoning-budget effect
  is plausible but unproven; neither absence of internal reasoning nor a causal
  link to constant scores can be inferred offline. The token limit is unchanged.

Implementation is confined to monitoring/judge_prompt_v2.md,
benchmark/calibrate_judge.py, monitoring/quality.py, tests/test_judge_v2.py and
this decision log. V1 and PLAN.md remain unchanged. V2 uses examples from the
actual tasks (average of 12/18/24, deadline-bounded retries, synchronous versus
asynchronous execution), requires checking all stated constraints, and limits
empty, off-topic, wrong or truncated answers to 1 or 2. It retains the integer
1..5 scale and exactly {"score": integer} output. Neither a model identity nor
a condition label is added to the rubric or judge payload.

`--rubric v1` remains the default. Quality accepts optional rubric text; its
ordinary live/benchmark default still reads frozen v1. Calibration snapshots the
chosen file before calls and writes `rubric` and `rubric_sha256` (SHA-256 of exact
file bytes) into every new pair row and the report, including incomplete runs.
The no-option v1 dry-run output is unchanged. The v2 dry run also exposes its
version/hash. V1 attempt UUIDs are preserved; v2 uses a separate version/hash
namespace so old completed v1 jobs cannot masquerade as fresh v2 scores. Within
a rubric, the existing idempotent reservation/settlement path remains in use.
Unresolved-only reruns reject a version/hash mismatch, rather than mixing old
scores with a new rubric; legacy unversioned reruns are accepted only as v1.
A v2 comparison therefore needs fresh scoring of all 50 answers and new output
paths; it must not be an unresolved-only rerun of completed v1 results.

`--raw-debug N` defaults to zero and captures the first N actually dispatched
judge calls' returned text before strict parsing, without stripping whitespace
or replacing malformed output. New `<output-stem>-raw-debug.jsonl` rows include
the call index, blind/prompt/side/attempt identity and raw_text. The report/plan
identifies the capture path. A call that raises before returning has no raw text
and still consumes one of the first N call positions. All requested output
paths are claimed exclusively before ledger setup/dispatch; a creation race
rolls back partial claims. Diagnostic write/flush errors are logged and cannot
skip parsing, alter the score, or prevent usage settlement. No debug file is
created by a dry run. Existing confirmation, caps, admission, recovery checks,
strict parsing, cancellation ownership and unknown-score behavior are retained.

V2 is chosen AFTER seeing v1 on the same 25 pairs. Recalibration on those pairs
is adaptive development and is not a clean validation of v2. Even a subsequent
statistical gate pass on these reused pairs would not remove that limitation;
independent, predeclared human-rated data are needed for a clean validation.
This change does not declare the judge validated or enable a quality claim.

Mock-only tests in tests/test_judge_v2.py:

- test_rubric_selection_reaches_system_message_without_metadata
- test_rubric_hash_uses_exact_file_bytes
- test_rubric_and_hash_recorded_in_every_output_row_and_report
- test_default_v1_preserves_request_and_attempt_identity
- test_cli_defaults_and_explicit_selection
- test_raw_debug_writes_first_n_raw_text_without_changing_scores_or_costs
- test_raw_debug_preserves_malformed_text_and_unknown_settled_usage
- test_debug_write_failure_does_not_change_score_or_settlement
- test_raw_debug_preserves_abnormal_stop_response_before_rejection
- test_dry_run_v2_with_debug_creates_no_files_or_provider
- test_existing_debug_file_is_never_overwritten
- test_debug_creation_race_rolls_back_before_ledger_or_dispatch
- test_real_spend_guard_remains_required_with_v2_and_debug
- test_v2_cannot_reuse_cached_v1_scores_and_remains_idempotent
- test_unresolved_rerun_rejects_different_rubric_or_hash
- test_invalid_rubric_and_negative_debug_fail_before_provider
- test_v2_contains_truncation_rule_constraints_and_task_examples


Verification for the versioned-rubric brief:

- Targeted new and existing calibration modules: 46 tests passed, no skips
  (5.783 s), including all 17 new tests named above. All judge calls were mocks.
- Full suite run 1: 255 tests passed, no skips (31.271 s).
- Full suite run 2: 255 tests passed, no skips (31.728 s).
- Both full runs used `./.venv/Scripts/python.exe scripts/manage.py test` with
  ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router and
  ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0 exported. Bytecode writes were
  disabled, real-spend confirmation was NO, and response journals were directed
  to temporary files. Infrastructure tests used their isolated test stores;
  no production ledger recovery or updates were performed.
- `./.venv/Scripts/python.exe scripts/manage.py lint` passed: 62 Python files.
- Required dry run, `./.venv/Scripts/python.exe -m benchmark.calibrate_judge --rubric v2`,
  printed the following JSON and did not create result/report/debug files:

```json
{"dry_run": true, "pairs": 25, "planned_calls": 50, "rubric": "v2", "rubric_sha256": "43747507d923b5bc0d1866a045772cb806d72a62f4655055ab947383f3a792bd"}
```

- The no-option v1 dry run still printed exactly
  `{"dry_run": true, "pairs": 25, "planned_calls": 50}`.
- Scoped git diff --check passed, with new/untracked files also checked for
  trailing whitespace and tabs. The workspace hash comparison confirmed frozen
  v1 and PLAN.md unchanged and no deletions. Our edits are the five allowed
  paths. A concurrent external .gitignore edit added `.venv-real/` at 08:49:29
  local time, after the 08:47:08 baseline; none of this task's write commands
  targeted .gitignore, and no source/test code refers to it. That edit was left
  untouched. Ignored historical result files were not included in the source
  hash inventory; preservation of those files follows from the read-only CLI
  dry runs and temporary mock outputs, rather than a baseline hash comparison.
- Verification logs: C:/Users/nsena/AppData/Local/Temp/sol-judge-v2-9t9exn0q/
  targeted.log, suite-1.log and suite-2.log.
- Not verified: the actual provider's reason for returning only 5, whether v2
  improves real scoring, real raw-debug responses, paid calibration, production
  ledger behavior under a new real run, or independent statistical validation.
  No real calls were made. API key values were not read or printed. Existing
  results, ratings, mapping and production ledger rows were not modified; no
  commit, push or branch change was performed.


### Brief reasoning rubric v3 and calibration output limit (2026-10-17)

V3 was chosen AFTER seeing both v1 and v2 return 5 for all 50 rated outputs
on the same 25 pairs. This is adaptive development on reused human-rated data,
not a clean validation. Even a gate pass on these pairs would not remove that
limitation; independent, predeclared human-rated data are needed for clean
validation. No judge validation or quality claim is established by this change.

The judge remains the configured mid model, openai/gpt-4.1-mini. V3 keeps v2's
1..5 scale and all anchors, asks for one or two brief sentences checking the
answer against the prompt (including empty, cut-off, off-topic or wrong answers),
then an integer score. Its strict JSON shape is
`{"reasoning": "<one or two sentences>", "score": <integer 1..5>}`.
V1 and v2 rubric files are unchanged. `--rubric v3` is opt-in; v1 remains the
default. Existing exact-file-byte SHA-256 provenance is recorded in new pair
rows and reports; v3 uses its own version/hash attempt namespace and cannot
mix rubric versions or hashes in an unresolved-only rerun.

`judge_calibration_max_tokens` is 256 in config/models.json. Config validates
an integer in 1..512 without coercion (including rejecting booleans), and older
configs without this setting also default to 256. Only benchmark.calibrate_judge
passes it to Quality. Both reservation and provider request use the same limit,
for every calibration rubric. Live and ordinary benchmark judges retain their
32-token limit and five-second timeout. Calibration retains its configured
30-second timeout, spend guards, caps, settlement and cancellation behavior.

Calibration parsing accepts the legacy score-only JSON or reasoning/score JSON.
It takes the score only from the integer `score` field in 1..5, never from
reasoning or a fallback. Invalid JSON, duplicate keys, nonstandard JSON constants,
extra fields/text, strings/floats/booleans used as scores and abnormal stop reasons
are rejected. Optional reasoning must be text; its contents never change the
score. Malformed returned calls still settle their original usage before being
recorded as quality_unknown. Reasoning is saved as `reasoning_a`/`reasoning_b`
in new pair outputs for diagnostics only and carried forward in unresolved
reruns. It is not used by the calibration gate. Existing idempotent completed
jobs are not replayed to reconstruct missing diagnostic text.

`--raw-debug N` still preserves exact returned raw text for the first N
actually dispatched calls and now includes a separate `reasoning` field (null
when no valid reasoning/score object is available). Diagnostic write errors
cannot change scores or usage settlement. Dry runs create no files and do not
initialize providers or ledger connections.

All new tests in tests/test_judge_v3.py use synthetic fixtures, temporary local
ledgers and mock providers. No paid calls, production recovery or paid reruns
are part of this work. Existing results, ratings, mapping and production ledger
rows are not modified. PLAN.md is unchanged. No API key values are read or
printed, and no commits, pushes or branch changes are made.

Mock-only regression test names in `tests/test_judge_v3.py`:

- `test_calibration_limit_default_and_integer_bounds`
- `test_all_calibration_rubrics_use_256_for_reservation_and_request`
- `test_configured_calibration_limit_reaches_reservation_and_request`
- `test_live_and_benchmark_judges_keep_32_for_reservation_and_request`
- `test_strict_parse_accepts_v3_and_reasoning_never_changes_score`
- `test_strict_parse_rejects_invalid_json_scores_extra_text_and_duplicates`
- `test_v3_selection_hash_and_reasoning_recorded_in_rows_report_and_debug`
- `test_invalid_v3_is_unknown_but_preserves_raw_text_and_settles_usage`
- `test_v3_debug_failure_does_not_change_score_or_settlement`
- `test_v3_dry_run_creates_no_files_or_provider`
- `test_cli_accepts_v3`
- `test_strict_adapter_resets_diagnostics_between_calls_and_rejects_cutoff`
- `test_v3_uses_fresh_attempts_and_rejects_mixed_rerun_provenance`
- `test_v3_preserves_scale_and_anchors_and_requires_brief_reasoning_first`

Verification using `./.venv/Scripts/python.exe`:

- Targeted new module: 14 tests passed, no skips (0.764 s).
- Full suite run 1: 269 tests, no skips (32.505 s); FAILED with three assertion
  failures across two existing test methods (267 methods passed).
- Full suite run 2: 269 tests, no skips (31.591 s); the identical three assertion
  failures across the identical two existing methods (267 methods passed).
- Both full runs used `scripts/manage.py test` with
  `ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router` and
  `ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0` exported, disabled bytecode
  writes, set real-spend confirmation to NO, and used temporary response journals.
  Infrastructure tests use isolated test stores; new provider calls are mocks.
- The failures are `test_only_50_rated_outputs_and_no_condition_or_human_metadata_reach_judge`
  at tests/test_calibrate_judge.py:153, and both v1/v2 subtests of
  `test_rubric_selection_reaches_system_message_without_metadata` at
  tests/test_judge_v2.py:69. Each still asserts calibration max_tokens == 32;
  the new required limit is 256. Those files are outside this brief's allowed
  scope and are deliberately unchanged. The full suite is therefore NOT green;
  those three assertions need updating in a separately authorized scope.
- `scripts/manage.py lint` passed: 63 Python files.
- `-m benchmark.calibrate_judge --rubric v3` completed without `--real` and
  printed exactly:

```json
{"dry_run": true, "pairs": 25, "planned_calls": 50, "rubric": "v3", "rubric_sha256": "9d784ca89f52d4a26f0f997b84f6b768126f6ec26e7aaf1e70da7305da7c57be"}
```

- The default v1 dry run still printed exactly
  `{"dry_run": true, "pairs": 25, "planned_calls": 50}`.
- Git diff checks confirm PLAN.md, frozen v1/v2 rubrics and the two older test
  files are unchanged. Only the seven permitted paths are changed:
  monitoring/judge_prompt_v3.md, config/models.json, routing/config.py,
  benchmark/calibrate_judge.py, monitoring/quality.py, tests/test_judge_v3.py
  and docs/DECISIONS.md. Scoped whitespace checks passed.
- Full-suite logs are in
  `C:/Users/nsena/AppData/Local/Temp/sol-judge-v3-20261017/suite-1.log` and
  `C:/Users/nsena/AppData/Local/Temp/sol-judge-v3-20261017/suite-2.log`.
- Not verified: real-provider scoring, whether v3 or the larger output limit
  improves calibration, paid reruns, and independent statistical validation.
  No real calls were made, as required.
