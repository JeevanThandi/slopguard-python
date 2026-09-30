# CLAUDE.md — slopguard-python

Guidance for Claude when working in this repo. Read this first.

## What this is

`slopguard-python` is the **Python port** of slopguard — a CRAP (Change Risk
Anti-Patterns) guardrail. It scores every function/method by **complexity ×
lack-of-coverage** (wCRAP) and emits a text or JSON report you can gate CI on.
Its `mutate` command is a mutation tester: it writes one small change at a
time into the sources, runs the tests, restores the file, and reports the
changes no test caught.

**Parity mandate:** `slopguard-python` is one of several sibling ports that must
stay **behaviourally aligned**. The wCRAP formula, the schema-2 JSON shape, the
CLI UX (flags, exit codes, stderr/stdout split), and the error-envelope shape
are a shared contract — don't change them unilaterally here, or you break
cross-tool consumers and drift from the siblings. The same holds for
`mutate`: its flags, operator ids, statuses, JSON shape (`reportType:
"mutation"`, `schemaVersion: "1"`), notes, exit codes and error codes are a
shared contract with every port, and the TypeScript port
(`slopguard-typescript/src/mutation/`) is its reference implementation:

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
make mutate-baseline  # assert sample-apps/todolist kills all 12 mutants
make compile     # byte-compile syntax gate
```

Run it against itself:

```bash
PYTHONPATH=src python -m slopguard analyze --path src/slopguard            # full, with coverage
PYTHONPATH=src python -m slopguard analyze --path . --no-coverage          # fast, complexity-only
PYTHONPATH=src python -m slopguard analyze --path . --json | jq '.methods | sort_by(-.crap)[:10]'
PYTHONPATH=src python -m slopguard mutate --path src/slopguard/crap.py     # mutation-test one module
```

Mutating the tool's own module runs the whole test suite once per mutant
(about 17 s each), so keep such runs to one small module.

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
- **`src/slopguard/mutation/`** — `mutate`. `source.py` (bytes → decoded text
  per PEP 263, line table, UTF-8 byte column → code-point column, splice +
  re-encode), `operators.py` (operator ids, `parse_operators`, the generator:
  `ast` for nodes, `tokenize` for operator positions), `ignore.py`
  (`slopguard-ignore-mutant` markers), `planner.py` (pure: generate → filter →
  sort → enclosing method → ignore), `models.py` (statuses, `MutantSite`, ids,
  score, summary), `runner.py` (mutant command, process-group spawn with
  timeout, compile check, classification), `guard.py` (`WorkspaceGuard`: lock,
  journal, backup, in-place write/restore, recovery), `pipeline.py`
  (orchestrator: plan → guard → plain baseline → coverage baseline → one run
  per mutant → report; signal handling). Report formatting lives in
  `formatting.py` (`mutation_pretty_report`, `mutation_json_report`).
- **`tests/`** — stdlib `unittest` (zero test deps). `test_complexity.py` pins
  the analyzer contract.
- **`sample-apps/todolist/`** — a self-contained fixture (its own
  `pyproject.toml`) used as a CI regression baseline (12 methods, 0 crappy,
  100% coverage; 12 mutants, all killed). `**/sample-apps/**` is excluded from
  scans. Tests that run `mutate` end to end use a temporary copy of it, never
  the checked-in directory.

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
- **`mutate` never leaves a mutated file behind.** Every mutant goes through
  `WorkspaceGuard.with_mutant`: journal (atomic) before the write, truncate +
  write in place (inode, mode and hard links survive), restore bytes and
  atime/mtime in a `finally`. The guard directory is
  `<tempfile.gettempdir()>/slopguard-mutate/<sha256(realpath(project))[:16]>/`
  (`lock`, `journal.json`, `original`) — never inside the project, and shared
  with the other ports. A stale lock (dead pid) triggers recovery, which
  restores a file only when it still holds exactly the journaled mutant.
- **Changed-file guard.** Immediately before every mutant write,
  `WorkspaceGuard.with_mutant` compares the file's current bytes with the
  bytes the mutants were planned from (`_begin` does it for the file's first
  mutant). On a mismatch it writes nothing and raises `SourceChanged`; the
  pipeline gives that mutant and every remaining mutant of the file the
  status `pending` (ignored ones stay `ignored`) and adds the note
  `<absolute path> changed while mutate was running, so its remaining
  mutants were not run.` Without the check, the mutant (spliced into the
  stale text) and the restore would both overwrite an edit saved before the
  write. The check cannot protect an edit saved while a mutant is in place:
  `restore()` writes the original back without comparing (as the shared
  contract and the TypeScript guard do), so that edit is lost and no note is
  added. The docs say so and tell users not to edit files under `--path`
  during a run; don't document a stronger guarantee than that.
- **Signals.** During a run, SIGINT/SIGTERM/SIGHUP handlers (main thread only)
  kill the running test process group, restore, release the guard, print
  `interrupted — restored <file>` and raise `MutationInterrupted`; the CLI
  returns 130/143/129. The output reader thread blocks those signals, so they
  always wake the main thread.
- **No stale bytecode.** Mutant runs set `PYTHONDONTWRITEBYTECODE=1`; each
  mutant is written with the original mtime + 1 s, and the restore puts the
  original mtime back.
- **`compile_error` is decided in-process.** `runner.compiles` runs
  `compile(text, filename, "exec", dont_inherit=True)` on the mutated text
  (warnings silenced) before the guard is asked to write anything. A mutant
  that fails is `compile_error`: never written, never run, excluded from the
  score.
- **Columns are code points.** `ast` `col_offset` is a UTF-8 byte offset;
  `SourceFile.column_of_byte` converts it. `tokenize` columns are already code
  points. `id` = `<file>:<line>:<column>:<operator>`.
- **Token-join guard.** `_Generator._emit_removal` (used by `invert_negative`
  and `remove_not`) replaces the removed token with one space instead of empty
  text when the removal would join two identifier characters: `return-x`
  becomes `return x`, never the name `returnx`.
- **Same mutants on every Python version.** Nothing inside f-strings is
  mutated (their tokens differ before 3.12), nor type annotations or `type`
  aliases. `tests/test_mutation_operators.py` pins the operator behaviour.
- **Two baselines.** The plain baseline (the exact mutant command, unmutated,
  no timeout) decides pass/fail (`baseline_failed`) and sets the timeout
  (`ceil(3 × seconds) + 10`); the coverage baseline only supplies
  `no_coverage` and never fails the run. Nothing to run (no mutants, or all
  ignored) skips both.

## Conventions

- Errors are `slopguard.errors.SlopguardError` with a stable `code`; surface
  them via `errors.envelope_for`. Exit codes: 0 ok, 1 error, 2 `--fail-over`
  (`analyze`) or `--fail-under` (`mutate`), 130/143/129 when a signal stops
  `mutate`.
- **Test coverage floor is 95%** (CI gate via `coverage report --fail-under`).
  Real subprocess integration lives in `tests/test_runner_integration.py` and
  is skipped when coverage.py isn't importable. The `mutate` subprocess tests
  (`tests/test_mutation_runner.py`, `tests/test_mutation_cli.py`) use
  `--no-coverage` or plain commands, so no `coverage run` nests inside a
  measured test run; keep their timing margins generous.
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
make mutate-baseline PY=python
# expect "mutate baseline ok: 12 mutants, 12 killed, 0 survived"
PYTHONPATH=src /usr/bin/python3 -m unittest discover -s tests   # 3.9 compatibility
```
