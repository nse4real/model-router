# Environment parity verification (2026-10-17)

## Full-completion SLO correction (2026-10-17)

Config and Health defaults are **20000 ms** for the live API, benchmark generation
and judging. Full-completion latency runs from provider dispatch to the final
response, before settlement/storage work. Each generation/judge run gets fresh
health for all its models; API health persists across requests. Regression tests
exercise the boundary, no initial cooldown, persistent API health, memory/Redis
judge isolation, idempotent outcomes and refreshed SQL latency exports.

The required commands were observed passing with local services enabled:
`ROUTER_TEST_POSTGRES_DSN=postgresql://router:router@127.0.0.1:5432/router` and
`ROUTER_TEST_REDIS_URL=redis://127.0.0.1:6379/0`.

| Interpreter | `scripts/manage.py test` | `scripts/manage.py lint` |
|---|---|---|
| `./.venv/Scripts/python.exe` | 169 tests, OK, 25.745 s; zero failures/errors/skips | Passed: 54 Python files |
| `./.venv-real/Scripts/python.exe` | 169 tests, OK, 36.497 s; zero failures/errors/skips | Passed: 54 Python files |

The focused SLO module passed all 17 tests with both local service URLs enabled.
Final config has only `slo_ms: 20000`; the separate benchmark field and selection
path have been removed. Config fallback, Health and shared admission also default
to 20000 ms. The docs search found no obsolete SLO values, and `git diff --check`
passed. Code and tests were unchanged after the two full suite runs.

Files changed for this correction (pre-existing unrelated edits were preserved):

- `config/models.json`
- `routing/config.py`, `routing/latency.py`, `routing/admission.py`, `routing/service.py`
- `api/app.py`
- `monitoring/ledger.py`, `monitoring/quality.py`
- `benchmark/run.py`, `benchmark/judge.py`, `benchmark/results.py`
- `tests/test_benchmark_slo.py`
- `README.md`, `docs/DECISIONS.md`, `docs/BENCHMARK.md`, `docs/TEST_MATRIX.md`, `docs/VERIFICATION.md`

Only mock providers and local infrastructure were exercised. Real-provider latency,
calibration and held-out behavior remain unverified; none of those commands were
run. This task did not edit PLAN.md or any calibration artifacts. A final
integrity check observed `results/calibration-real-3.json` change during
verification (SHA-256 `1549ee8d...` to `6c6feb78...`); no command in this task
writes that file. It was left untouched. PLAN.md and the other four calibration
JSON hashes remained stable between the recorded integrity checks. The source
of that concurrent artifact change is unverified. No API key values were read
or printed. No commit, push or branch change was made.

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

## Verification for the temperature correction

Local service environment for both full suites (no .env or provider keys read):

~~~powershell
$env:ROUTER_TEST_POSTGRES_DSN='postgresql://router:router@127.0.0.1:5432/router'
$env:ROUTER_TEST_REDIS_URL='redis://127.0.0.1:6379/0'
~~~

| Exact command | Observed result |
|---|---|
| ./.venv/Scripts/python.exe scripts/manage.py test | Exit 0: **122 tests, 122 passed, 0 failures/errors, 0 skipped**, 9.651 s |
| ./.venv-real/Scripts/python.exe scripts/manage.py test | Exit 0: **122 tests, 122 passed, 0 failures/errors, 0 skipped**, 17.049 s |
| ./.venv/Scripts/python.exe scripts/manage.py lint | Exit 0: **51 Python files**, syntax/hygiene lint passed |
| ./.venv-real/Scripts/python.exe scripts/manage.py lint | Exit 0: **51 Python files**, syntax/hygiene lint passed |

Both suites exercised local HTTP transport, four Postgres integration methods and
one Redis integration method. Provider calls used mocks or in-memory LiteLLM
stubs only. No calibration or held-out execution, real provider call, API key
inspection, commit, push or branch change occurred. Hash checks before and after
verification confirm PLAN.md, monitoring/ledger.py and results/calibration-real.json
are unchanged. Existing unrelated working-tree edits were preserved.

The changed files for this correction are api/contracts.py, benchmark/run.py,
tests/test_stage1.py, tests/test_stage2.py, tests/test_stage4.py,
tests/test_resolved_models.py, README.md and the four required docs:
BENCHMARK.md, DECISIONS.md, VERIFICATION.md and TEST_MATRIX.md.

