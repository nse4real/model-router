# Benchmark and human evaluation workflow

## Current quality source: human ratings pending (2026-10-17)

The automated judge is dropped for quality conclusions. Quality conclusions
come from **human ratings only**, pending the **60-pair blinded held-out A/B
subset**, selected by seed before any scoring under PLAN.md section 5.7.
The conclusion covers **B versus A only**. C-cold and C-warm quality conclusions
are withheld unless also human-rated; the existing human fallback withholds C.
A matching real dev run is still required for preflight, and held-out execution
still requires explicit approval. No real calls are authorized by this update.

**Deviation (2026-10-17, user decision).** The human subset is **54 complete pairs**,
not the predeclared 60. Six prompts had an incomplete A or B output: complex-001,
complex-021, complex-028, complex-032, complex-039 and complex-047. They are excluded
from all human-subset analysis, including cost and completion. This departs from
PLAN.md section 5.7, which scores non-completed outputs as 1. The 60-pair gate in
`benchmark/results.py` is unchanged, so the B quality claim stays withheld. The
54-pair result is reported descriptively only: one blinded rater, mean rating A 4.32,
B 4.52, mean difference +0.20, with no significance test or interval yet. Scores
cluster at 5 (78 of 108), so a ceiling effect is possible.

V1, v2 and v3 on mid returned constant 5s; v3 on frontier (`gpt-4o`) had
near-zero paired-difference correlation. All failed validation; evidence is in
`results/judge-calibration-run5*-report.json`. Existing calibration ratings are
**50 separate scores on 25 pairs** in
`results/ratings-calibration-run5/ratings-blinded.json`, not the held-out subset.
See DECISIONS.md for the audit and reporting limitations. The runner currently
serializes `quality_source="pending"` and `judge_validated=False`; it does not
literally serialize "human pending". Historical judge workflows below remain
diagnostic and do not supersede this human-only decision.

## Full-completion latency and health isolation

`config/models.json` defines one `slo_ms: 20000` for the live API, benchmark
generation and judging. The metric is **full-completion latency**, from provider
dispatch to the final response (or failure/cancellation), measured monotonically
before settlement/storage work. It is not time to first token. The circuit still
needs 20 samples before applying P95 over the latest 50 attempts; request
deadlines are configured separately below; provider timeouts and the 60-second
cooldown are unchanged.

Each generation run starts fresh health for every model in its chains and its
attached judge. Health/probe keys are scoped to the run ID:
`router:health:benchmark:<run_id>:<model>` and
`router:probe:benchmark:<run_id>:<model>`. Standalone `benchmark.judge` starts a
fresh scope on every invocation, so it cannot inherit live, generation or prior
judge cooldowns. Memory runs also replace their health store. Redis observations
refresh a 3600-second TTL, and probe leases remain 45 seconds. The API retains
persistent health across requests. Redis RPM limits and Postgres spend caps
continue to be shared.

This follows the user decision after `results/calibration-real-v3.json` returned
120 of 200 HTTP 503 responses due to cooldown. See docs/DECISIONS.md.

Each attempt row has nullable `latency_ms` using the same full-completion metric
for inference and judging, including retries, timeouts and cancellation. Existing
SQLite and Postgres schemas migrate automatically. Old or unavailable measurements
stay null. A latency-write outage does not discard a valid paid response.

The common exporter refreshes `attempts[].latency_ms` and `attempt_latencies`
from SQL for generation, standalone judging and recovery. For example:
`[{"attempt_id": "...", "latency_ms": 5021.4}]`. Failed requests retain all their
attempt measurements; warm cache hits have no inference attempt. Durable generation
records also refresh attached judge timings. A process crash before record refresh
can leave that snapshot incomplete; the exporter recovers stored timings from SQL.

Validation uses mock providers and local Postgres/Redis; no real, calibration or
held-out run was performed. Historical calibration files remain unchanged.

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

