# Alias and resolved-snapshot regression matrix (2026-10-17)

## Full-completion SLO correction (2026-10-17)

All entry points use `slo_ms: 20000`, measured from provider dispatch to the final
response. Benchmark runs reset health for every chain model; the API retains it.

| Contract | Regression coverage |
|---|---|
| One SLO, 20000 ms defaults, shared config in every mode | test_one_full_completion_slo_for_live_benchmark_and_judge |
| Full-completion boundary: 20000 accepted, 20001 opens cooldown after 20 samples | test_full_completion_slo_boundary_in_both_modes |
| Generation starts with no live/prior-run cooldown for all chain models | test_runner_resets_previous_run_health_and_exports_latency; test_runner_ignores_prior_redis_run_and_accesses_only_new_health_namespace |
| API health persists across requests; cooled models dispatch no calls | test_api_keeps_cooldown_between_requests |
| Standalone judge gets fresh memory/Redis health, keeps idempotent outcomes, refreshes latency exports | test_standalone_judge_memory_isolation_and_full_completion_export; test_standalone_judge_redis_isolation_and_full_completion_export |
| Attached judges share run health and export full-completion timings | test_attached_judge_attempts_have_latency_and_isolated_health |
| Full completion excludes settlement, preserves success/error/retry/cancellation assertions | test_latency_excludes_settlement_and_write_outage_keeps_answer; test_latency_records_success_failure_retry_and_timeout; test_cancellation_at_request_timeout_records_attempt_latency |

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

## Current temperature regression coverage

Both .venv and .venv-real: **122 tests passed, 0 failures/errors, 0 skipped**,
with local Postgres and Redis enabled. Syntax/hygiene lint passed on **51 Python
files** in each environment. Exact commands and timings are in VERIFICATION.md.

| Requirement | Observed evidence |
|---|---|
| API and direct request defaults use 1 | ContractTests.test_defaults |
| Range 0..2 is retained | ContractTests.test_temperature_range_endpoints; test_limits rejects out-of-range values |
| All A/B/C-cold/C-warm requests use matched temperature 1 | BenchmarkAndMetricsTests.test_dev_mini_conditions_and_mock_guard checks all eight requests and retains outcome/cache/cost assertions |
| OpenAI-style and Anthropic-style adapter calls receive 1 unchanged | ResolvedModelTests.test_matched_temperature_reaches_both_provider_adapters_unchanged uses mid/cheap LiteLLM stubs, including conflicting provider configuration |
| Cache identity changes with temperature | Existing CacheTests.test_adversarial compares default 1 against explicit 0; no duplicate cache test |

Two new test methods cover adapter forwarding and range endpoints; existing
contract, cache and benchmark tests retain their outcome assertions. No live
provider support or statistical reproducibility result is claimed by these tests.

## Historical alias and resolved-snapshot verification

Current suite: **120 discovered, 120 passed, 0 failures/errors, 0 skipped**.
Lint passed on **51 Python files**. HTTP transport, local Postgres and local Redis
integration tests ran using the environment documented in VERIFICATION.md.
Configured alias: `anthropic/claude-haiku-5-5`; user-approved caps:
**USD 15 global / USD 3 evaluation**. Pricing logic is unchanged.

| Requirement | Observed evidence |
|---|---|
| Stubbed provider returns a snapshot different from the requested alias | test_provider_response_snapshot_keeps_requested_alias_and_prices: Anthropic/OpenAI stubs, ledger, API, JSON records and run summary |
| Unavailable response identity stays null | test_missing_response_model_is_unknown; test_rejected_response_and_no_response_are_distinct |
| Rejected content and settlement failures retain returned snapshot | test_rejected_response_and_no_response_are_distinct; test_snapshot_survives_usage_settlement_failure |
| Judge calls retain snapshot even on invalid scores | test_judge_snapshot_survives_invalid_score |
| Result records retain every fallback attempt | test_export_retains_all_fallback_attempts |
| Cache provenance, judging, run-scoped counts and recovery | test_results_cache_judge_summary_and_recovery |
| Existing SQLite rows survive repeat migration without inferred snapshots | test_existing_sqlite_ledger_migrates_without_inventing_snapshot |
| Existing Postgres rows migrate, settle and reopen with separate IDs | PostgresIntegrationTests.test_snapshot_column_migrates_existing_postgres_ledger |
| Alias replaces the unlisted dated model ID; pricing stays configured | PricingTests.test_replacement_model_and_dated_version and all existing pricing methods |

