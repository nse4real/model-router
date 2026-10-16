# PLAN: Model Router for Cost-Optimised AI Pipelines

Version 3 (revised after adversarial review, rounds 1 and 2).
Source spec: `Project_13_Model_Routing_System.pdf` (Project 13 / 14).
Repo: `model-router` on github.com/nse4real, public, MIT licence.
This folder is the repo root. The spec's folder layout sits at the root.

## 1. Goal

A FastAPI service that receives a chat-style request and picks a model:
- Simple requests go to a cheap, fast model.
- Complex requests go to a frontier model.
- Routing respects a cost budget, a latency deadline and a quality floor.
- Every paid call is logged and visible on a Grafana dashboard.

Portfolio claim, stated precisely: on a frozen held-out benchmark, the router reduces total spend per prompt versus an "always frontier" baseline, and routed quality is within a predeclared non-inferiority margin of the baseline. The 40-60% figure in the spec is a hypothesis. The README reports the measured number with confidence intervals, whatever it is. If quality non-inferiority fails or the judge does not validate, the README says the quality claim is unsupported.

## 2. Decisions

| Topic | Decision |
|---|---|
| Model calls | Mock providers for development and tests. Real calls only in calibration, smoke test and benchmark. |
| Budget | Under £30 total. Hard cap of **USD 25** on real API spend, enforced in code (section 5.1). Confirm GBP conversion before the held-out run. |
| Ollama | Optional. Verified only if the user runs the opt-in test. Never required by tests. Not installed yet. |
| Local tooling | Docker Desktop, Python 3.11+, Git. Redis, PostgreSQL, Prometheus, Grafana via `docker-compose.yml`. |
| Quality | User rates 50 samples for calibration. Judge may affect reported quality only if validated. Judge never affects routing during the benchmark. |
| Benchmark data | Public base set (licence verified and recorded in `docs/`) plus about 150 hand-written prompts. Frozen into dev, calibration and held-out splits. |
| Scope | All four spec milestones. |

### 2.1 Spend allocation (USD)

| Activity | Cap |
|---|---|
| Smoke tests and provider checks | 2 |
| Calibration outputs (50 rated samples) and judge scoring of them | 3 |
| Dev split (tuning) | 3 |
| Held-out benchmark (A, B, C-cold, C-warm) | 12 |
| Judge scoring of held-out pairs | 3 |
| Reserve (one re-run of a failed or partial step) | 2 |
| **Total** | **25** |

Before the held-out run, a preflight step computes expected cost from measured per-prompt costs on the dev split. If the estimate exceeds the held-out allocation, the held-out sample size is reduced before the run starts, and the reduction is recorded in the results file.

## 3. Architecture

```
client -> api (FastAPI) -> validation -> classifier -> policy engine
                                                          |
                                cache lookup (exact key) -+-> hit: return, ledger row (cost 0)
                                                          |
                          spend admission (Postgres, durable reservation before dispatch)
                                                          |
                                     fallback chain (deadline-bound) -> LiteLLM -> provider
                                                          |
                        settlement + attempt ledger (Postgres), Prometheus metrics
                        async quality sampler -> judge queue (never blocks response)
```

## 4. Contracts

### 4.1 API contract
- `POST /v1/route`: body `messages` (role + content list), optional `max_tokens` (default 512, hard max 2048), optional `max_cost_usd` per request, optional `deadline_ms` (default 10000, max 60000).
- Response: model used, tier, content, usage, cost, cache_hit, fallback_count, request_id.
- Errors: 400 invalid or input over 32 KB; 422 unknown model or missing price; 402 no eligible model fits the request budget; 503 no eligible model or all attempts failed, or spend storage unavailable (includes request_id, never an empty success); 504 deadline exhausted.
- Redaction: API keys and `Authorization` headers are removed before any log write. Raw provider errors are logged with sensitive fields redacted (spec Risk 2).

### 4.2 Policy decision table (precedence top to bottom)

1. **Validation** rejects the request before any spend.
2. **Cache:** exact hit returns immediately at zero cost.
3. **Spend admission:** if spend storage is unavailable, refuse with 503 (fail closed). If the reservation would exceed the global cap, refuse with 503.
4. **Request budget:** a model is eligible only if its reserved worst-case cost fits the request's `max_cost_usd`. Budget can lower the tier, down to the cheapest tier. Quality or classifier pressure can never override budget. If no eligible model fits, 402.
5. **Model health:** a model in latency cooldown (5.4) is ineligible. If no eligible model remains, 503.
6. **Rate limit:** each model has a per-minute request cap in Redis (default 300, configurable). Over the cap, the model is ineligible for this request. If Redis is unavailable, rate limiting fails open, because the spend cap in step 3 still applies.
7. **Quality routing:** disabled during the benchmark and until the judge is validated (5.7). When enabled, it can raise the tier for new requests on a route after an alert (5.7). It never re-calls a paid model for the same live request. Budget (step 4) still applies.
8. **Fallback:** on failure, the next eligible model in the chain is tried. Each attempt re-runs steps 3-6 and checks the remaining deadline.

