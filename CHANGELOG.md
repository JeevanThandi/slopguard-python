# Changelog

All notable changes to slopguard-python are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/), and the project adheres to
[Semantic Versioning](https://semver.org/).

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