## Historical environment parity verification

These results supersede the historical counts below. The only changes for this
brief are tests/test_regressions.py, tests/test_stage3.py, scripts/lint.py,
docs/DECISIONS.md and docs/VERIFICATION.md. Product code is unchanged by this
brief; the pre-existing working-tree changes remain in place.

PowerShell environment for both full-suite commands (explicit IPv4, Redis DB 0):

~~~powershell
$env:ROUTER_TEST_POSTGRES_DSN='postgresql://router:router@127.0.0.1:5432/router'
$env:ROUTER_TEST_REDIS_URL='redis://127.0.0.1:6379/0'
~~~

| Exact command | Observed result |
|---|---|
| .\.venv\Scripts\python.exe scripts/manage.py test | Exit 0: **120 discovered, 120 passed, 0 failures/errors, 0 skipped**, 4.822 s; Python 3.15.0 |
| .\.venv-real\Scripts\python.exe scripts/manage.py test | Exit 0: **120 discovered, 120 passed, 0 failures/errors, 0 skipped**, 6.233 s; Python 3.12.10 |
| .\.venv\Scripts\python.exe scripts/manage.py lint | Exit 0: **51 Python files**, supplied syntax/hygiene checker |
| git diff --check | Exit 0; no whitespace errors; pre-existing config/models.json line-ending advisory only |

Both full suites exercised HTTP transport, four real local Postgres integration
methods and one real local Redis integration method. Provider calls remained
mocked or used in-memory LiteLLM stubs. No API key values were read or printed,
no .env was loaded by the diagnostic commands, and no real provider call,
calibration/held-out run, commit, push or branch change was performed.

## Root-cause experiments

Before editing fixtures, this exact focused command was run with each Python:

~~~powershell
.\.venv-real\Scripts\python.exe -m unittest tests.test_regressions.RegressionTests.test_injected_deadline_mid_chain tests.test_stage3.QualityAndPolicyTests.test_renewal_failure_abandons -v
.\.venv\Scripts\python.exe -m unittest tests.test_regressions.RegressionTests.test_injected_deadline_mid_chain tests.test_stage3.QualityAndPolicyTests.test_renewal_failure_abandons -v
~~~

The originals produced **2 failures in 0.079 s (exit 1)** on Python 3.12 and
**2 passes in 0.056 s (exit 0)** on Python 3.15. The failures were the reported
0-versus-2 provider calls and still-valid permit. They occur without running the
rest of the suite, ruling out suite ordering as a required cause.

An isolated stdin Python probe installed this import guard before loading the
two test names with unittest.defaultTestLoader.loadTestsFromNames:

~~~python
class NoLiteLLM:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'litellm' or fullname.startswith('litellm.'):
            raise AssertionError('unexpected LiteLLM import')
sys.meta_path.insert(0, NoLiteLLM())
~~~

The original pair still failed twice in .venv-real (0.067 s), and printed
`litellm loaded: False` using membership in sys.modules. It passed twice in
.venv (0.066 s), also without loading LiteLLM. importlib.util.find_spec confirmed
LiteLLM is installed only in .venv-real. Thus Python 3.12 versus 3.15 timer
behaviour explains the difference without importing the real dependency.

| Diagnostic | Python 3.12.10 | Python 3.15.0 |
|---|---|---|
| time.get_clock_info('monotonic').implementation | GetTickCount64() | QueryPerformanceCounter() |
| Reported resolution / loop._clock_resolution | 0.015625 s | 0.0000001 s |

Tracing asyncio.wait_for measured a warmed 5 ms cache timeout at 4.655 ms via
perf_counter, before any provider call, while the injected clock remained zero.
Cold cache-thread startup also consumed the 5 ms wall budget (6.617 ms observed).
For renewal, 1 ms and 5 ms timers can enter the same ready batch; completing the
provider can cancel the renewal task before it calls store.renew. The local
Python 3.12 asyncio/base_events.py schedules callbacks through
`end_time = self.time() + self._clock_resolution`.