Terminal outcomes: success, 402, 503, 504. No silent degradation.

### 4.3 Retry ownership
- Application code owns retries and fallback. LiteLLM retries are disabled (`num_retries=0`), so each attempt is counted once.
- Retryable: timeouts, 429, 5xx, connection errors. Max 2 attempts per model, max 3 models per request.
- Non-retryable: 400, 401, 403, 404 for a model, content policy errors. These fail that attempt and move on without retrying the same model.

## 5. Components

### 5.1 Spend control (`monitoring/` + `routing/`)
- **Durable reservation before dispatch.** For every paid call, in one database transaction, insert an `attempts` row in state `reserved` with a unique attempt id and the worst-case cost. The reservation is counted against the cap under row locking.
- **Dispatch.** Only after the reservation commits is the provider called. The row moves to `dispatched`.
- **Settlement.** On a response, the row moves to `settled` with the actual cost from usage.
- **Unknown outcome.** On timeout or crash with no response, the row stays `dispatched`. Recovery marks it `unknown` and charges the full reserved amount. It is never automatically replayed as though it did not run. A replay requires a new attempt with its own reservation.
- **Crash between reservation and dispatch update.** A `reserved` row whose call was made but whose state write was lost cannot be distinguished from one never dispatched. Therefore recovery treats every `reserved` row older than the call timeout plus a 30 s grace period as `unknown` and charges the reserved amount. A reservation is released at zero cost only by a settlement that carries actual usage, never by inference from absence of a response.
- **Settlement-write failure.** If the provider responds but the settlement write fails, the row stays `dispatched`, the response is not returned as a success, and the row is later charged at the reserved amount as `unknown`. The response content is logged with its attempt id for audit, not returned.
- **Fail closed.** No paid call is made without a committed reservation. If Postgres is unavailable, requests that need a paid call return 503. Cache hits still work, since they do not spend.
- **Scope.** Applies to all paid calls: user requests, fallback attempts, judge calls and benchmark runs. Cap is shared across processes.
- Accept: tests for concurrent admission (N parallel requests never exceed the cap); crash after reservation before dispatch; crash after dispatch before settlement (charged, not replayed); restart mid-benchmark; Postgres outage (no paid call made).

### 5.2 Complexity classifier (`classifier/`)
- Pure function: `score(request) -> (tier, score, features)`. No I/O, no model calls.
- Features: length, keyword density (reasoning, code, maths, multi-step), task type, code-block presence.
- Thresholds in config, tuned on the **dev** split only.
- Accept: deterministic; unit tests per feature and per boundary. Accuracy on the labelled set is reported as a diagnostic. The outcome that matters is answer quality (5.10).

### 5.3 Routing (`routing/`)
- Tier to ordered model chain, from config. Default: simple = Claude Haiku, medium = a mid-tier model, complex = GPT-4o. The spec names GPT-3.5 as the cheap tier; if unavailable at build time, substitute a current cheap model and record it in `docs/`.
- Config-driven. No model ID in logic.
- Accept: decision-table tests (4.2), including: low budget plus quality alert, all models in cooldown, budget exhausted after a failed attempt, no eligible fallback, Redis down (rate limit fails open, cap still enforced), Postgres down (503).

### 5.4 Latency (`routing/` + `monitoring/`)
- **Scope:** the SLO and cooldown apply to **model** only, everywhere in the plan.
- **Measurement:** provider-call latency, P95 over the last 50 calls for that model, acting only after 20 samples.
- **Breach:** model enters cooldown for 60 s.
- **Recovery:** after cooldown, one probe is admitted. The probe succeeds only if it returns HTTP success **and** its latency is within the SLO. Success restores the model. Failure extends cooldown by 60 s.
- **Single probe:** admitted via a Redis `SET NX` lock. The probe's own timeout is 30 s. The lock has a 45 s TTL and its holder renews it every 10 s while the probe is in flight, then releases it on completion. A worker whose renewal fails must abandon its probe result and not update cooldown state. Only one probe runs across workers at a time.
- **Deadline:** covers the whole request (cache, backoff, all attempts). Exceeded means 504.
- Accept: injected-clock tests for deadline, cooldown, probe success, probe fail on latency only, and two workers racing for one probe.
- Real-provider latency is reported from the benchmark only. The mock load test reports local overhead only.