Eight new snapshot test methods plus one local Postgres migration method.
Providers are stubs/mocks throughout. Live provider formats, availability and
billing remain unverified; no real calls, API key values or held-out/calibration
real runs were used. Historical mock-result descriptions below are retained.

# Tier pricing regression matrix (2026-10-17)

Historical tier-pricing suite: **101 discovered, 98 passed, 0 failures/errors, 3 skipped**.
All 16 tests in tests/test_pricing.py passed. Lint passed on 49 Python files.
The dev diagnostic remains 20/43 (46.51%) with frozen thresholds [2,5].

Cheap model: provider alias `anthropic/claude-haiku-5-5` (Claude Haiku 5.5).
Configured user-approved caps: USD 15 global / USD 3 evaluation. The dated
`claude-haiku-5-5-20261001` was not listed, per the supplied provider model-list
finding. Provider-returned snapshots are now recorded separately per attempt.
The replacement ID and prices were supplied by the user and not independently
verified against the provider page. USD input/output per million tokens:
$0.10/$0.50 up to 100,000 input tokens; $0.50/$2.50 above 100,000.
One input-selected tier prices each whole request, pending user confirmation.
Mid/frontier/local flat prices remain unchanged.

| Requirement | Observed offline evidence in tests/test_pricing.py |
|---|---|
| Configured alias and dated price version | test_replacement_model_and_dated_version |
| Exactly 100000 uses lower tier | test_exactly_100000_uses_up_to_tier |
| 100001 uses higher tier for all input/output | test_100001_uses_above_tier_for_entire_request |
| Input selects tier independently of output/cumulative usage | test_input_alone_selects_tier; test_tier_is_per_request_not_cumulative |
| Estimate 89999 lower; 90000/99999/100000 reserve higher | test_reservation_10_percent_guard_and_boundaries |
| UTF-8 estimate, framing and max output are reserved | test_reservation_uses_utf8_framing_and_output_limit |
| Actual input selects settlement tier, ledger cost/version reconcile | test_settlement_uses_actual_tier_and_records_effective_version; test_error_usage_settles_actual_tier |
| Higher reserve controls request budget before dispatch | test_guarded_reservation_controls_request_budget_before_dispatch |
| Mid/frontier/local retain flat costs | test_flat_models_unchanged |
| Missing/malformed prices return 422, no dispatch | test_malformed_tiers_raise_same_422_price_error; test_missing_all_prices_raises_422; test_flat_prices_still_reject_missing_or_invalid_values; test_price_error_before_dispatch |
| Threshold/input/output price edits invalidate keys without manual bump | test_price_changes_invalidate_cache_even_without_manual_version_bump |

Boundary fixtures bypass the public 32 KB transport limit only for synthetic
internal pricing tests. Providers are mocks or in-memory client stubs. Skipped:
HTTP transport (FastAPI/httpx missing), local Postgres DSN and local Redis URL
not supplied. Model availability, provider billing and tokenizer bounds remain
unverified. No real call, key or held-out benchmark was used.

# Round-two review regression matrix (2026-10-16)

Historical round-two suite: 85 discovered, 82 passed, 0 failed/errors, 3 optional integration
skips. All tests below live in tests/test_review_round2.py. Fixed means the code
and offline regression pass; it does not claim live infrastructure verification.
The earlier plan matrix below is retained for broader acceptance gaps.

