# Benchmark and human evaluation workflow

No held-out benchmark has been run. All executable entry points use mock providers
only. Current results are development fixtures, not measured provider savings,
quality or latency. The API key environment is never needed for this workflow.

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
reduction and source=mock. All generation settings are matched: the fixed system
prompt, max_tokens=512, temperature=0. A uses only the configured frontier model;
B has no cache; C starts cold and is then repeated warm. A/B/C-cold are interleaved
in a seeded random order per prompt; warm follows the entire cold pass. The unique
cold repetition rate is 0 and the warm repetition rate is 1. These are cache
scenarios, not an estimate of a production workload's repetition rate.

## Preflight and protected held-out execution

The CLI defaults to dev. Held-out needs an explicit --allow-held-out-mock flag
and --dev-results pointing to a final mock dev artifact. Dataset/model/price/threshold
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

Each record is checkpointed with atomic JSON replacement after its durable ledger
writes. An interruption before the checkpoint can leave an unrecorded request in
the ledger; recovery still includes its full spend in the run total. It cannot
honestly reconstruct condition attribution from a missing record, so it reports
incomplete coverage rather than inventing an observation.

~~~text
python -m benchmark.recover results/dev-mock.json
~~~

Wait until the 30-second call timeout plus 30-second grace has passed for aged
unknown attempts. Recovery performs no provider calls and never replays an old
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
No actual ratings were supplied: the operational gate is pending and disabled.

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

Paired non-inferiority compares B-A and, only with a validated judge, C-A. Use
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