### 5.5 Fallback (`fallback/`)
- Implements 4.3. Every attempt is recorded in the attempt ledger, including its redacted raw error.
- Accept: tests for timeout, 429, 500, 400, total chain failure (503) and deadline mid-chain (504).

### 5.6 Cache (`cache/`)
- Default: **exact match only**. Key = SHA-256 of canonical JSON of: full message list including roles and system messages, model ID, price-table version, `max_tokens`, temperature and any other generation parameter.
- **Near-duplicate matching is disabled** and is not part of the benchmark claim. It may be enabled only after validation (see below). The statistical rule: with zero observed errors in n assessed hits, the 95% one-sided upper bound on the error rate is about 3/n (rule of three). To claim ≤1% with this bound, at least 300 hits must be assessed by the human rater with zero errors. This is documented as future work unless the user chooses to rate 300 hits.
- Adversarial tests: negation, reordered entities, changed numbers, code, differing system prompts. These must miss the cache.
- Redis outage: the cache is bypassed and the request proceeds. Logged as `cache_unavailable`. Spend admission (5.1) still applies.
- Accept: hit/miss tests, adversarial tests, outage test.

### 5.7 Quality (`monitoring/`)
- **Rubric:** written and committed before any scoring, with anchored examples for scores 1, 3 and 5. Stored in `monitoring/judge_prompt_v1.md`.
- **Calibration set:** the user's 50 human ratings, stratified by tier and task type. The judge scores the same outputs.
- **Validation gate.** The judge is validated only if all of the following hold on the calibration set:
  - Within-one agreement on individual scores is at least 0.80.
  - Paired-difference bias: the mean of (judge − human) across paired A/B differences lies within ±0.2 on the 5-point scale, with a 95% bootstrap CI that also lies within ±0.2.
  - Paired-difference agreement: Spearman correlation on paired differences, with bootstrap CI excluding 0.
  - Results are broken down by tier and task type. A failure in any breakdown means the gate fails.
- **If the gate fails:** the judge is diagnostic only. The quality conclusion is restricted to the human-rated subset of the held-out split. The user rates a predeclared subset of 60 held-out A/B pairs (blinded, randomised), selected by a seed before any scoring.
- **Quality alert (operation):** rolling mean of the last 30 judged responses on a route below 3.5 raises an alert. Action: the route's tier is raised for new requests (budget still applies). Recovery: when the rolling mean over the next 30 judged responses is at least 3.5, the raise is removed.
- **Judge operation:** 10% of live responses sampled. Judging is asynchronous through a bounded queue with its own spend cap. Timeouts, malformed scores and cap exhaustion are logged as `quality_unknown`, never as a pass.
- **Benchmark:** quality routing is off. The judge measures outcomes only and cannot change routing for any condition.
- Accept: tests for judge timeout, malformed score, cap exhaustion, queue overflow, alert and recovery thresholds, and the validation gate logic.

### 5.8 Ledger (`monitoring/`)
- **`requests`**: one row per API request. Outcome, tier, final model, `inference_cost`, `evaluation_cost` (judge calls with this request as parent), latency, cache hit, fallback count, quality status.
- **`attempts`**: one row per paid call (provider or judge). Model, provider, `purpose` (inference | evaluation), parent request id, attempt id (unique), tokens, price version, cost, state (`reserved | dispatched | settled | unknown`), `cost_status` (measured | estimated | unknown), redacted error.
- **Attribution:** every judge call carries its parent request id. Judge jobs are idempotent on (attempt id); repeated updates do not double-count.
- **Finalisation barrier:** a benchmark run is final only when every attempt is `settled` or `unknown`, and every judge job it created is terminal (completed, failed or quality_unknown). Totals are read only after the barrier.
- **Cost scopes:** dashboard and benchmark report `inference_cost` and `evaluation_cost` separately, and a total. Both use the same definition.
- **Reconciliation test:** sum of attempt costs = sum of request costs (inference + evaluation) = benchmark total = dashboard total. Asserted in tests with fallback, timeout, unknown-outcome, cache-hit and judge cases.
- Accept: each request writes exactly one `requests` row; each paid call writes exactly one `attempts` row; reconciliation test passes.