No held-out benchmark has been run. Entry points default to mock providers;
explicit real execution requires --real, ROUTER_CONFIRM_REAL_SPEND=YES and shared
Postgres storage. Successful executed results are development fixtures; the failed
real calibration above does not measure provider savings, quality or latency. Mock workflows require no API keys.

## Frozen inputs

benchmark/data/manifest.json fixes version, SHA-256, split seed, 60 human-pair IDs
and 30 repeat IDs. Thresholds and the versioned mock price table are in
config/models.json. Prompt assignments are saved in prompts.jsonl and are checked
for duplicates. See DATASET.md for provenance/licence and original author labels.

## Dev run and diagnostics

~~~text
python -m benchmark.diagnostics
python -m benchmark.run --split dev --output results/dev-mock.json
~~~

The normal backend is shared Postgres. With unavailable infrastructure, explicitly
use --storage test-double; its file-backed SQLite is a contract double, not a
production admission proof. Output paths are exclusive: choose a new name for a
new run. Run artifacts and sidecar doubles are ignored by git.

Every result records git commit, dataset checksum/version, configured model IDs,
price-table version, thresholds, seed, UTC timestamp, storage, split, preflight
reduction and data_source (mock or real-unverified). All generation settings are matched: the fixed system
prompt, max_tokens=512, temperature=1. A uses only the configured frontier model;
B has no cache; C starts cold and is then repeated warm. A/B/C-cold are interleaved
in a seeded random order per prompt; warm follows the entire cold pass. The unique
cold repetition rate is 0 and the warm repetition rate is 1. These are cache
scenarios, not an estimate of a production workload's repetition rate.

## Preflight and protected held-out execution

The CLI defaults to dev. Mock held-out needs --allow-held-out-mock; real held-out
needs --real and --allow-held-out plus the environment confirmation. Both require
--dev-results pointing to a final dev artifact from the same provider mode. Dataset/model/price/threshold
metadata must match. Mean complete four-condition dev costs include inference,
evaluation, failed attempts, retries and unknown charges. Preflight multiplies
this by 1.25 and reduces the selected sample before execution to fit USD 12.
The result saves the requested count, admitted count, allocation and reduction.

This estimate cannot replace the USD 25 durable global cap. All production-style
commands use the same Postgres database/schema as the API. All evaluation calls
also consume the USD 3 evaluation allowance. A cap abort retains every incurred
charge and marks coverage incomplete; partial results never support a claim.
The second-run flag selects the manifest's fixed 30 held-out IDs and requires
preflight to admit the full subset. No held-out or repeat command was run here.

## Checkpoints, failures and finalisation

Before the first dispatch, the ledger stores run ID, seed, dataset checksum,
configuration and price version. Before every dispatch it also stores a pending
record with prompt ID, condition and request ID; this includes the very first
reservation. Results are checkpointed before dispatch and after completion with
atomic JSON replacement. Recovery can reconstruct an incomplete run using the
ledger alone even if no JSON artifact survived. It preserves condition attribution
and charges while leaving missing outputs non-completed; it never invents content.

~~~text
python -m benchmark.recover results/dev-mock.json
python -m benchmark.recover results/dev-double-mock.sqlite
~~~

For Postgres with missing JSON, supply an output JSON path, --storage postgres
and --run-id; DATABASE_URL locates the durable run metadata. SQLite recovery can
use its ledger path directly. Recovery does not require real-spend confirmation
because it cannot dispatch calls.

Wait until the configured call timeout and recovery grace have elapsed
(60 seconds total by default) for aged unknown attempts. Recovery performs no provider calls and never replays an old
attempt ID. Deliberately rerunning work means a new run and new reservations.
A finalisation barrier requires every attempt settled/unknown and every judge
job terminal. Until it passes, final=false and totals/analysis are withheld.
Finalised and full-coverage are separate concepts: an incomplete run can have
fully reconciled final spend, but never a headline claim.

