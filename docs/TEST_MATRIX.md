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
| 5.2 labelled-set accuracy diagnostic | Covered on dev | diagnostics command: 20/43 correct tier labels, not answer quality |
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
| 5.7 60 blinded human held-out A/B pairs | Tooling supplied; not met | Manifest fixes IDs; held-out execution and human rating intentionally not run |
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
| 5.10 fixed mock load, error <1%, local P95 threshold | Partial | local in-process diagnostic ran; HTTP load unavailable without FastAPI |
| 6 compose stack starts and dashboard screenshot | Not verified | Docker daemon unavailable; compose/provisioning supplied |
| 6 human validation, real benchmark results/CIs | Not met by design | Human input and separately authorised benchmark still pending |
| 9 secrets/log/artifact redaction | Covered on fixtures | recursive redaction, JSON error strings, provider-error logs and artifact scans; no provider keys required |

Real-provider LiteLLM/OpenAI/Anthropic/Ollama code paths are unverified and never
exercised by tests. Synthetic calibration successes do not validate a judge;
monitoring/calibration_status.json remains pending and unvalidated.