### 5.9 Metrics and dashboard (`monitoring/`)
- Prometheus metrics exported by the API. Grafana dashboard JSON provisioned from the repo.
- Each panel's formula, denominator and data source are documented in `docs/dashboard.md`.
- Each figure is labelled **measured** (real calls), **counterfactual** (baseline estimated from routed token counts), or **mock** (development data).
- The savings panel uses matched benchmark results only. Any live counterfactual is labelled as an estimate.
- Accept: a fixture with known totals (failures, retries, cache hits, judge calls, unknown outcomes) reproduces the expected panel values.

### 5.10 Benchmark (`benchmark/`)

**Dataset.** Frozen with version, source, licence and checksum. Three splits: dev (tuning), calibration (the 50 human-rated items), held-out (final claim). Deduplicated across splits. Split assignment saved with a seed.

**Definitions.**
- **Completed:** the provider returned content with a normal stop reason (not truncated, not an error).
- **Accepted:** completed and passes the quality measure in use for that prompt.
- **Cost per prompt** = total incurred spend in a condition (all attempts, retries, failures and unknown-outcome charges, inference and evaluation) divided by the number of prompts in the held-out set. Failures are included in the numerator and the denominator. No prompt is dropped.
- **Cost per accepted response** is reported as a secondary figure, with its numerator and denominator stated.

**Design.** Three conditions on the same held-out prompts: (A) always frontier, (B) routing without cache, (C) routing with cache. C is run cold (empty cache) and warm (after a first pass). Repetition rate of the workload is reported. Matched `max_tokens`, temperature and system prompt. Calls are interleaved, with order randomised using a saved seed.

**Coverage rule.** The headline claim is made only if all three conditions complete the full held-out set. If the cap aborts a run, the run is reported as incomplete, its spend is still reported in full, and no headline claim is made. Partial results are not analysed for the claim.

**Non-inferiority.** Difference d = quality(B) − quality(A) per prompt, paired by prompt. Margin δ = −0.3 on the 5-point scale. Procedure: bootstrap over prompts (resampling prompt pairs, with replacement), 10,000 resamples. Claim supported if the lower bound of the two-sided 95% percentile CI for mean d is greater than −0.3. This is the one-sided non-inferiority test at α = 0.025. The same test is applied to C.

**Quality source.** The validated judge if it passed the gate (5.7); otherwise the human-rated subset.

**Non-completed outputs in quality analysis.** Any prompt output that is not completed (failure, missing, truncated, budget rejection) is scored **1** (worst on the 5-point scale) for the quality analysis, in every condition, before pairing. This is predeclared. A sensitivity analysis that excludes non-completed outputs is also reported, labelled as such. Completion rate is reported per condition, so the quality claim cannot hide a completion problem.

**Human-rated quality scope.** If the judge fails its gate, the quality claim covers **B vs A only**, using the 60-pair human subset (5.7). The C-cold and C-warm quality claims are then withheld and the README says so. Cost claims for C are still reported.

**Reproducibility.** The results file records git commit, dataset checksum, model IDs, price-table version, seed, timestamp and any sample-size reduction. The second run is a fixed 30-prompt subset of the held-out split, run under the same conditions. Reproducibility is judged as: the second run's cost per prompt falls within the first run's per-prompt cost 95% bootstrap CI for that subset, and quality is within the same CI. A fixed percentage tolerance is not used. Live outputs are not identical between runs, and the README says so.

**Load test.** Async generator against the API with **mock providers only**. Fixed workload: N requests, concurrency C, duration D, hardware described. Pass: error rate below 1% and local overhead P95 below a stated threshold. Results labelled mock.

## 6. Delivery sequence

- **Week 1:** scaffold, `pyproject.toml`, Makefile, Docker compose, API contract (4.1), mock provider, classifier, policy engine, routing. Unit tests green.
- **Week 2:** spend control (5.1), ledger (5.8), Redis cache (5.6), fallback (5.5). Failure-matrix tests green, including Redis and Postgres outage behaviour.
- **Week 3:** quality contract, rubric, calibration and validation gate (5.7), latency SLO and cooldowns (5.4). Human ratings completed. Judge validation result recorded, including if it fails.
- **Week 4:** Prometheus, Grafana (5.9), benchmark with frozen splits, preflight and paired design (5.10), load test, README with measured results, CIs, quality result, limitations and screenshots.

Each week ends with a checkpoint commit.

