# slopguard-python

[![CI](https://github.com/JeevanThandi/slopguard-python/actions/workflows/ci.yml/badge.svg)](https://github.com/JeevanThandi/slopguard-python/actions/workflows/ci.yml)

> **CRAP (Change Risk Anti-Patterns) guardrail for Python.**

> ⚠️ **Alpha (v0.1.x).** The analyzer is stable and self-tested, but the CLI surface and JSON schema may still change before v1.0.

`slopguard-python` measures **complex, undertested code** in Python projects. It computes a weighted CRAP score combining cyclomatic and cognitive complexity with line coverage, and prints a structured report you can pipe into `jq` or fail CI on. It is the Python sibling of [slopguard-go](https://github.com/JeevanThandi/slopguard-go), [slopguard-swift](https://github.com/JeevanThandi/SlopGuard-Swift), [slopguard-typescript](https://github.com/JeevanThandi/slopguard-typescript) and [slopguard-kotlin](https://github.com/JeevanThandi/slopguard-kotlin) — same formula, same schema, same UX.

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
```

Progress markers (`slopguard: running pytest under coverage…`) go to **stderr**, so piped stdout stays clean. `--verbose` streams the underlying test-runner output through; `--quiet` silences progress entirely.

You can also run it as a module: `python -m slopguard analyze --path ./src`.

## How coverage works

Coverage is an *artifact of the analysis*, not an input — mirroring how slopguard-swift drives `xcodebuild test` and slopguard-typescript drives vitest/jest:

1. **Project discovery.** Walk up from `--path` to the nearest project root (`pyproject.toml` / `setup.py` / `setup.cfg` / `.git`). Override with `--project-dir`.
2. **Runner detection.** Auto-find the tests: prefer `pytest` when the project shows pytest signals (a `pytest.ini`/`conftest.py`, a `[tool.pytest.ini_options]` / `[tool:pytest]` block, or `pytest` in the deps), otherwise fall back to stdlib `unittest`. Override with `--runner`.
3. **Test run.** Drive the suite under coverage.py — `python -m coverage run --source=<root> -m pytest` (or `-m unittest discover`) — into a slopguard-owned temp directory, then `coverage json`. Failing tests don't abort (partial coverage is still useful — a note is attached); a run that produces no usable coverage with a non-zero exit aborts with the output tail.
4. **Join.** Parse the `coverage json` report into a per-line index, resolve its file paths to disk (basename + longest-suffix fallback for CI-vs-local path mismatches), join per-method line coverage onto the parsed declarations, then delete the temp dir.

A `coverage json` report is the universal Python interchange format — anything you can run under `coverage` produces one — so a report your CI already generated is supported via `--coverage-file`.

slopguard-python itself has **zero runtime dependencies**; coverage.py lives in your project's environment (like `pytest`), not here.

## Subcommands

| Command   | Purpose |
|-----------|---------|
| `analyze` | Walk a directory of Python sources, drive the test suite for coverage, emit a wCRAP report (text or JSON). |
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

## Why it exists

Test coverage alone says "this code ran in a test"; complexity alone says "this code has many paths." Neither tells you whether the *risky* code is tested. CRAP combines them: a method with 20 branches and 0% coverage scores 420; the same method at 100% coverage scores 20 (just its complexity). The score lights up the code most likely to break under a refactor *and* be the hardest to verify the fix for — exactly the code your coding agents trip over.

## What counts as a method

Top-level **functions**, **methods**, **constructors** (`__init__`), and property **getters**/**setters**. Named nested `def`s get their own entry; anonymous `lambda`s don't — their branches count toward the enclosing method, with a cognitive nesting bump for the lambda body, per the Sonar spec.

Methods attach to their **lexically enclosing `class`** (like the Swift/Kotlin/TypeScript ports), so nested classes get qualified names: `Outer.Inner.method`.

Default excludes keep noise out: `.venv/`, caches (`__pycache__`, `.mypy_cache`, …), test files and dirs (`test_*.py`, `*_test.py`, `tests/`, `conftest.py`), generated stubs (`*_pb2.py`) and anything carrying an `@generated` / `DO NOT EDIT` header. Analyze excluded code with `--no-default-excludes`.

## Posture

* **Zero runtime dependencies** — Python standard library only (`ast`, `argparse`, `json`).
* **The only subprocess slopguard-python spawns is your project's own test suite under coverage.py.**
* **No network, no telemetry, no source mutation.** See [`SECURITY.md`](SECURITY.md) for the full threat model.
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