| Finding | Status | Regression tests | Remaining verification |
|---|---|---|---|
| 1 Authorization and provider error secrets | Fixed | test_entire_authorization_and_provider_secrets | Real provider error formats unverified |
| 2 Whole-request deadline and response I/O | Fixed | test_slow_cache_write_and_quality_submit_leave_response_path; test_slow_quality_submission_is_off_response_path; test_deadline_after_settlement_and_finish_keeps_measured_spend; test_sqlite_busy_timeout_is_bounded_by_deadline; test_postgres_repeatable_snapshot_and_deadline_timeouts | Real Postgres lock/connect timing unverified; cleanup can outlast the deadline but cannot produce late success |
| 3 Judge recovery/claim/reservation race | Fixed | test_recovery_between_claim_and_reserve_cannot_enable_paid_call_after_barrier; test_terminal_job_cannot_reserve_after_barrier; test_recovery_contends_with_claim_and_reservation_as_one_transaction | Concurrency verified with SQLite threads; actual Postgres process race unverified |
| 4 Full provider parameters in cache key | Fixed | test_provider_parameters_and_version_change_cache_key; test_router_key_uses_model_generation_parameters | Redis adapter integration skipped |
| 5 Lost probe retains measured usage | Fixed | test_lost_probe_settles_measured_usage_without_health_change | Real Redis lease expiry timing unverified |
| 6 Consistent metrics scrape | Fixed | test_postgres_repeatable_snapshot_and_deadline_timeouts; test_metrics_snapshot_cannot_mix_concurrent_reservation | Postgres driver contract and SQLite concurrency verified; live Postgres scrape unverified |
| 7 Judge shares model admission and latency | Fixed | test_judge_shares_inference_rpm_and_cooldown; test_judge_latency_in_shared_model_window | Shared memory admission verified; live Redis cross-process state unverified |
| 8 First reservation crash recovery | Fixed | test_crash_first_reservation_recover_from_ledger_without_artifact | File-backed SQLite crash injection verified; actual process kill/Postgres crash unverified |
| 9 Guarded real execution | Fixed | test_real_guard_and_stubbed_litellm_shared_cap; test_api_real_flags_and_shared_spend_with_stubbed_client; test_real_judge_requires_flag_and_uses_only_stubbed_litellm | Stubbed LiteLLM only; actual API/provider/price behaviour unverified |
| Classifier note | Original frozen rules restored | test_original_keyword_rules_for_short_prompts; test_original_frozen_rules_dev_agreement | Heuristic with low dev agreement: 20/43 (46.51%), thresholds [2,5]; no held-out data used; answer quality is the outcome that matters and remains unmeasured |

No real provider calls, keys, external network requests or held-out execution were
used in tests. The API composition test stubs its optional HTTP framework; it does
not substitute for the skipped HTTP transport test. Synthetic judge results do not
validate real quality.

# Offline acceptance matrix

Actual commands/results are in VERIFICATION.md. Covered means an offline behaviour
ran successfully, not that an external integration passed. Three optional methods
are skipped here: HTTP transport, Postgres and Redis.