A second isolated probe warmed concurrent.futures' executor import with
`asyncio.run(asyncio.to_thread(lambda: None))` inside an async wrapper, then
patched asyncio.events.new_event_loop to create an otherwise normal loop with
`loop.time = time.perf_counter` and
`loop._clock_resolution = time.get_clock_info('perf_counter').resolution`.
Both original tests passed under Python 3.12 (0.069 s). A cold high-resolution
probe still failed the cache fixture, so executor startup is an additional
reason the original 5 ms test was not a deterministic injected-clock test.
No such patch is applied to product code or committed tests.

The corrections retain all original outcome assertions. The deadline fixture
uses a 1 s virtual budget and advances the injected clock 0.6 s per failed
provider call, requiring exactly two calls, status 504 and virtual time 1.2 s.
The renewal fixture gates completion on an observed renewal event instead of
assuming a 1 ms timer beats a 5 ms timer. DECISIONS.md explains why these are
fixture corrections permitted by the brief rather than relaxed product rules.

The same exact focused commands after correction passed **2/2** in each venv:
0.077 s on Python 3.15 and 0.087 s on Python 3.12. Separate in-memory Python 3.12
mutation probes made the corrected deadline fixture fail when Router ignored
the injected clock, and the renewal fixture fail when store.renew falsely
returned True (one assertion failure and zero errors for each probe).

The initial requested lint run returned exit 1 with 404 diagnostic lines from
.venv-real installed packages, including documentation separators misidentified
as merge markers. Excluding that virtual environment, consistent with the
existing .venv exclusion, produced the 51-project-file pass above. The patch
helper failed to write twice; workspace edits succeeded via Python.

PLAN.md SHA-256 before and after:
`3de35537540c0b4cd0b77313a07d87018ff0cbe66b20d6dc27c448ef80d6fd76`.
All requested suite/lint checks are verified. Direct LiteLLM import under Python
3.15 was not attempted because it is not installed there. Real provider
behaviour, model availability, billing and production latency remain unverified;
no claim about them follows from mock/stub tests. Ruff and standalone benchmarks
were not run.

# Alias and resolved-snapshot verification (2026-10-17)

Current verification supersedes the historical counts below. Configured model:
`anthropic/claude-haiku-5-5`; user-approved caps: **USD 15 global / USD 3 evaluation**.
The supplied provider model-list finding says the dated Haiku ID was not listed;
no live provider availability or billing verification was attempted.

PowerShell environment for the local Compose services:

~~~powershell
$env:ROUTER_TEST_POSTGRES_DSN='postgresql://router:router@127.0.0.1:5432/router'
$env:ROUTER_TEST_REDIS_URL='redis://127.0.0.1:6379/15'
~~~

| Exact command | Observed result |
|---|---|
| .\.venv\Scripts\python.exe scripts/manage.py test | Exit 0: **120 discovered, 120 passed, 0 failures/errors, 0 skipped**, 6.908 s |
| .\.venv\Scripts\python.exe scripts/manage.py lint | Exit 0: **51 Python files**, supplied syntax/hygiene checker |

The complete suite exercised HTTP transport, four local Postgres integration
methods (including migration of an existing attempt table) and one local Redis
integration method. Eight methods in tests/test_resolved_models.py cover stubbed
Anthropic/OpenAI response snapshots distinct from requested aliases, unchanged
configured prices, absent response IDs, rejected responses, settlement failure,
judge scores, cache provenance, per-attempt exports, run-scoped summary counts,
recovery and repeat SQLite migration. tests/test_pricing.py now expects the alias.

The first focused snapshot run exposed two fixture issues: a SQLite connection
needed explicit close on Windows, and recovery correctly refreshed judge costs.
Both fixtures were corrected. A full run with localhost was interrupted while
Postgres connection establishment waited on the local address; a diagnostic
trace confirmed it was waiting in psycopg.connect. The documented explicit IPv4
addresses avoid that wait. The next full run discovered 119 tests with one error:
the exporter required request_id on an existing request-less redaction fixture.
The exporter now handles it, and the final 120-test run above passed.