## Quality collection and calibration

monitoring/judge_prompt_v1.md fixes the 1/3/5 anchors before scoring. Live sampling
is 10% through a bounded async queue. Mock scores are fixed diagnostic fixtures:

~~~text
python -m benchmark.judge results/dev-mock.json
~~~

Judging an existing mock artifact reserves shared evaluation spend before every
call. Deterministic job IDs make reruns idempotent. Inference/evaluation totals
are refreshed separately. Timeouts, bad scores and cap exhaustion are unknown.
The mock judge cannot validate itself and cannot support a quality conclusion.
For a real artifact use benchmark.judge with --real and the environment
confirmation. Real generation uses run-scoped Redis health keys and the API's
shared RPM keys; the separate judge entry point retains its existing health
admission. Both retain shared Postgres caps and the artifact's frozen configuration. Scores
remain diagnostic under the current human-only decision. All adapter tests
use a stubbed LiteLLM client, with no credentials or provider network calls.

The calibration split has 50 prompts. Seeded round-robin selection supplies 25
A/B pairs, hence 50 individually rated outputs, across every tier/task stratum.
After a separately chosen calibration mock run, export its completed A/B records:

~~~text
python -m benchmark.ratings --artifact results/calibration-mock.json --mode calibration --output results/calibration-ratings
~~~

Give the rater only ratings-blinded.json. The mapping-private.json file records
the random left/right assignment and original prompt IDs. Human ratings must be
integers 1..5. Do not infer missing human ratings from fixture judge outputs.

Calibration validation input is a JSON list with fields human_a, human_b,
judge_a, judge_b, tier, task_type on each pair. Align human left/right scores
through the private mapping to A/B; use judge scores for exactly those outputs.
This alignment is deliberately reviewable rather than guessing human scores.

~~~text
python -m benchmark.ratings --validate-calibration results/calibration-pairs.json --output results/calibration-validation.json
~~~

Gate requirements: >=50 individual human ratings, within-one agreement >=0.80;
paired-difference judge-minus-human bias and its bootstrap CI entirely within
[-0.2,+0.2]; positive Spearman agreement on paired differences with CI excluding
zero; every tier/task breakdown must pass. Degenerate/inadequate strata fail.
Actual calibration ratings now exist (50 scores on 25 run-5 pairs), but all
tested judge variants failed the gate. Judge-based quality conclusions are dropped.

If the gate is unvalidated, the quality conclusion is restricted to B vs A on
exactly the 60 predeclared blinded human pairs. Export mode human-held-out and
benchmark.evaluate merge those integer ratings with the private mapping. C-cold
and C-warm quality conclusions remain withheld. No held-out outputs or human
scores were generated in this build.

## Statistics and reporting

Cost per prompt = all incurred condition spend / full planned prompt count,
including failures. Completed means content returned with normal stop reason.
Accepted means completed and quality >=4; unknown quality makes acceptance
unknown, never a pass. Cost per accepted response is secondary and states its
denominator. Inference, evaluation and total cost are separate.

Current paired non-inferiority compares B-A on the human-rated subset; C quality
conclusions are withheld unless also human-rated. Use
10,000 prompt-pair bootstrap resamples, a two-sided 95% percentile CI and margin
-0.3: lower CI bound must be strictly greater than -0.3. Non-completed outputs
score 1 before pairing; completion rates and completed-only sensitivity are
reported separately. Missing scores on completed outputs withhold the conclusion.
Cost/savings intervals also resample matched prompts. All current claims remain
unsupported because data_source is mock.

benchmark.reproduce compares existing first/second artifacts on the fixed 30
IDs. Second cost-per-prompt and quality means must fall within first-run subset
bootstrap intervals, not a fixed percentage tolerance. Metadata must agree. Live
outputs would not be identical across runs. With unknown quality, reproducibility
is unsupported. No real or held-out second run occurred.

## Load test

