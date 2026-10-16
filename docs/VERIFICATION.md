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