Changed for this brief: fallback/providers.py, monitoring/ledger.py,
monitoring/schema.sql, monitoring/quality.py, routing/service.py,
benchmark/run.py, benchmark/results.py, tests/test_pricing.py,
tests/test_services.py, new tests/test_resolved_models.py, README.md,
docs/DECISIONS.md, docs/TEST_MATRIX.md and docs/VERIFICATION.md.
The existing config already contained the alias and USD 15 cap. Pricing logic
and historical mock artifacts/descriptions were preserved. PLAN.md remains
unchanged (SHA-256 3de35537540c0b4cd0b77313a07d87018ff0cbe66b20d6dc27c448ef80d6fd76).

All provider paths used mocks or in-memory LiteLLM stubs. No API key values were
read or printed, no real provider call or held-out/calibration real run was made,
and no commit, push or branch change was performed. Real provider response
formats, model availability, billing, tokenizer bounds and real quality remain
unverified. Ruff, standalone benchmarks and deployment checks were not run.

# Historical tier pricing verification (2026-10-17)

These results supersede historical test/lint counts below. The cheap model now
uses the provider alias `anthropic/claude-haiku-5-5` (Claude Haiku 5.5), replacing the
retired model reported by the user. Its ID and prices were supplied by the user
and were not independently verified against the provider page. Input/output
USD per million: $0.10/$0.50 through 100,000 input tokens, $0.50/$2.50 above.
The per-request tier assumption is pending user confirmation in DECISIONS.md.

| Exact command | Observed result |
|---|---|
| python scripts/manage.py test | Exit 0: **101 discovered, 98 passed, 0 failures/errors, 3 skipped**, 2.746 s |
| python scripts/manage.py lint | Exit 0: **49 Python files**, supplied syntax/hygiene checker |
| python -m benchmark.diagnostics | Exit 0: dev only, **20 correct / 43 (46.51%)**, thresholds [2,5] |
| python -m unittest tests.test_pricing -v | Exit 0: **16 passed**, no skips, 0.054 s |

The three skips remain HTTP transport (FastAPI/httpx not installed), Postgres
(local DSN not supplied) and Redis (local URL not supplied). Ruff was not run.
All executed provider paths used mocks or in-memory LiteLLM stubs; no real
provider call or API key was used. No held-out benchmark or standalone benchmark
run was performed. Historical ignored results/ artifacts were not edited and
are not evidence for the new prices. Model availability, provider billing,
real tokenizer bounds and external integrations remain unverified.

Tests cover exact 100000/100001 usage, 89999/90000/99999 reservation estimates,
actual-usage settlement on success/error, unchanged flat models, malformed tier
and flat data, pre-dispatch budget rejection, and automatic tier-sensitive cache
versions. The public input cap remains 32 KB; large boundary requests are
synthetic internal fixtures. Price version is the dated string
`user-supplied-2026-10-17-v2`, plus a canonical price-content digest in effective
cache keys, ledger rows and benchmark price metadata.

The first focused run found one fixture failure: the shell had encoded a literal
accented character as ASCII, invalidating its expected UTF-8 byte count. The
fixture now uses an explicit Unicode escape; all 16 focused tests then passed.
The patch helper failed twice; Python workspace writes succeeded. PLAN.md was
unchanged, and no commit, push or branch change was issued.

Files changed for this brief: config/models.json, routing/config.py,
tests/test_pricing.py, tests/test_regressions.py, README.md, docs/DECISIONS.md,
docs/TEST_MATRIX.md and docs/VERIFICATION.md. Pre-existing uncommitted edits
were preserved.

# Historical round-two review verification (2026-10-16)

These historical results superseded the earlier implementation record retained below.
Commands ran directly through the working PowerShell terminal in
D:/Projects/model_routing_system using Python 3.15.0. No provider keys were read,
no real provider/network calls were made by tests, and no held-out benchmark ran.
Real adapter tests supply an in-memory LiteLLM module. No commit, push or branch
change was issued. PLAN.md is byte-for-byte unchanged (SHA-256
3de35537540c0b4cd0b77313a07d87018ff0cbe66b20d6dc27c448ef80d6fd76).

## Historical observed commands and results

The test, lint and diagnostic commands below were rerun after restoring the
original classifier expectations. The benchmark, judge and load results are
retained from the earlier review; those commands were not rerun for this revert
and are not validation of the restored classifier's routing outcomes.