The Make target uses loopback HTTP, checks /health for providers=mock, and runs
N=100, concurrency=10, paced duration=5 seconds. Pass is errors <1% and local
P95 <50 ms. Reports include platform, architecture, logical CPU count and Python.
The explicit --transport in-process diagnostic tests core overhead without HTTP;
it is labelled mock and does not verify the HTTP performance acceptance criterion.


## Calibration subsets, request deadlines and immutable merges (2026-10-17)

Generation requests in `benchmark.run` explicitly use `benchmark_deadline_ms`
from the model configuration, default **60000 ms**, for every condition. The
live API request default remains **10000 ms** and its maximum remains 60000 ms.
The configuration accepts integer values in 1..60000. This is the whole-request
deadline, including cache, backoff and attempts; the existing per-attempt timeout
of at most 30 seconds and the shared 20000 ms full-completion SLO are unchanged.
Both run metadata and the frozen configuration record the benchmark deadline.

`--prompt-ids-file <path>` accepts a nonempty JSON list of unique prompt IDs and
requires `--split calibration`. Unknown IDs and IDs from other splits are rejected
before storage or provider dispatch. Filtering retains the frozen split's order;
`--seed` still controls prompt/condition shuffling. Use the original run's seed.
The dataset checksum/version, human subset and repeat subset are unchanged. Every
selected prompt runs A, B, C-cold and C-warm, subject to the existing spend-cap
abort behavior. No unselected prompt is dispatched. The output records the
selected IDs, IDs-file path and selected prompt count. Output creation is
exclusive, including when another process creates that path during setup.

For the 22 missing calibration pairs described in the brief, the IDs-file content
is the following (save separately when preparing the authorized run):

~~~json
["complex-002", "complex-006", "complex-007", "complex-018", "complex-020",
 "complex-024", "complex-025", "complex-026", "complex-029", "complex-031",
 "complex-033", "complex-040", "complex-041", "complex-042", "complex-043",
 "complex-044", "medium-007", "medium-027", "medium-038", "medium-042",
 "medium-043", "medium-049"]
~~~

Example commands for a subsequent, separately authorized real run (not executed
as part of this change); replace `<original-seed>` with the base artifact's seed
and enable the existing real-spend confirmation in the environment:

~~~text
./.venv/Scripts/python.exe -m benchmark.run --split calibration --prompt-ids-file results/calibration-missing-ids.json --seed <original-seed> --real --storage postgres --output results/calibration-subset-60s.json
./.venv/Scripts/python.exe -m benchmark.merge results/calibration-real-4.json results/calibration-subset-60s.json results/calibration-merged-60s.json
~~~

The offline function `benchmark.merge.merge(base_path, subset_path, output_path)`
and its CLI create a new artifact. For every ID actually present in the subset,
all its base records are replaced with the subset's records. Other base records
retain their values. Each record names its originating file in `source`; the
artifact records `merged=true`, both input paths, exact sorted replaced IDs and
both source metadata snapshots. Matching split, seed, dataset, models, prices,
thresholds and provider mode are required; the benchmark deadline may differ.

Replaced base records remain in `superseded_records` for accounting and audit.
Totals are recomputed with Decimal from every attempt in `records` **plus**
`superseded_records`, separately for inference and evaluation. This includes all
failed/retried calls and full unknown-outcome charges from both files; rerunning
a prompt never refunds its earlier spend. Record cost snapshots and source
totals are not used to calculate merged totals. Missing attempt exports require
recovery of the source first; duplicate attempt IDs, unknown prompt IDs and
empty subsets are rejected. Repeated merges preserve earlier superseded charges
and original record provenance. Neither input is rewritten; an existing output,
including either input path, is always refused with exclusive file creation.

