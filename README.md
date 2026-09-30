# slopguard-python

[![CI](https://github.com/JeevanThandi/slopguard-python/actions/workflows/ci.yml/badge.svg)](https://github.com/JeevanThandi/slopguard-python/actions/workflows/ci.yml)

> **CRAP (Change Risk Anti-Patterns) guardrail for Python.**

> ⚠️ **Alpha (v0.2.x).** The analyzer is stable and self-tested, but the CLI surface and JSON schema may still change before v1.0.

`slopguard-python` measures **complex, undertested code** in Python projects. It computes a weighted CRAP score combining cyclomatic and cognitive complexity with line coverage, and prints a structured report you can pipe into `jq` or fail CI on. Its `mutate` command checks that the tests catch changes to the code (see [Mutation testing](#mutation-testing)). It is the Python sibling of [slopguard-go](https://github.com/JeevanThandi/slopguard-go), [slopguard-swift](https://github.com/JeevanThandi/SlopGuard-Swift), [slopguard-typescript](https://github.com/JeevanThandi/slopguard-typescript) and [slopguard-kotlin](https://github.com/JeevanThandi/slopguard-kotlin) — same formula, same schema, same UX.

```
wCRAP(m) = (cyc × cog) × (1 − cov/100)³ + sqrt(cyc × cog)
```

* `cyc` — cyclomatic complexity (McCabe), parsed via the standard-library [`ast`](https://docs.python.org/3/library/ast.html). Counts `if`/`elif`, `for`/`while`, each `except`, the ternary, each `and`/`or`, comprehension `for`/`if` clauses, and `match` cases — comparable to `mccabe`.
* `cog` — cognitive complexity per the [SonarSource 2023 spec](https://www.sonarsource.com/resources/cognitive-complexity/) — penalises nesting, charges a whole `match` once, ignores early-exit shapes (plain `return`/`break`/`continue`).
* `wt`  — `sqrt(cyc × cog)`, the geometric blend fed into the formula. A flat 50-branch dispatch scores like a small method; a deeply nested 3-branch tangle scores like medium-complex code.
* `cov` — line coverage gathered by slopguard-python itself, by driving the project's own test suite (pytest or unittest, under coverage.py). Never user-supplied.
* Default crappy threshold: **30** (on wCRAP).

## Install

```bash
pip install slopguard-python
```

…or from source:

```bash
git clone https://github.com/JeevanThandi/slopguard-python.git
cd slopguard-python
pip install .
```

Requires Python 3.9+. Run it from your project's environment (the same one your
tests run in) so coverage gathering can import your package and its tests.

## Quickstart

```bash
# Zero-config: analyze the current project (auto-finds and runs its tests for coverage)
slopguard-python

# Scan a specific directory and print the top crappy methods
slopguard-python analyze --path ./src --threshold 30

# Name the test runner instead of auto-detecting it
slopguard-python analyze --path ./src --runner pytest

# Full JSON for CI / downstream tooling
slopguard-python analyze --path . --json | jq '.methods | sort_by(-.crap)[:10]'

# Fail CI when any method's CRAP exceeds 50
slopguard-python analyze --path . --fail-over 50

# Complexity only (skip the test run — every method shows 0% coverage)
slopguard-python analyze --path ./src --no-coverage

# Join coverage CI already produced (a `coverage json` report)
slopguard-python analyze --path . --coverage-file coverage.json

# Mutation testing: change the code, run the tests, list the changes no test caught
slopguard-python mutate --path src/pkg/store.py
```

Progress markers (`slopguard: running pytest under coverage…`) go to **stderr**, so piped stdout stays clean. `--verbose` streams the underlying test-runner output through; `--quiet` silences progress entirely.

You can also run it as a module: `python -m slopguard analyze --path ./src`.

## How coverage works

Coverage is an *artifact of the analysis*, not an input — mirroring how slopguard-swift drives `xcodebuild test` and slopguard-typescript drives vitest/jest:

1. **Project discovery.** Walk up from `--path` to the nearest project root (`pyproject.toml` / `setup.py` / `setup.cfg` / `tox.ini` / `.git`). Override with `--project-dir`.
2. **Runner detection.** Auto-find the tests: prefer `pytest` when the project shows pytest signals (a `pytest.ini`/`conftest.py`, a `[tool.pytest.ini_options]` / `[tool:pytest]` block, or `pytest` in the deps), otherwise fall back to stdlib `unittest`. Override with `--runner`.
3. **Test run.** Drive the suite under coverage.py — `python -m coverage run --source=<root> -m pytest` (or `-m unittest discover`) — into a slopguard-owned temp directory, then `coverage json`. Failing tests don't abort (partial coverage is still useful — a note is attached); a run that produces no usable coverage with a non-zero exit aborts with the output tail.
4. **Join.** Parse the `coverage json` report into a per-line index, resolve its file paths to disk (basename + longest-suffix fallback for CI-vs-local path mismatches), join per-method line coverage onto the parsed declarations, then delete the temp dir.

A `coverage json` report is the universal Python interchange format — anything you can run under `coverage` produces one — so a report your CI already generated is supported via `--coverage-file`.

slopguard-python itself has **zero runtime dependencies**; coverage.py lives in your project's environment (like `pytest`), not here.

## Subcommands

| Command   | Purpose |
|-----------|---------|
| `analyze` | Walk a directory of Python sources, drive the test suite for coverage, emit a wCRAP report (text or JSON). |
| `mutate`  | Apply one small change at a time to the sources, run the tests against each, report the changes no test caught (text or JSON). |
| `version` | Print version metadata as JSON. |

`analyze` is the default subcommand and `--path` defaults to the current directory — a bare `slopguard-python` in your project root just works.

### `analyze` flags

| Flag | Default | Meaning |
|------|---------|---------|
| `-p, --path` | `.` | Directory of Python sources, or a single `.py` file. |
| `-t, --threshold` | `30` | wCRAP threshold above which a method/type is crappy. |
| `--project-dir` | auto | Project root the test run executes in. |
| `--runner` | auto | `pytest` or `unittest`. |
| `--no-coverage` | off | Skip the test run; report complexity only (0% coverage). |
| `--coverage-file` | — | Prebuilt `coverage json` report to join instead of running tests. |
| `--include` | — | Glob of files to include (repeatable). |
| `--exclude` | — | Extra glob to exclude, combined with defaults (repeatable). |
| `--no-default-excludes` | off | Skip built-in excludes. |
| `--json` | off | Emit JSON to stdout (default is pretty text). |
| `--fail-over` | — | Exit `2` if any method's CRAP exceeds this value. |
| `-v, --verbose` | off | Stream test-runner output to stderr. |
| `--quiet` | off | Suppress all progress chatter. |

Exit codes: **0** success, **1** error, **2** `--fail-over` exceeded.

## JSON output

`--json` emits a stable, versioned (`schemaVersion: "2"`, shared with every slopguard port) report with:

* `summary` — file/type/method counts, average + max wCRAP, weighted coverage.
* `methods[]` — every analyzed function/method with `complexity`, `cognitiveComplexity`, `weightedComplexity`, `coverage`, `crap`, `isCrappy`, and a stable `id`.
* `types[]` — per-class aggregation: `aggregatedCrap` (formula applied to type totals) and `maxCrap` (worst single-method offender).

Slice with `jq`:

```bash
# Top 10 worst methods
slopguard-python analyze --path . --json | jq '.methods | sort_by(-.crap)[:10]'

# Only crappy types
slopguard-python analyze --path . --json | jq '.types[] | select(.isCrappy)'

# Coverage gaps: high complexity, low coverage
slopguard-python analyze --path . --json \
  | jq '.methods[] | select(.complexity >= 5 and .coverage <= 50)'
```

### Build an agent work queue

```bash
slopguard-python analyze --json --quiet \
  | jq '[.methods[] | select(.isCrappy)] | sort_by(-.crap)
         | map({id, crap, coverage, file, line})'
```

Drop this into `CLAUDE.md` / `AGENTS.md` so your agent gates on slop and refactors the worst offenders first:

> Use `slopguard-python` to analyze this repo and find the method with the highest wCRAP score. Show me its file and line, then add tests or refactor until its score is under 30.

## Mutation testing

Coverage says a line ran during a test. It does not say a test would fail if the line were wrong. `slopguard-python mutate` checks that.

It makes one small change to the source (a *mutant*), runs the tests, and puts the original back. It does this for every mutant it can generate. A mutant that makes a test fail is **killed**. A mutant that every test passes with has **survived**: no test checks that behaviour. The report lists each survivor with its file, line, column, original text, replacement text and enclosing method. You or an agent can then write the test that kills it.

Robert C. Martin describes this workflow in the video on [slopguard.dev](https://slopguard.dev). Have the AI cover the code, run a mutation tester, and have the AI write a failing test for every mutant that survives. `analyze` is the other half of that workflow.

```bash
# One file
slopguard-python mutate --path src/pkg/store.py

# A directory, narrowed with globs (relative to --path, same rules as analyze)
slopguard-python mutate --path src --include "pkg/core/**" --exclude "**/legacy/**"

# Only some operators
slopguard-python mutate --path src --operators boundary,negate_conditional

# List the mutants without running any tests
slopguard-python mutate --path src --dry-run

# Survivors as JSON, for an agent
slopguard-python mutate --path src --json | jq '.mutants[] | select(.status == "survived")'

# Fail CI below an 80% mutation score
slopguard-python mutate --path src --fail-under 80
```

### How a run works

1. Walk `--path` with the same include/exclude rules as `analyze` (test files are skipped by default) and generate the mutants.
2. Run the test suite once, unmutated, with the exact command used for mutants. That command is `python -m pytest -x -q -p no:cacheprovider` (plus `--no-cov` when pytest-cov is installed) or `python -m unittest discover -f`. It must pass, or the run stops with `baseline_failed`. Its duration sets the per-mutant timeout, unless `--timeout` is given: 3 × the baseline time, rounded up to whole seconds, plus 10 seconds.
3. Run the suite once under coverage.py, exactly as `analyze` does. A mutant on a line that no test executes gets `no_coverage` and is not run. This run never stops `mutate`: without usable coverage data (coverage.py missing, no report) a note says so and every mutant runs. `--no-coverage` skips this step and runs every mutant.
4. For each remaining mutant, in file and line order, compile the mutated source in memory, write the mutant into the file, run the tests and restore the file. A mutant that does not compile gets `compile_error` and is never written. A run that passes the timeout is killed with its whole process group. Before each write, `mutate` checks that the file still holds the bytes it planned the mutants from. If it does not, the file's remaining mutants are not run and stay `pending` (see [Safety](#safety)).

When nothing is left to run (no mutants, or every mutant is ignored), steps 2 to 4 are skipped.

### Flags

| Flag | Meaning |
|------|---------|
| `-p, --path` | Directory or single `.py` file to mutate. Default: `.` |
| `--include` / `--exclude` | Narrow the files, exactly as for `analyze` (repeatable). `--no-default-excludes` drops the built-in excludes. |
| `--operators` | Operators to apply, comma-separated or repeated. Default: all. |
| `--runner`, `--project-dir` | Which runner to drive (`pytest` or `unittest`) and where, exactly as for `analyze`. |
| `--no-coverage` | Skip the coverage run and test every mutant. |
| `--timeout` | Per-mutant timeout in seconds. Default: computed from the baseline run (step 2). |
| `--dry-run` | List the mutants. Run no tests and touch no files. |
| `--json` | Emit JSON to stdout. |
| `--fail-under` | Exit `2` when the mutation score is below this value. |
| `-v, --verbose` / `--quiet` | Stream the runner output / silence progress. |

Exit codes: `0` success, `1` error, `2` score below `--fail-under`, `130`/`143`/`129` interrupted by SIGINT/SIGTERM/SIGHUP (the source is restored first).

### Operators

| id | Change |
|----|--------|
| `arithmetic` | `+`↔`-`, `*`→`/`, `/`→`*`, `%`→`*`, `//`→`*`, and `+=`↔`-=`, `*=`↔`/=`, `%=`→`*=`, `//=`→`*=`. String concatenation (`"a" + b`, f-strings) is skipped. |
| `boolean_literal` | `True`↔`False` |
| `boundary` | `<`↔`<=`, `>`↔`>=` |
| `increment` | `++`↔`--`. Python has no such operator, so this id produces no mutants here. |
| `invert_negative` | `-x` → `x`. One space stays where the removal would join two names: `return-x` → `return x`. |
| `logical` | `and`↔`or` |
| `negate_conditional` | `==`↔`!=`, `<`→`>=`, `<=`→`>`, `>`→`<=`, `>=`→`<`, `is`↔`is not`, `in`↔`not in` |
| `remove_call` | `save(x)` → `pass` for a statement that is only a call (optionally awaited). `print(...)`, logging calls (`logging.*`, `logger.*`, `log.*`, also as an attribute such as `self.logger.info`), a bare `super(...)` call and `__init__` delegation such as `super().__init__(...)` are skipped. Other calls on `super()`, such as `super().save()`, are removed like any other call. |
| `remove_not` | `not x` → `x` |

Comments, strings, type annotations, `type` aliases and the inside of f-strings are never mutated. Every slopguard port uses the same operator ids.

### Statuses and score

| Status | Meaning | Counts as |
|--------|---------|-----------|
| `killed` | A test failed. | detected |
| `timeout` | The run passed the timeout, usually an infinite loop. | detected |
| `survived` | Every test passed. | undetected |
| `no_coverage` | No test executes the line. | undetected |
| `compile_error` | The mutated file does not compile. It is not run. | excluded |
| `ignored` | An ignore marker switched it off. | excluded |
| `pending` | Not run: listed by `--dry-run`, or its file changed during the run. | excluded |

`mutationScore = (killed + timeout) / (killed + timeout + survived + no_coverage) × 100`.

### Equivalent mutants

Some mutants change nothing observable, so no test can kill them. `if score > best: best = score` behaves the same with `>=`, because assigning an equal value changes nothing. Mark such a line with a comment on the line itself, or on a comment line directly above it:

```python
if score > best:  # slopguard-ignore-mutant(boundary): assigning an equal best changes nothing
    best = score
```

`slopguard-ignore-mutant` alone ignores every operator on the line. `slopguard-ignore-mutant(boundary,logical)` ignores only the listed operators.

### JSON output

`--json` emits a versioned report (`reportType: "mutation"`, `schemaVersion: "1"`) with sorted keys. The other ports emit the same shape. This excerpt comes from the sample app before its tests checked the ids:

```json
{
  "mutants": [
    {
      "column": 23,
      "file": "store.py",
      "id": "store.py:14:23:arithmetic",
      "line": 14,
      "method": "TodoStore.add",
      "operator": "arithmetic",
      "original": "+=",
      "replacement": "-=",
      "status": "survived"
    }
  ],
  "summary": {
    "compileErrors": 0, "fileCount": 4, "ignored": 0, "killed": 11, "mutantCount": 12,
    "mutationScore": 91.66666666666666, "noCoverage": 0, "pending": 0, "survived": 1, "timedOut": 0
  }
}
```

The full report also carries `coverageAvailable`, `generatedAt`, `notes`, `operators`, `projectRoot`, `runner`, `sourceRoot`, `timeoutSeconds`, `tool` and `toolVersion`.

### Safety

`mutate` writes to your source files, one file at a time, and always puts them back:

* The original bytes stay in memory and in a backup under the OS temp directory, and the original timestamps stay in memory. Both are written back after every test run, when an error occurs, and on SIGINT, SIGTERM or SIGHUP.
* A journal names the file that holds a mutant and the mutant's hash. If the process is killed without warning (for example by SIGKILL), the next `mutate` run for that project restores the file. It does so only when the file still holds exactly that mutant, so later edits are never overwritten. After a power loss, recovery works only if the OS temp directory survives the restart. If it does not, check the files under `--path` against version control.
* Only one `mutate` run per project can hold the lock. A second run stops with `mutation_in_progress`.
* Mutant runs set `PYTHONDONTWRITEBYTECODE=1`. Each mutant gets the original modification time plus one second, so Python never loads a cached `.pyc` of the original for it. The restore puts the original time back, so your `.pyc` files stay valid.
* A file is written with its own encoding (PEP 263), BOM and line endings. Only the mutated span changes.
* Do not edit files under `--path` while `mutate` runs. An edit saved while a file holds a mutant is overwritten when `mutate` restores the original. Before each mutant write, `mutate` checks that the file still holds the bytes it planned the mutants from. That check catches an edit to a file `mutate` has not reached yet, or one saved between two mutant runs. The file keeps that edit, its remaining mutants stay `pending`, and a note names the file.
* `--dry-run` writes nothing.

## Why it exists

Test coverage alone says "this code ran in a test"; complexity alone says "this code has many paths." Neither tells you whether the *risky* code is tested. CRAP combines them: a method with 20 branches and 0% coverage scores 420; the same method at 100% coverage scores 20 (just its complexity). The score lights up the code most likely to break under a refactor *and* be the hardest to verify the fix for — exactly the code your coding agents trip over.

## What counts as a method

Top-level **functions**, **methods**, **constructors** (`__init__`), and property **getters**/**setters**. Named nested `def`s get their own entry; anonymous `lambda`s don't — their branches count toward the enclosing method, with a cognitive nesting bump for the lambda body, per the Sonar spec.

Methods attach to their **lexically enclosing `class`** (like the Swift/Kotlin/TypeScript ports), so nested classes get qualified names: `Outer.Inner.method`.

Default excludes keep noise out: `.venv/`, caches (`__pycache__`, `.mypy_cache`, …), test files and dirs (`test_*.py`, `*_test.py`, `tests/`, `conftest.py`), generated stubs (`*_pb2.py`) and anything carrying an `@generated` / `DO NOT EDIT` header. Analyze excluded code with `--no-default-excludes`.

## Posture

* **Zero runtime dependencies** — Python standard library only (`ast`, `argparse`, `json`).
* slopguard-python spawns only the Python interpreter it runs under. Those subprocesses run your project's test suite (under coverage.py for coverage, and without coverage for `mutate`'s baseline and mutant runs), coverage.py's report step, and one-line checks that coverage.py and pytest-cov are installed.
* slopguard-python makes no network requests and collects no telemetry. `analyze` never writes to your sources. `mutate` does, one file at a time, and restores every file (see [Safety](#safety)). [`SECURITY.md`](SECURITY.md) has the full threat model.
* **MIT licensed** ([`LICENSE`](LICENSE)).

## Library use

Everything the CLI does is importable:

```python
from slopguard.coverage import run, CoverageSource
from slopguard import default_analysis_options, json_report

report = run("./src", CoverageSource(), threshold=30.0,
             options=default_analysis_options())
print(json_report(report))
```

For complexity-only analysis with no I/O, `slopguard.analyze_source(source, "file.py")` returns the per-file metrics directly.

Mutation testing works the same way:

```python
from slopguard.mutation import MutateOptions, run as run_mutate
from slopguard import mutation_json_report

report = run_mutate(MutateOptions(source_path="./src", dry_run=True))
print(mutation_json_report(report))
```