| Exact command | Observed result |
|---|---|
| python scripts/manage.py test | Exit 0: 85 discovered, **82 passed, 0 failures/errors, 3 skipped**; post-revert run 2.494 s |
| python scripts/manage.py lint | Exit 0: **48 Python files**, supplied AST/syntax/whitespace/conflict-marker checker |
| python -m benchmark.diagnostics | Exit 0: original frozen rules, 20/43 (46.51%), dev only; thresholds [2,5] |
| python -m benchmark.run --split dev --storage test-double --output results/review-round2-dev-mock.json | Exit 0: 43 dev prompts x 4 conditions = 172 records, final=true; mock inference/total USD 0.02011130 before judging |
| python -m benchmark.judge results/review-round2-dev-mock.json | Exit 0: data_source=mock, final=true, quality_claim=withheld; all costs reconciled |
| python -m benchmark.load_test --transport in-process --n 100 --concurrency 10 --duration 1 --output results/review-round2-load-mock.json | Exit 0: 100 requests, 0 errors, local overhead P95 1.9914 ms <50 ms; elapsed 0.9941 s; mock core only |

The three skips are APITests.test_transport (FastAPI/httpx missing),
PostgresIntegrationTests.test_cross_worker_admission (local DSN not supplied),
and RedisIntegrationTests.test_exact_rate_and_probe_lease (local URL not supplied).
Ruff was not installed/run; the reported lint is the supplied narrower offline
checker. No HTTP load, Docker, Grafana or Prometheus deployment check was run in
this review. The driver-contract tests do not prove live Postgres MVCC/timeouts,
and a transport stub does not prove FastAPI serialization/startup correctness.
Real model availability, tokenizer bounds, usage, current prices, GBP conversion,
human calibration, held-out quality/cost and real reproducibility remain unverified.
At that review, the shipped prices were illustrative mock prices.

## Failure-before-fix evidence and iterations

- python -m unittest tests.test_review_round2 -v initially exited 1: 14 tests,
  reported failures=5, errors=9 (subtests included). They reproduced authorization
  leakage, late success after finish, paid probe usage loss, judge cooldown bypass
  and the separated claim/recovery race; new queue/key/metadata/guard APIs were absent.
- python -m unittest tests.test_review_round2.ReviewClassifierTests -v exited 1:
  2 tests, failures=5 including subtests, because expectations contradicted the
  original frozen rules. The diagnostic reported 20/43. The regression expectations
  now assert the original rules and exact dev agreement.
- The first python scripts/manage.py test after queue integration exited 1:
  78 discovered, failures=3, errors=1, skipped=3. Cache tests expected synchronous
  persistence; the 2 ms real-time timeout could expire before dispatch. Fixtures
  now explicitly drain asynchronous writes and isolate provider timeout with a
  20 ms/no-cache request. The deterministic deadline regressions remain separate.
- Subsequent python scripts/manage.py test runs passed at 80, 83 and 85 tests as
  regression coverage grew; all retained 3 optional integration skips.
- A read-only in-memory harness loaded monitoring/ledger.py and metrics.py using
  git show 79131b2:<path>. Running
  ReviewPostgresContractTests.test_postgres_repeatable_snapshot_and_deadline_timeouts
  against that historical transaction/metrics pair reproduced exactly one assertion
  failure: requests=0, attempts=1, spend=1.0. The harness expected that historical
  failure and exited 0. The same test passes with REPEATABLE READ in the fixed code.
  No checkout or workspace rollback was used.
- python -m unittest tests.test_review_round2 -v passed once the initial 14
  regressions were fixed, then passed all 19 tests after adding the concurrent
  claim, Postgres contract and API composition tests; two more tests were added
  for independently slow quality submission and guarded real judging.
- The edit helper could not write a file on its first attempt or retry; terminal
  PowerShell/Python writes succeeded. No terminal command failed to start.

Finding-by-finding test names and integration gaps are in TEST_MATRIX.md. All nine
findings are fixed in code with offline regressions. The classifier was reverted
to the original frozen heuristic rules, with thresholds [2,5] unchanged. Dev
agreement is low: 20/43 (46.51%). No held-out data was used. Answer quality is the
outcome that matters; this diagnostic does not measure it or establish generalisation.
The dev benchmark/load artifacts live in ignored results/; no held-out prompt was
dispatched. Source integrity and final whitespace results are recorded below.

## Files changed in this review

