# Changelog

All notable changes to slopguard-python are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

## [Unreleased]

## [0.2.0] — 2026-09-30

### Added

- A `mutate` command for mutation testing. It writes one small change (a
  mutant) into a source file, runs the project's tests, and restores the file.
  A mutant that makes a test fail is killed. A mutant that every test passes
  with survived, and the report lists it with its file, line, column (in code
  points), original and replacement text, and enclosing method.
- `mutate` takes `--path`, `--include`, `--exclude`, `--no-default-excludes`,
  `--operators`, `--project-dir`, `--runner`, `--no-coverage`, `--timeout`,
  `--dry-run`, `--json`, `--fail-under`, `--verbose` and `--quiet`.
  `--dry-run` lists the mutants and runs no tests. `--fail-under` exits with
  code 2 when the mutation score is below it.
- Each mutant ends as `killed`, `survived`, `timeout`, `no_coverage`,
  `compile_error`, `ignored` or `pending`. The mutation score is
  (killed + timeout) / (killed + timeout + survived + no_coverage) × 100.
- `mutate` shares its core flags, operator ids, statuses, JSON shape
  (`reportType: "mutation"`, `schemaVersion: "1"`) and error codes with the
  other slopguard ports. The runner flags differ per port. This port's runner
  flag is `--runner`.
- The operators for Python are `arithmetic` (with `//`→`*` and `//=`→`*=`),
  `boolean_literal`, `boundary`, `invert_negative`, `logical` (`and`↔`or`),
  `negate_conditional` (with `is`↔`is not` and `in`↔`not in`), `remove_call`
  (a call statement becomes `pass`) and `remove_not` (`not x` → `x`).
  `increment` is accepted and produces no mutants in Python.
- `mutate` finds operator tokens with `tokenize` between the `ast` operand
  positions. It never mutates type annotations, `type` aliases, f-string
  internals, comments or strings. Where removing a token would join two names,
  as in `return-x`, one space stays.
- Each mutant is compiled in memory before it is written. A mutant that does
  not compile gets `compile_error` and is never written or run.
- Before any mutant, `mutate` runs the exact mutant test command once without
  changes. It must pass, or the run stops with `baseline_failed`. Its duration
  sets the per-mutant timeout, unless `--timeout` is given: 3 × the baseline
  time, rounded up to whole seconds, plus 10 s.
- The `analyze` coverage run then marks mutants on lines that no test executes
  as `no_coverage`. `--no-coverage` skips it. The coverage run never stops the
  run: without usable data a note says so and every mutant runs.
- Mutant runs use `python -m pytest -x -q -p no:cacheprovider` (plus
  `--no-cov` when pytest-cov is installed) or `python -m unittest discover -f`.
  Each run gets a new process group, and a timeout kills the whole group.
- A workspace guard allows one run per project (`mutation_in_progress`). It
  keeps a journal and a backup under `<temp dir>/slopguard-mutate/`, restores
  the file on errors and on SIGINT, SIGTERM and SIGHUP (exit 130, 143 or 129),
  and recovers a file that a killed run left mutated.
- Before each mutant write, `mutate` checks that the file still holds the
  bytes it planned the mutants from. The check catches an edit to a file that
  `mutate` has not reached yet, or one saved between two mutant runs. The file
  keeps that edit, its remaining mutants stay `pending`, and a note names the
  file. An edit saved while the file holds a mutant is overwritten when
  `mutate` restores the original. Do not edit files under `--path` during a
  run.
- Mutant runs set `PYTHONDONTWRITEBYTECODE=1`, and each mutant carries the
  original mtime + 1 s. No stale `.pyc` is loaded, and your `.pyc` files stay
  valid. PEP 263 encodings, a BOM and line endings are preserved.
- The `slopguard-ignore-mutant` marker, with an optional operator list,
  switches off equivalent mutants.
- Three error codes are new: `baseline_failed`, `mutation_in_progress` and
  `restore_failed`.
- `slopguard.mutation` provides `MutateOptions` and `run` for library use.
  The `slopguard` package also exports `MutateOptions`, `OPERATOR_IDS`,
  `generate_mutants`, `mutation_json_report` and `mutation_pretty_report`.
- `make mutate-baseline` and a CI step pin the sample app's mutation baseline
  at 12 mutants, 12 killed and 0 survived. The sample app gained one test,
  `test_ids_count_up_from_one`, which kills the `+=` → `-=` mutant in
  `TodoStore.add`.

### Changed

- `analyze` and `mutate` share the file walk (`diranalyzer.list_files`), the
  generated-file check (`fileanalyzer.is_generated`), the `generatedAt`
  format (`aggregator.format_generated_at`), the coverage runner and the
  coverage index loader (`coverage.pipeline.load_index`).
- `coverage.runner.TestOutcome` also carries the exit code and output tail.
  `run_tests(..., raise_on_failure=False)` returns instead of raising.
- `CoverageIndex.method_coverage(..., exact=True)` matches a file's real path
  only, with no basename fallback.
- An interrupted coverage run (Ctrl-C, or a signal that stops `mutate`) now
  kills the test process instead of leaving it running.
- The package keywords include `mutation-testing`.

### Posture

- `analyze` still never writes to your sources. `mutate` does, one file at a
  time, and restores every file.
- `SECURITY.md` describes what `mutate` writes, where it keeps its backup and
  journal, and which test commands it runs.

## [0.1.0] — 2026-06-28

Initial alpha release. The Python sibling of slopguard-go, slopguard-kotlin,
slopguard-swift and slopguard-typescript — same wCRAP formula, same schema-2
JSON, same CLI UX.

### Added

- **wCRAP analyzer** over the standard-library `ast`: cyclomatic complexity
  (McCabe, comparable to `mccabe`) and cognitive complexity (SonarSource 2023
  spec) computed in a single pass, blended as `sqrt(cyc × cog)`.
- **Lexical type aggregation** — methods attach to their enclosing `class` by
  lexical nesting and roll up per type (matching the Swift/Kotlin/TypeScript
  ports; the Go port differs with receiver-based aggregation).
- **Coverage pipeline** that auto-finds the project's tests and drives them
  under coverage.py (`pytest` when detected, else stdlib `unittest`), with
  `auto`, `--coverage-file` (prebuilt `coverage json`), and `--no-coverage`
  modes.
- **CLI**: `analyze` (default) and `version`, with `--path`, `--threshold`,
  `--project-dir`, `--runner`, `--include`/`--exclude`,
  `--no-default-excludes`, `--json`, `--fail-over`, `--verbose`, `--quiet`.
- Stable, versioned JSON report (`schemaVersion: "2"`) and a human-readable
  text report ranking the top methods by wCRAP.
- Method kinds for Python: `function`, `method`, `constructor` (`__init__`),
  `getter` (`@property`) and `setter` (`@x.setter`). Anonymous `lambda`s fold
  into the enclosing method; named nested `def`s get their own entry.
- Default excludes for `.venv/`, caches, test files/dirs, generated stubs
  (`*_pb2.py`) and files carrying an `@generated` / `DO NOT EDIT` header.
- `sample-apps/todolist` reference fixture used as a CI regression baseline
  (12 methods, 0 crappy, 100% coverage).

### Posture

- Zero runtime dependencies (Python standard library only).
- No network, no telemetry, no source mutation.

[Unreleased]: https://github.com/JeevanThandi/slopguard-python/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/JeevanThandi/slopguard-python/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/JeevanThandi/slopguard-python/releases/tag/v0.1.0