| Plan criterion | Coverage | Evidence / remaining gap |
|---|---|---|
| 4.1 API defaults, limits, roles, finite budget, 32 KB, response fields | Partial | ContractTests ran; APITests requires unavailable FastAPI/httpx |
| 4.1 400/402/422/503/504, request_id, no empty success | Partial | ContractTests, RoutingTests, RegressionTests ran; HTTP serialization skipped |
| 4.1 redacted errors and headers | Covered offline | fallback_error_matrix, redaction, redaction_json_error_string |
| 4.2 validation/cache/spend/budget/health/rate/quality precedence | Covered in doubles | validation_before_spend, success_cache_and_ledger, global_cap_precedes_request_budget, quality budget and outage tests |
| 4.3 application retries: 2/model, 3 models; non-retryable statuses | Covered offline | total_failure asserts 6 calls, fallback_error_matrix; LiteLLM num_retries=0 configured but unexercised |
| 5.1 N concurrent admissions do not exceed cap | Partial | 50 SQLite admissions admit exactly 5; Postgres cross-worker test supplied/skipped |
| 5.1 crash after reservation before dispatch; grace rule | Partial | crashes_and_restart charges aged reserved rows; actual Postgres crash not run |
| 5.1 crash after dispatch, charge full reserve, never replay | Partial | crashes_and_restart and crash_artifact_recovery_does_not_replay use file-backed double |
| 5.1 settlement-write failure withholds success and logs attempt audit | Covered in double | settlement_failure asserts 503, dispatched state and attempt-id log |
| 5.1 restart mid-benchmark | Partial | durable checkpoint recovery fixture; no process-kill test against Postgres/full benchmark |
| 5.1 Postgres outage: zero paid calls, cache still serves | Partial | outages double test; real outage unavailable; outage request/cache ledger writes necessarily best effort |
| 5.2 deterministic, pure feature scorer | Covered | deterministic and feature tests for length, density, task type, code blocks |
| 5.2 configured threshold boundaries | Covered | scores 2 and 5 plus neighbors, injected length thresholds |
| 5.2 labelled-set accuracy diagnostic | Covered on dev | Original frozen heuristic rules: 20/43 (46.51%), thresholds [2,5]; no held-out data used; answer quality is the outcome that matters |
| 5.3 low budget plus quality alert | Covered offline | low_budget_quality_alert chooses cheap model within cumulative budget |
| 5.3 all models in cooldown | Covered offline | all_cooldown returns 503, zero calls |
| 5.3 exhausted budget after failure | Covered offline | budget_exhausted_after_failure returns 402 after one charged attempt |
| 5.3 no eligible fallback | Covered offline | no_eligible_fallback, total_failure |
| 5.3 Redis down: rate open, cap enforced | Partial | rate_fail_open, cache_down_rate_down_cap_enforced, redis_health_outage_no_unlocked_probe; real Redis skipped |
| 5.3 Postgres down: 503 | Partial | unavailable contract store tested; real server unavailable |
| 5.4 model P95, 50-call window, minimum 20 samples, cooldown | Covered offline | injected-clock minimum_samples_and_cooldown and probe tests |
| 5.4 whole-request deadline: cache, retry/backoff, mid-chain | Covered offline | injected_deadline_includes_cache, injected_deadline_mid_chain, asyncio timeout |
| 5.4 probe success, failure, latency-only breach | Covered offline | probe_success at SLO boundary, probe_latency_only_failure |
| 5.4 two workers racing for one probe | Partial | shared atomic store race passes; Redis Lua test supplied/skipped |
| 5.4 lease TTL, renewal, stale/lost result abandonment | Partial | ttl_renew_and_stale_result, renewal_failure_abandons; real TTL timing unverified |
| 5.5 timeout/429/500 retries, 400/401/403/404 no same-model retry | Covered offline | fallback_error_matrix with expected counts and redacted raw errors |
| 5.5 total failure 503 / deadline mid-chain 504 | Covered offline | total_failure, injected_deadline_mid_chain |
| 5.6 exact hit/miss; model/price/generation key isolation | Covered offline | success_cache_and_ledger, adversarial |
| 5.6 negation, reordered entities, numbers, code, system prompt | Covered offline | adversarial explicitly tests all five misses |
| 5.6 Redis cache outage bypass | Partial | outage double tested; real Redis integration skipped |
| 5.6 near-duplicate validation, 300 assessed hits | Not covered; disabled | No near-duplicate matcher or claim; future work |
| 5.7 rubric and anchored examples before scoring | Supplied | judge_prompt_v1.md; only synthetic fixtures scored |
| 5.7 fifty actual human calibration ratings | Not met | Zero supplied; blinded export and validation tools supplied |
| 5.7 timeout, malformed score, cap exhaustion, overflow | Covered offline | timeout_malformed_cap_and_queue; all become quality_unknown |
| 5.7 alert/recovery thresholds, exact mean 3.5 | Covered offline | alert_recovery_and_gates, quality_threshold_exactly_three_point_five |
| 5.7 agreement/bias/correlation CIs and breakdown gate | Covered on fixtures | validation_pass, bias_and_breakdown_gate, degenerate_correlation_fails; actual validation pending |
| 5.7 bounded async sampling and job idempotency | Covered offline | queue tests, valid_judge_attribution_idempotency, durable_judging_idempotent_dev_fixture |
| 5.7 benchmark quality routing disabled | Covered offline | benchmark and validated/enabled flag tests |
| 5.7 60 blinded human held-out A/B pairs | Partial; deviation | Held-out run executed (240/240 records, USD 0.2115). One blinded rater rated 54 complete pairs; six excluded (incomplete A or B). 60-pair gate unchanged, so B quality claim withheld; 54-pair result descriptive only (docs/BENCHMARK.md) |
| 5.8 one request row and one row per paid call | Covered in double | success/cache/fallback and idempotency assertions |
| 5.8 judge parent attribution and distinct cost scopes | Covered in double | settlement_idempotency_and_judge_attribution; valid_judge_attribution_idempotency |
| 5.8 finalisation barrier for attempts and jobs | Covered offline | finalisation_barrier_and_redaction; no totals before barrier |
| 5.8 attempts = requests = benchmark = dashboard totals | Covered in double | failure_fixture_reconciliation_and_panels covers fallback, cache, unknown and judge; dev mini also reconciles |
| 5.9 metrics export and source-labelled panels | Partial | snapshot and artifact checks ran; live scrape/PromQL/rendering unavailable |
| 5.9 fixture reproduces panel values | Partial | numerical formulas/counts and JSON titles tested; Grafana/Prometheus not run |
| 5.10 frozen source/licence/checksum, split dedup | Covered offline | frozen_split_and_dedup, checksum_rejected; upstream licence checked separately |
| 5.10 preflight estimator, allocation and sample reduction | Covered offline | preflight tests positive/negative/missing dev cost cases |
| 5.10 A/B/C-cold/C-warm matched design | Covered on dev mocks | dev_mini_conditions_and_mock_guard, full dev mock command; held-out never executed |
| 5.10 failure-inclusive fixed denominator/cost per prompt | Covered offline | incomplete_denominator_and_withheld_c and reconciliation |
| 5.10 accepted denominator / unknown quality | Covered offline | threshold >=4 documented; pending quality returns null accepted count/cost |
| 5.10 coverage rule and cap abort | Partial | incomplete fixture suppresses claims; caps tested; runner aborts on cap; held-out cap-abort run intentionally not executed |
| 5.10 paired NI, bootstrap, failed output=1, sensitivity | Covered offline | statistics fixtures and default 10,000-resample dev analysis |
| 5.10 C quality withheld without validated judge | Covered offline | human-source and diagnostic mock-judge fixtures |
| 5.10 reproducibility fixed subset / paired CIs | Covered algorithmically | reproducibility_fixed_subset and paired_savings_interval; no second held-out run |
| 5.10 git/dataset/model/price/seed/time/reduction metadata | Covered on dev mock | result writer and successful dev artifact; checkpoint/recovery fixtures |
| 5.10 fixed mock load, error <1%, local P95 threshold | Partial; HTTP threshold passed after pooling | In-process p95 0.5 ms passes. HTTP on the Docker stack (mock provider, N=100, C=10), after the connection pool: 0 errors, p95 48.3, 42.4 and 43.1 ms against a 50 ms threshold (three runs, cache flushed each time). Before pooling: p95 1,902 ms, FAILED. Cause before pooling: each routed request opened 9 new Postgres connections (about 270 to 330 ms per request even at concurrency 1). Threshold unchanged. |
| 6 compose stack starts and dashboard screenshot | Not verified | Docker daemon unavailable; compose/provisioning supplied |
| 6 human validation, real benchmark results/CIs | Not met by design | Human input and separately authorised benchmark still pending |
| 9 secrets/log/artifact redaction | Covered on fixtures | recursive redaction, JSON error strings, provider-error logs and artifact scans; no provider keys required |