- .env.example
- README.md
- api/app.py
- benchmark/evaluate.py
- benchmark/judge.py
- benchmark/recover.py
- benchmark/results.py
- benchmark/run.py
- cache/exact.py
- config/models.json
- docs/BENCHMARK.md
- docs/DECISIONS.md
- docs/TEST_MATRIX.md
- docs/VERIFICATION.md
- fallback/providers.py
- monitoring/ledger.py
- monitoring/metrics.py
- monitoring/quality.py
- monitoring/schema.sql
- monitoring/security.py
- routing/admission.py
- routing/config.py
- routing/service.py
- tests/test_review_round2.py
- tests/test_stage1.py
- tests/test_stage2.py
- tests/test_stage4.py

Final `git diff --check` exited 0. Final PLAN.md SHA-256 matched the original.

## Historical implementation verification

# Verification record

Executed on Windows 11 with Python 3.15.0. Commands used Node child_process.execFile
in D:/Projects/model_routing_system after the unified exec helper failed before
process creation. No provider credentials were read or required. No real-provider
code path was exercised. No held-out prompt was executed. No commit, push or
branch-changing command was issued.

## Final checks actually run

| Exact command | Actual result |
|---|---|
| python scripts/manage.py test | Exit 0: 64 discovered, **61 passed, 0 failed/errors, 3 skipped**, 1.533 s |
| python scripts/manage.py lint | Exit 0: **46 Python files** passed the offline AST/syntax/whitespace/conflict-marker checker; Ruff was not run |
| python -m benchmark.run --split dev --storage test-double --output results/final-dev-mock.json | Exit 0: 43 dev prompts x 4 conditions = 172 records, final barrier satisfied, mock only |
| python -m benchmark.judge results/final-dev-mock.json | Exit 0: durable diagnostic mock judging completed; final=true; quality claim withheld |
| python -m benchmark.load_test --transport in-process --n 100 --concurrency 10 --duration 1 --output results/final-load-mock-core.json | Exit 0: 100/100 completed, 0 errors; local core P95 0.6687 ms <50 ms; mock, not HTTP/provider performance |
| python -m benchmark.diagnostics | Exit 0: dev tier-label agreement 20/43 (46.51%); not answer quality |
| docker compose config --quiet | Exit 125: compose unavailable; unknown --quiet at Docker root; config-file access warning |
| docker compose version | Exit 1: docker compose is not an available subcommand in this environment |
| git diff --check | Exit 0, no output; only tracked-file whitespace is checked, offline lint also checks new Python files |

Three skips: APITests.test_transport (FastAPI/httpx missing),
PostgresIntegrationTests.test_cross_worker_admission (local DSN not supplied),
RedisIntegrationTests.test_exact_rate_and_probe_lease (local URL not supplied).
Docker daemon was unavailable, so neither infrastructure test could be provisioned.
No Docker/Grafana/Prometheus/HTTP deployment correctness is claimed.

Mock core load environment: Windows-11-10.0.26300-SP0, Python
3.15.0, 20 logical CPUs.
Windows did not supply processor/architecture text. N=100, C=10, D=1 second,
actual elapsed 0.9916 seconds. This is a local core diagnostic;
it does not satisfy the HTTP load criterion. Raw generated results are in the
ignored results/ folder. No cost-saving or real quality result is reported.

## Stage checkpoints and failures encountered

The same exact command, python scripts/manage.py test, was run after each stage:

| Checkpoint | Discovered | Passed | Failed/errors | Skipped |
|---|---:|---:|---:|---:|
| Classifier/contracts/mock/policy | 8 | 8 | 0 | 0 |
| Spend/ledger/cache/fallback | 25 | 24 | 0 | 1 |
| Quality/latency | 41 | 40 | 0 | 1 |
| Initial metrics/benchmark tooling | 51 | 50 | 0 | 1 |
| Final expanded regression/tooling suite | 64 | 61 | 0 | 3 |

All stage checkpoints were green before proceeding. During later hardening, a
cache-return indentation edit caused 1 failure and 18 error reports on a 51-test
run. The suite caught it; it was fixed and the immediate 51-test rerun passed.
An added threshold fixture later failed once on a 61-test run because it reused
an already partially filled recovery window; the fixture was isolated correctly,
and the 61-test rerun passed. Both intermediate failures are retained in
COMMAND_LOG.json rather than omitted from the history.