The merged artifact is a derived audit/ratings artifact with a new ID and
`storage="merged-artifact"`, not a durable ledger run. Final is true only when
both inputs are final. Source analyses are retained only in `source_metadata`;
there is no merged `analysis`, since old quality/savings statistics would be
stale and would omit rerun charges. In particular, active-record costs alone do
not equal total incurred spend. Use original run artifacts for judging or ledger
recovery, and do not treat the merge as a new measured quality or savings claim.
This change ran only synthetic mock fixtures and local-infrastructure tests;
no real calls, calibration/subset execution or historical result edits occurred.


## Output limit for the full rerun (2026-10-17)

The full rerun uses **1024 output tokens for every generation condition**: A, B,
C-cold and C-warm. `config/models.json` sets `benchmark_max_tokens: 1024`;
`benchmark.run` passes the value explicitly to each request. Custom configurations
may select an integer in 1..2048; omitting the key defaults to 1024. Run metadata
and the frozen configuration record the value used. Worst-case cost reservations
use that same request limit before provider dispatch, including retries/fallback.
Warm cache hits make no paid inference attempt.

This follows the user decision after **70 of 88 subset outputs were truncated
at 512**. Truncated outputs still score 1 under the existing completion rule.
The live API default remains 512; generation temperature, models, pricing, spend
caps, deadlines and SLO retain their current values. Earlier 512-token artifacts
remain unchanged for audit. This implementation has not executed the full rerun
or any real/calibration/subset run; it makes no new quality or savings claim.


## Judge calibration for the 25 rated run-5 pairs (2026-10-17)

The default command is a read-only plan:

~~~text
./.venv/Scripts/python.exe -m benchmark.calibrate_judge
~~~

It reads `results/calibration-real-5.json`, the 25 rows in
`results/ratings-calibration-run5/ratings-blinded.json`, and the matching
`mapping-private.json`. It prints 25 pairs and 50 planned calls, without provider
initialization, storage access or output creation. Alternate paths are available
through `--artifact`, `--ratings`, `--mapping`, `--output` and `--report`.

For a subsequently authorized paid run, add `--real` and set
`ROUTER_CONFIRM_REAL_SPEND=YES`. Both are required. The current configured judge
must be `mid`; its current prices, global USD cap and evaluation cap (at most
USD 3) apply through shared Postgres reservations. `DATABASE_URL` and `REDIS_URL`
use the same defaults as `benchmark.judge`. No paid run was executed for this
change.

Blind IDs join ratings to the private prompt mapping, then prompt ID and
condition identify source records. Left B swaps the human scores into human_b
and human_a. Both source answers must be completed HTTP 200 outputs and match
the blinded content exactly. Duplicate IDs, invalid human ratings, missing pairs,
changed prompts, checksum mismatches and inconsistent stratification are rejected
before judging. The existing exporter omits right_condition; its complement is
inferred, and an explicitly supplied right_condition must agree.

Only the 50 rated A/B answers are judged, individually with the original prompt
and matched system instruction. Quality loads the frozen
`monitoring/judge_prompt_v1.md`; the judge receives no condition, blind ID, human
rating, tier or task-type metadata. Responses must be exactly a JSON object with
one score key and an integer 1..5. Strings, floats, booleans, extra/duplicate keys,
missing scores, Markdown wrappers and non-normal completion are unknown. Invalid
responses retain their usage-based evaluation charge. No score is guessed.

`results/judge-calibration-run5.json` is the pair list accepted by
`validate_calibration`; `results/judge-calibration-run5-report.json` contains that
validator's report, judge model/source run and any unknown outputs identified by
blind ID, prompt ID, side and ledger attempt ID. Failed gates or unknown scores
produce exit status 1 after writing both artifacts. Existing pair or report
files are refused, including exclusive-creation races, before provider calls.
A stable calibration attempt ID reuses terminal ledger jobs without replaying
paid calls; failed/unknown jobs are not automatically retried. Provider timeouts
retain the existing reservation/recovery semantics. Neither source inputs nor
existing generation result artifacts are rewritten.