Real-provider integrations remain unverified. The opt-in LiteLLM adapter and real
entry-point composition are exercised only with an in-memory client stub. Synthetic calibration successes do not validate a judge;
monitoring/calibration_status.json remains pending and unvalidated.

## Ledger timeout and paid-response regressions (2026-10-17)

These checks implement the ledger timeout brief. DECISIONS.md records its explicit
change to PLAN 5.1's former paid-answer-withholding behavior; PLAN.md is unchanged.
All provider responses are mocks/in-process fakes, including the Postgres tests.

| Requirement | Regression / evidence | Result |
|---|---|---|
| N=8 ordinary-statement timeout reproduced | PostgresStatementTests.test_n8_slow_statement_reproduces_57014_then_default_budget_passes; 250 ms insert trigger, old 200 ms budget | 8 QueryCanceled/57014 failures; then 8/8 settled with new default |
| No spontaneous failures claimed on idle server | Pre-change N=8, 20 cycles per worker | 160/160 completed; controlled delay required for deterministic reproduction |
| Independent statement budget and expired-deadline cleanup | AdmissionRetryTests.test_configurable_statement_budget; PostgresStatementTests.test_ordinary_read_has_statement_budget_even_with_expired_request; updated test_ledger_lock/test_review_round2 contracts | default 2000 ms; configurable/validated; slow read succeeds |
| Deadline still prevents dispatch/return | RequestDeadlineAfterLockTests; existing ReviewRoutingTests deadline contract; shared SettlementContract.test_request_deadline_remains_504_after_settlement_retry | 504 retained; no provider dispatch after admission consumes deadline; measured cleanup retained |
| Contention classification and bounded jitter | AdmissionRetryTests.test_sqlstate_classification, test_bounded_contention_retry_and_no_outage_retry, test_expired_deadline_stops_retries | 57014/55P03/40001/40P01 retryable; genuine failures one execution; max 3 attempts |
| Actual Postgres serialization rollback/retry | PostgresStatementTests.test_real_serialization_failure_retries_same_reservation | Two 40001 failures, one reserved row and one charge |
| Idempotent lost acknowledgements | Shared SQLite/Postgres SettlementContract.test_lost_commit_acknowledgements_never_double_charge_or_dispatch | Lost reserve/dispatch/settle acknowledgements; one fake provider call, one attempt, reconciled charge |
| In-memory N=8 equivalent | AdmissionRetryTests.test_sqlite_n8_idempotent_reserve_and_settle_cycles | 8 attempts; repeated reserve/settle does not double-charge; total USD 0.08 |
| Capture before settlement backoff | Shared test_retried_settlement_preserves_answer_and_durable_content | Journal can be reopened before retry; content survives and eventual actual settlement is recorded |
| Actual Postgres settlement cancellation | PostgresStatementTests.test_real_settlement_cancellation_is_retried_and_answer_is_returned | Trigger raises 57014; one provider call; content returned as settlement_retried |
| Real outage remains fail-closed on admission | Existing routing outage, Postgres lock-exhaustion, diagnostic and admission retry tests | No paid call without committed reservation; non-transient outages not retried as contention |
| Outage after paid response | Shared test_total_outage_after_paid_response_returns_unknown_and_recovers | Content returned with unknown status/reserved cost; persistent journal replay mirrors unknown, no provider replay/refund |
| Completed benchmark output with failed settlement | SQLiteSettlementTests.test_benchmark_preserves_completed_unknown | Four mock conditions retain completed=true/status=200/settlement_status=unknown and positive reserved spend |
| Restart without completed benchmark artifact | SQLiteSettlementTests.test_benchmark_restart_recovers_journal_content_without_provider_replay | Pending record restored from durable response; measured cost and final barrier reconcile |
| Paid-response identity retained | Updated test_resolved_models settlement-failure regression | Resolved snapshot remains; answer returned as unknown with reservation |
| Haiku empty/policy output is failure | SQLiteSettlementTests.test_empty_answer_fails_attempt_and_moves_to_next_model_without_same_model_retry; existing paid filtered-output test | Measured nonzero charge plus error 400; skip same-model retry; eligible fallback succeeds |
| Historical four Haiku attempts reviewed | DECISIONS.md read-only evidence table | Four failed outputs, USD 0.0010434; three request 503s and one 504; no artifact rewrite |
| Global cap/rejection reconciliation | Existing concurrent SQLite/Postgres rejection tests with scheduling-independent final fill | Concurrent total at most cap; final measured count and exact cap totals retained |

Final full suites: 152/152 pass in each requested venv with local Postgres/Redis,
zero failures/skips. New module without service variables: 12 pass, 8 skip.
Syntax/hygiene lint: 53 Python files, zero errors in each venv. Exact commands,
durations, log paths and reproduction limitations are in VERIFICATION.md.