The first full dev mock command:
python -m benchmark.run --split dev --storage test-double --output results/benchmark-mock-dev.json
returned exit 1 because unknown quality=None was compared to an integer during
reporting. Pending acceptance now reports null; a regression assertion was added.
The next command:
python -m benchmark.run --split dev --storage test-double --output results/benchmark-mock-dev-verified.json
returned exit 0. The final dev command above also returned exit 0. The initial
artifact was preserved, and no automatic replay was used. All these are dev-only
mock runs with separate test-double files, never held-out runs.

Earlier local core load diagnostic:
python -m benchmark.load_test --transport in-process --n 100 --concurrency 10 --duration 1 --output results/load-test-mock-core.json
returned exit 0 with zero errors. The final workload above is the current check.

## Environment probes and installation failures

- Get-ChildItem / rg / Get-Content and a later Get-Location via the unified shell
  helper failed before any process was created (setup refresh helper error).
- python -c 'import sys; print(sys.version); import pypdf; print("PYPDF_OK")'
  printed Python 3.15.0 and exited 1: pypdf unavailable. PDF extraction instead used
  C:/Program Files/Git/mingw64/bin/pdftotext.exe -layout
  D:/Projects/model_routing_system/Project_13_Model_Routing_System.pdf -
  and exited 0 with both pages extracted in full.
- py --list-paths, where.exe pdftotext, where.exe uv,
  python -m pip --version and python -m pip list --format=columns succeeded.
- uv python find 3.13 exited 2: default cache path access denied.
- uv --cache-dir D:/Projects/model_routing_system/.tooling/uv-cache python install
  3.13 --install-dir D:/Projects/model_routing_system/.tooling/python
  exited 1: network permission blocked the isolated Python download.
- python -m pip install --target .tooling/deps --retries 0 --timeout 5 fastapi httpx ruff
  exited 1: no available FastAPI distribution. No runtime dependency installation
  succeeded, so HTTP tests and Ruff remain unverified.
- docker info --format {{.ServerVersion}} exited 1: Docker config access warning
  and no running docker_engine named pipe.
- git status --short, git ls-files, git rev-parse HEAD, git log -3 --oneline,
  git check-ignore -v .gitignore results/benchmark-mock-dev-verified.json .tooling,
  git show --no-patch --oneline 9067435, git ls-files --stage and
  git show HEAD:.gitignore were read-only repository checks. No index mutation.

COMMAND_LOG.json contains the exact validation commands and exit codes in their
recorded order, including repeated test runs. Independent checks ran concurrently;
its order is completion order within those groups. File creation/inspection used
Node filesystem APIs. A one-off Python script froze the seeded data assignments;
this did not dispatch prompts. The only web access fetched public dataset/licence
text from GitHub; no model endpoint was contacted.

## Source integrity and review

Source SHA-256 values recorded before implementation:

- PLAN.md: 3de35537540c0b4cd0b77313a07d87018ff0cbe66b20d6dc27c448ef80d6fd76
- Project_13_Model_Routing_System.pdf: c4f7576de3ad946b83dc8a34e5a365b6be749e713881d5f7078e87176f7b76b1

The final integrity check verifies these bytes stayed unchanged. The requested
9067435 checkpoint is available. A later read-only check observed HEAD at
28a7735dd726b3bd7d855c333e595c1fd1a98a77; this agent issued no commit/checkout/branch
commands and did not rewrite that state. New implementation files remain available
for user review without staging or committing them.

Final source-integrity check: both SHA-256 values matched exactly. Final read-only
git diff --check returned exit 0. The implementation inventory lists 73 files.

## Ledger timeout and durable response brief verification (2026-10-17)

Local Postgres and Redis were enabled for both required full-suite runs. Every
provider used a mock or in-process stub; no real provider endpoint was called.
No calibration or held-out run, key-value inspection, .env loading, commit, push
or branch change was performed. All database mutation tests used isolated
router_test_* schemas; historical evidence was queried read-only.

