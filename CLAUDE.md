# CLAUDE.md — slopguard-python

Guidance for Claude when working in this repo. Read this first.

## What this is

`slopguard-python` is the **Python port** of slopguard — a CRAP (Change Risk
Anti-Patterns) guardrail. It scores every function/method by **complexity ×
lack-of-coverage** (wCRAP) and emits a text or JSON report you can gate CI on.

**Parity mandate:** `slopguard-python` is one of several sibling ports that must
stay **behaviourally aligned**. The wCRAP formula, the schema-2 JSON shape, the
CLI UX (flags, exit codes, stderr/stdout split), and the error-envelope shape
are a shared contract — don't change them unilaterally here, or you break
cross-tool consumers and drift from the siblings:

- Go (the canonical reference): https://github.com/JeevanThandi/slopguard-go
- TypeScript (the reference for intent): https://github.com/JeevanThandi/slopguard-typescript
- Swift: https://github.com/JeevanThandi/SlopGuard-Swift
- Kotlin: https://github.com/JeevanThandi/slopguard-kotlin

## Environment

System `python3` here is **3.9** and has no `coverage`/`pytest`. The tool itself
needs neither — it's stdlib-only. To run the test suite and the coverage gate,
use a venv with coverage.py installed:

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install coverage
```

`ast.Match` is 3.10+, so the `match`-statement analysis is guarded and its test
is skipped on 3.9. Everything else runs on 3.9+.

## Build / test / run

A `Makefile` wraps the common tasks. The dev convention is `PYTHONPATH=src`
(there is no compiled artifact; the package lives under `src/slopguard`):

```bash
make test        # PYTHONPATH=src python -m unittest discover -s tests
make coverage    # tests under coverage.py + enforce the 95% floor (needs coverage)
make dogfood     # analyze own src, complexity-only, --fail-over 300
make baseline    # assert sample-apps/todolist reports 12 methods / 0 crappy
make compile     # byte-compile syntax gate
```

Run it against itself:

```bash
PYTHONPATH=src python -m slopguard analyze --path src/slopguard            # full, with coverage
PYTHONPATH=src python -m slopguard analyze --path . --no-coverage          # fast, complexity-only
PYTHONPATH=src python -m slopguard analyze --path . --json | jq '.methods | sort_by(-.crap)[:10]'
```

## Architecture

A `core`-style analysis surface, a coverage subsystem, and a thin CLI — **no
third-party runtime dependencies** (`ast`, `argparse`, `json` only).

- **`src/slopguard/`** — pure analysis, no subprocesses. `crap.py` (the wCRAP
  formula), `complexity.py` (the single-pass `ast` analyzer), `models.py`
  (dataclasses + id helpers), `aggregator.py` (joins coverage, builds the
  report dict), `glob.py` / `diranalyzer.py` (excludes + enumeration),
  `formatting.py` (text + JSON), `fileanalyzer.py`, `errors.py`, `progress.py`,
  `version.py`, `cli.py` (`run(argv, stdout, stderr) -> int`), `__main__.py`.
- **`src/slopguard/coverage/`** — drives the project's tests, parses the report,
  joins coverage. `detection.py` (project root + pytest/unittest detection),
  `runner.py` (spawns `python -m coverage run -m pytest|unittest`), `report.py`
  (parse `coverage json`), `index.py` (per-line lookup + path resolution),
  `pipeline.py` (orchestrator with auto / prebuilt / none modes).
- **`tests/`** — stdlib `unittest` (zero test deps). `test_complexity.py` pins
  the analyzer contract.
- **`sample-apps/todolist/`** — a self-contained fixture (its own
  `pyproject.toml`) used as a CI regression baseline (12 methods, 0 crappy,
  100% coverage). `**/sample-apps/**` is excluded from scans.

## Key invariants — don't break these

- **wCRAP formula** (`crap.py`): `crap_score(comp, cov) = comp²(1−cov/100)³ +
  comp`, fed `comp = sqrt(cyclomatic × cognitive)`. Default threshold 30.
- **Cyclomatic** counting matches `mccabe` (if/elif/for/while/except/ternary/
  bool-op/comprehension-clause/match-case, base 1). **Cognitive** follows the
  SonarSource 2023 spec (whole `match` = one increment, nesting-amplified,
  boolean-run collapse — Python groups like ops into one `BoolOp` node so each
  node is one run, early exits free). `tests/test_complexity.py` pins exact
  numbers — if you touch the analyzer, those tests are the contract, and the
  numbers must stay equal to the siblings'.
- **Python-specific: lexical type aggregation.** Unlike the Go port
  (receiver-based), methods attach to their enclosing `class` by lexical
  nesting, keyed by `(file, qualified class name)` in `aggregator.py`. Named
  nested `def`s get their own entry; `lambda`s fold in with a nesting bump.
- **JSON is alphabetically sorted** (`json.dumps(sort_keys=True)`) and whole
  floats render without a trailing `.0` (`formatting._normalize`) — diff-stable
  output byte-aligned with the siblings. Keep that.
- **`typeName` is `null`** for free functions, the qualified class name for
  methods. `generatedAt` uses `%Y-%m-%dT%H:%M:%S.%fZ` truncated to ms (UTC).
- **Generated files are skipped** (`@generated` / `DO NOT EDIT` header) in
  `fileanalyzer.py`, on top of the glob excludes in `diranalyzer.py`.
- **Coverage is an artifact, never an input.** `auto` mode runs the project's
  own tests; failing tests don't abort (a note is attached), but a non-zero exit
  with no usable coverage is `test_run_failed`.

## Conventions

- Errors are `slopguard.errors.SlopguardError` with a stable `code`; surface
  them via `errors.envelope_for`. Exit codes: 0 ok, 1 error, 2 `--fail-over`.
- **Test coverage floor is 95%** (CI gate via `coverage report --fail-under`).
  Real subprocess integration lives in `tests/test_runner_integration.py` and
  is skipped when coverage.py isn't importable.
- Keep it stdlib-only. No `requests`, no `click`, no `toml` parser — heuristic
  text scans are deliberate so the tool stays dependency-free.

## When verifying a change

```bash
PYTHONPATH=src python -m compileall -q src tests          # syntax
PYTHONPATH=src python -m coverage run --source=src -m unittest discover -s tests
python -m coverage report --fail-under=95
PYTHONPATH=src python -m slopguard analyze \
  --path sample-apps/todolist/todolist --project-dir sample-apps/todolist \
  --json --quiet | jq '{methods:.summary.methodCount, crappy:.summary.crappyMethodCount}'
# expect {"methods":12,"crappy":0} — the regression baseline
```