## 7. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Quality floor subjective (spec Risk 1) | 50 stratified human ratings. Judge gated on validation, including paired-difference bias. Fallback to human subset if it fails. |
| LiteLLM hides provider errors (spec Risk 2) | Raw redacted errors logged per attempt. LiteLLM retries disabled. |
| Savings not reproducible (spec Risk 3) | Frozen dataset with checksum, seed, price table, model IDs, commit. Second-run check against per-prompt CI. |
| Savings bought with quality loss | Non-inferiority test with a stated margin and CI rule. Cost per prompt includes failures. |
| Spend overrun | Durable reservation before dispatch, fail closed, shared cap. Preflight cost estimate. Mock providers for development. |
| Crash during a paid call | Unknown-outcome state charged at reserved amount and never replayed silently. |
| Cache false hits | Exact-match default. Near-duplicate disabled unless 300 assessed hits with zero errors. |
| Overfitting thresholds | Dev split for tuning, held-out for the claim, thresholds frozen before held-out run. |
| Model IDs or prices change | Config-driven, dated price table, IDs verified at run time. |
| Windows-specific issues | Makefile targets call Python scripts. Docker tested on Docker Desktop. |

## 8. Spec traceability

| Spec item | Status at delivery |
|---|---|
| Complexity classifier | Implemented, unit-tested, accuracy reported as diagnostic |
| Cost-aware routing (GPT-3.5 / Haiku / GPT-4o) | Implemented. Substitution recorded if GPT-3.5 unavailable |
| Latency P95 and reroute | Implemented, injected-clock tests. Real latency from benchmark only |
| Quality scoring (LLM-as-judge) | Implemented. Validation result reported, routing gate enforced |
| Fallback | Implemented, failure matrix tested |
| Redis caching | Implemented, exact-match default |
| Redis rate tracking | Implemented as per-model per-minute cap in Redis, fails open, spend cap still applies |
| Cost ledger | Implemented, reconciled |
| Grafana dashboard | Implemented, labelled sources |
| Ollama | Optional. Verified only if the user runs the opt-in test. Otherwise labelled unverified |
| Benchmark and load test | Implemented. Mock load test; real benchmark on held-out split |
| Docker | Implemented via `docker-compose.yml` |

## 9. Security and privacy

- API keys from environment variables only. `.env.example` committed. `.env` in `.gitignore`.
- Redaction applied before any log write and before benchmark artifacts are saved. Tests scan logs and results for key patterns.
- The ledger stores token counts and hashes, not prompt text, by default. Full text is stored only in benchmark artifacts, which use the public or user-written dataset.

## 10. Out of scope

- Authentication, multi-tenancy, a web UI beyond Grafana.
- Training or fine-tuning any model.
- Cloud deployment. Docker compose is the deliverable.
- Streaming responses (future work).
- Near-duplicate caching in the benchmark claim (future work, 5.6).

## 11. Definition of done

- `make test` passes on a clean clone with mock providers.
- `docker compose up` starts the API, Redis, Postgres, Prometheus and Grafana; the dashboard shows mock data.
- `make benchmark` produces a results file. README reports cost per prompt with CIs, the non-inferiority result, the judge validation result, dataset, method and limitations.
- Spend ledger reconciles with the benchmark total.
- No secrets in the repo or logs.

## 12. Open items for review

- Exact model IDs and current prices, verified at build time.
- Judge model: cheap, and ideally a different family from the judged models. Choice recorded in `docs/`.
- Public dataset: choose and verify licence at build time.
- GBP to USD rate for the cap. Confirm with the user before the held-out run.
- User decision: rate 300 cache hits to enable near-duplicate caching (default: no).

## Revision log

- v1: draft from interview.
- v2: revised after Sol critique, round 1 (12 findings, REVISE).
- v4: revised after Sol critique, round 3 (4 blocking findings, REVISE). Changes: recovery rules for reservation-without-dispatch and settlement-write failure (5.1); non-completed outputs scored 1 and predeclared (5.10); C quality claims withheld if judge fails (5.7, 5.10); probe timeout shorter than lock TTL, with renewal and abandonment on renewal failure (5.4).
- v3: revised after Sol critique, round 2 (9 findings, REVISE). Changes: non-inferiority rule fixed (margin −0.3, lower CI bound test); durable reservation and unknown-outcome recovery; fail-closed spend storage; cost-per-prompt including failures and coverage rule; judge gate on paired-difference bias with human-subset fallback; explicit alert and recovery thresholds; quality routing off in benchmark; judge attribution, finalisation barrier and separate cost scopes; preflight sample sizing; model-level cooldown with single-probe lock and latency-based recovery; rule-of-three bound for near-duplicate cache, which is now disabled by default.