```powershell
$env:ROUTER_TEST_POSTGRES_DSN='postgresql://router:router@127.0.0.1:5432/router'
$env:ROUTER_TEST_REDIS_URL='redis://127.0.0.1:6379/0'
./.venv/Scripts/python.exe scripts/manage.py test
./.venv-real/Scripts/python.exe scripts/manage.py test
./.venv/Scripts/python.exe scripts/manage.py lint
./.venv-real/Scripts/python.exe scripts/manage.py lint
```

| Final check | Discovered / executed / passed | Failed / skipped | Duration |
|---|---|---|---|
| .venv Python 3.15 full suite | 152 / 152 / 152 | 0 / 0 | 23.756 s |
| .venv-real Python 3.12 full suite | 152 / 152 / 152 | 0 / 0 | 30.918 s |
| .venv syntax/hygiene lint | 53 Python files | 0 errors | passed |
| .venv-real syntax/hygiene lint | 53 Python files | 0 errors | passed |

Final full-suite logs: results/ledger-timeout-venv-tests.log and
results/ledger-timeout-venv-real-tests.log. These are new ignored verification
artifacts; calibration-real-v2.json was not overwritten. The first exploratory
full run had 147 tests and one failure from the existing concurrent-cap test's
scheduling assumption; see DECISIONS.md for the corrected assertion. A later
149-test Python 3.12 run passed before the last three regressions were added.
The final counts above supersede those intermediate runs.

With both service variables absent, this command verifies the fallback module:

```powershell
Remove-Item Env:ROUTER_TEST_POSTGRES_DSN -ErrorAction SilentlyContinue
Remove-Item Env:ROUTER_TEST_REDIS_URL -ErrorAction SilentlyContinue
./.venv/Scripts/python.exe -m unittest discover -s tests -p test_ledger_timeouts.py -v
```

Result: 20 discovered, 12 executed/passed, 8 Postgres tests skipped, zero failures,
0.907 s. Log: results/ledger-timeout-memory-tests.log. The same new module's eight
Postgres tests ran in both full suites, alongside the existing Postgres/Redis
integration checks. No provider network calls are needed for any of them.

Evidence: the unmodified ledger's idle N=8 run completed 160/160 cycles. An
isolated schema with a deterministic 250 ms insertion delay then produced eight
QueryCanceled / SQLSTATE 57014 failures under the old 200 ms budget. The regression
repeats that old budget explicitly and requires eight failures, then uses the new
2000 ms default and requires 8/8 settled attempts, USD 0.008 total fixture cost.
An ordinary pg_sleep(0.25) read also succeeds under an expired request context,
proving storage and request budgets are separate. Real Postgres triggers inject
serialization failures twice during admission and QueryCanceled once during
settlement, proving bounded retry, unchanged attempt identity, durable capture,
actual measured charges and exactly one fake provider dispatch.

SQLite/Postgres shared contracts cover lost reserve/dispatch/settle commit
acknowledgements, full storage loss immediately after a paid response, restart of
the local store, idempotent unknown reconciliation, and request-deadline 504 even
after a retried measured settlement. SQLite additionally exercises N=8 repeated
reservation/settlement, four mock benchmark conditions with completed unknown
responses, reconstruction of pending benchmark content after restart, and
empty-output failure followed by next-model fallback without a same-model retry.

Historical read-only checks found the run ID
5dbea74b-8934-466b-bfb7-7a133f90293c, 200 records (68 HTTP 200, 131 HTTP 503,
one HTTP 504), and 56 attempts totaling USD 0.0428793. The four failed Haiku
attempts, their exact identifiers, measured USD 0.0010434 spend and subsequent
routing are tabulated in DECISIONS.md. None was reclassified as successful.

PLAN.md SHA-256 still matches the original integrity record:
3de35537540c0b4cd0b77313a07d87018ff0cbe66b20d6dc27c448ef80d6fd76.
The protected calibration artifact's read-only SHA-256 at verification is
a53f9fffc31658a314b195fbd4461ec9798375c31199da4434c527c6e0cbab04.
The new journal's Compose volume wiring is supplied; container recreation and
power-loss behavior were not exercised. SQLite FULL commits and reopen/recovery
were tested locally. If local disk and Postgres fail together, the response
explicitly reports recovery_persisted=false and content remains in the returned
answer, but durable local recovery cannot be guaranteed.

Final read-only git diff --check: exit 0 (no whitespace/conflict errors).
