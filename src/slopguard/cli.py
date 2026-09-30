"""The command-line surface: the default ``analyze`` command, ``mutate`` and
``version``.

``Run(argv, stdout, stderr) -> int`` is a thin shim over the analysis,
coverage and mutation layers so the wiring can be exercised in-process by
tests. stdout carries the report; stderr carries progress and errors. Exit
codes: 0 success, 1 error, 2 when ``--fail-over`` is exceeded (``analyze``) or
the score is below ``--fail-under`` (``mutate``), 130 / 143 / 129 when
SIGINT / SIGTERM / SIGHUP stops ``mutate``.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
from typing import IO, List, Optional

from . import errors
from .coverage import MODE_AUTO, MODE_NONE, MODE_PREBUILT, CoverageSource
from .coverage import run as run_pipeline
from .crap import DEFAULT_CRAP_THRESHOLD
from .diranalyzer import DEFAULT_EXCLUDE_GLOBS, AnalysisOptions
from .formatting import (
    error_json,
    error_text_line,
    json_report,
    mutation_json_report,
    mutation_pretty_report,
    pretty_report,
)
from .mutation import OPERATOR_IDS, MutateOptions, MutationInterrupted, parse_operators
from .mutation import run as run_mutation
from .mutation.runner import TIMEOUT_GRACE_SECONDS
from .progress import NORMAL, VERBOSE, ProgressReporter
from .version import TOOL_NAME, VERSION

_USAGE = f"""{TOOL_NAME} {VERSION} — CRAP (Change Risk Anti-Patterns) guardrail for Python.

{TOOL_NAME} finds complex, undertested code by combining cyclomatic and
cognitive complexity (parsed via the standard-library `ast`) with line coverage
gathered from the project's own test suite (pytest or unittest, under
coverage.py). Coverage is an artifact of the analysis, not an input.

  Formula:  wCRAP(m) = (cyc × cog) × (1 − cov/100)³ + sqrt(cyc × cog)
  Default crappy threshold: 30.

Use `mutate` to check that the tests catch changes to the code.

Usage:
  {TOOL_NAME} [analyze] [flags]
  {TOOL_NAME} mutate [flags]
  {TOOL_NAME} version

Commands:
  analyze   Walk a directory of Python sources, drive the test suite for
            coverage, emit a wCRAP report (text or JSON). The default command.
  mutate    Mutate the source one change at a time, run the tests against each
            mutant, and report the mutants the tests miss (text or JSON).
  version   Print version metadata as JSON.

Run '{TOOL_NAME} analyze --help' or '{TOOL_NAME} mutate --help' for the flags.
"""

_ANALYZE_EPILOG = f"""examples:
  {TOOL_NAME}                                              # zero-config: analyze .
  {TOOL_NAME} analyze --path ./src --threshold 30
  {TOOL_NAME} analyze --path . --json | jq '.methods | sort_by(-.crap)[:10]'
  {TOOL_NAME} analyze --path . --fail-over 50              # fail CI when any method's CRAP > 50
  {TOOL_NAME} analyze --path ./pkg --no-coverage           # skip the test run (complexity-only)
  {TOOL_NAME} analyze --path . --runner pytest             # name the test runner
  {TOOL_NAME} analyze --path . --coverage-file coverage.json   # join a prebuilt coverage.py report
"""

_MUTATE_EPILOG = f"""statuses: killed (a test failed), survived (every test passed), timeout,
no_coverage (no test runs the line), compile_error, ignored, pending (not run).
operators: {", ".join(OPERATOR_IDS[:5])},
           {", ".join(OPERATOR_IDS[5:])}.
Ignore marker, on the mutated line or on a comment line above it:
  # slopguard-ignore-mutant(boundary): equal values keep the same best

examples:
  {TOOL_NAME} mutate --path src/pkg/store.py           # one file
  {TOOL_NAME} mutate --path src --include "pkg/core/**" --exclude "**/legacy/**"
  {TOOL_NAME} mutate --path src --operators boundary,negate_conditional
  {TOOL_NAME} mutate --path src --dry-run              # list mutants, run nothing
  {TOOL_NAME} mutate --path src --json | jq '.mutants[] | select(.status == "survived")'
  {TOOL_NAME} mutate --path src --fail-under 80        # fail CI below an 80% score
"""


def run(argv: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    """Parse ``argv`` (excluding the program name) and execute the selected
    command."""
    rest = argv
    if argv:
        head = argv[0]
        if head == "version":
            return _run_version(stdout)
        if head in ("-h", "--help", "help"):
            stdout.write(_USAGE)
            return 0
        if head == "--version":
            stdout.write(VERSION + "\n")
            return 0
        if head == "mutate":
            return _run_mutate(argv[1:], stdout, stderr)
        if head == "analyze":
            rest = argv[1:]
    return _run_analyze(rest, stdout, stderr)


def _run_version(stdout: IO[str]) -> int:
    payload = {"name": TOOL_NAME, "version": VERSION}
    stdout.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{TOOL_NAME} analyze",
        description="Analyze a directory or file and print a CRAP report. "
        "slopguard-python drives the project's own test suite (pytest or "
        "unittest) under coverage.py to gather line coverage. Coverage is an "
        "artifact of the analysis, not an input.",
        epilog=_ANALYZE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-p", "--path", default=".", help="Directory of Python sources, or a single .py file.")
    p.add_argument(
        "-t",
        "--threshold",
        type=float,
        default=DEFAULT_CRAP_THRESHOLD,
        help="CRAP threshold above which a method/type is crappy.",
    )
    p.add_argument("--project-dir", help="Project root the test run executes in. Defaults to the nearest project marker above --path.")
    p.add_argument("--runner", choices=("pytest", "unittest"), help="Test runner to drive (auto-detected when omitted).")
    p.add_argument("--no-coverage", action="store_true", help="Skip the test run and report complexity only (every method shows 0%% coverage).")
    p.add_argument("--coverage-file", help="Prebuilt coverage.py JSON report to join instead of running tests.")
    p.add_argument("--include", action="append", metavar="GLOB", help="Glob of files to include. Repeatable.")
    p.add_argument("--exclude", action="append", metavar="GLOB", help="Extra glob of files/dirs to exclude (combined with defaults). Repeatable.")
    p.add_argument("--no-default-excludes", action="store_true", help="Skip built-in excludes (.venv, test files, generated code, caches).")
    p.add_argument("--json", action="store_true", dest="json_out", help="Emit JSON to stdout (default is pretty text).")
    p.add_argument("--fail-over", type=float, default=None, metavar="N", help="Exit with code 2 if any method's CRAP exceeds this value. Useful in CI.")
    p.add_argument("-v", "--verbose", action="store_true", help="Stream test-runner output and other subprocess chatter to stderr.")
    p.add_argument("--quiet", action="store_true", help="Suppress all progress chatter on stderr.")
    return p


def _run_analyze(args: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    parser = _build_parser()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            ns = parser.parse_args(args)
        except SystemExit as exc:
            return 0 if exc.code in (0, None) else 1

    json_out = ns.json_out

    def emit_err(err: BaseException) -> None:
        env = errors.envelope_for(err)
        if json_out:
            stderr.write(error_json(env) + "\n")
        else:
            stderr.write(error_text_line(env) + "\n")

    # Analysis options.
    include = ns.include or []
    exclude = ns.exclude or []
    if ns.no_default_excludes:
        options = AnalysisOptions(include_globs=include, exclude_globs=list(exclude))
    else:
        options = AnalysisOptions(
            include_globs=include, exclude_globs=list(DEFAULT_EXCLUDE_GLOBS) + list(exclude)
        )

    # Coverage source.
    src = CoverageSource(runner=ns.runner)
    if ns.no_coverage:
        src.mode = MODE_NONE
    elif ns.coverage_file:
        src.mode = MODE_PREBUILT
        src.coverage_file = os.path.abspath(_expand_tilde(ns.coverage_file))
    else:
        src.mode = MODE_AUTO
    if ns.project_dir:
        src.project_dir = os.path.abspath(_expand_tilde(ns.project_dir))

    progress = _resolve_progress(stderr, ns.verbose, ns.quiet)
    source_path = os.path.abspath(_expand_tilde(ns.path))

    try:
        report = run_pipeline(
            source_path=source_path,
            coverage=src,
            threshold=ns.threshold,
            options=options,
            progress=progress,
        )
    except Exception as exc:  # noqa: BLE001 — surfaced as a stable error envelope
        emit_err(exc)
        return 1

    if json_out:
        stdout.write(json_report(report) + "\n")
    else:
        stdout.write(pretty_report(report, 20))

    if ns.fail_over is not None and report["methods"]:
        worst = report["methods"][0]
        if worst["crap"] > ns.fail_over:
            stderr.write(
                f"{TOOL_NAME}: CRAP {worst['crap']:.2f} exceeds --fail-over {ns.fail_over:.2f}\n"
            )
            return 2
    return 0


def _build_mutate_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog=f"{TOOL_NAME} mutate",
        description="Mutate the source one change at a time, run the tests against each\n"
        "mutant, and report the mutants the tests miss. A killed mutant shows that a\n"
        "test checks the changed line, not only runs it.",
        epilog=_MUTATE_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("-p", "--path", default=".", help="Directory of Python sources, or a single .py file, to mutate.")
    p.add_argument("--include", action="append", metavar="GLOB", help="Only mutate files matching this glob. Repeatable.")
    p.add_argument("--exclude", action="append", metavar="GLOB", help="Extra glob of files/dirs to skip (combined with defaults). Repeatable.")
    p.add_argument("--no-default-excludes", action="store_true", help="Skip built-in excludes (.venv, test files, generated code, caches).")
    p.add_argument(
        "--operators",
        action="append",
        metavar="IDS",
        help="Comma-separated mutation operator ids to apply. Repeatable. Defaults to all.",
    )
    p.add_argument("--project-dir", help="Project root the tests run in. Defaults to the nearest project marker above --path.")
    p.add_argument("--runner", choices=("pytest", "unittest"), help="Test runner to drive (auto-detected when omitted).")
    p.add_argument(
        "--no-coverage",
        action="store_true",
        help="Skip the coverage run and test every mutant, including mutants on lines no test executes.",
    )
    p.add_argument(
        "--timeout",
        metavar="SECONDS",
        help=f"Per-mutant test timeout in seconds. Defaults to 3 x the baseline run time, rounded up, + {TIMEOUT_GRACE_SECONDS}.",
    )
    p.add_argument("--dry-run", action="store_true", help="List the mutants without running any tests.")
    p.add_argument("--json", action="store_true", dest="json_out", help="Emit JSON to stdout (default is pretty text).")
    p.add_argument(
        "--fail-under",
        metavar="SCORE",
        help="Exit with code 2 if the mutation score (0-100) is below this value. Useful in CI.",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Stream test-runner output to stderr.")
    p.add_argument("--quiet", action="store_true", help="Suppress all progress chatter on stderr.")
    return p


def _run_mutate(args: List[str], stdout: IO[str], stderr: IO[str]) -> int:
    parser = _build_mutate_parser()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        try:
            ns = parser.parse_args(args)
        except SystemExit as exc:
            return 0 if exc.code in (0, None) else 1

    json_out = ns.json_out

    def emit_err(err: BaseException) -> None:
        env = errors.envelope_for(err)
        stderr.write((error_json(env) if json_out else error_text_line(env)) + "\n")

    try:
        options = _mutate_options(ns)
        fail_under = _parse_number("--fail-under", ns.fail_under, positive=False)
    except errors.SlopguardError as exc:
        emit_err(exc)
        return 1
    progress = _resolve_progress(stderr, ns.verbose, ns.quiet)

    try:
        report = run_mutation(options, progress)
    except MutationInterrupted as exc:
        return exc.exit_code
    except Exception as exc:  # noqa: BLE001 — surfaced as a stable error envelope
        emit_err(exc)
        return 1

    stdout.write(mutation_json_report(report) + "\n" if json_out else mutation_pretty_report(report))

    score = report["summary"]["mutationScore"]
    if _below_fail_under(score, fail_under, ns.dry_run):
        stderr.write(f"{TOOL_NAME}: mutation score {score:.2f}% is below --fail-under {fail_under:.2f}\n")
        return 2
    return 0


def _mutate_options(ns: argparse.Namespace) -> MutateOptions:
    """Validate the ``mutate`` flags. Raises ``invalid_argument``."""
    exclude = list(ns.exclude or [])
    defaults = [] if ns.no_default_excludes else list(DEFAULT_EXCLUDE_GLOBS)
    return MutateOptions(
        source_path=os.path.abspath(_expand_tilde(ns.path)),
        analysis=AnalysisOptions(include_globs=ns.include or [], exclude_globs=defaults + exclude),
        operators=parse_operators(ns.operators or []),
        runner=ns.runner,
        project_dir=os.path.abspath(_expand_tilde(ns.project_dir)) if ns.project_dir else None,
        coverage=not ns.no_coverage,
        timeout_seconds=_parse_number("--timeout", ns.timeout, positive=True),
        dry_run=ns.dry_run,
    )


def _below_fail_under(score: Optional[float], fail_under: Optional[float], dry_run: bool) -> bool:
    """``--fail-under`` fails a run whose score is strictly below it. A dry run
    or a ``null`` score never fails."""
    if fail_under is None or dry_run or score is None:
        return False
    return score < fail_under


def _parse_number(name: str, raw: Optional[str], positive: bool) -> Optional[float]:
    """A finite number (``positive``: also > 0), or ``None`` when the flag is
    absent. Raises ``invalid_argument`` otherwise."""
    if raw is None:
        return None
    reason = f"not a positive number: {raw}" if positive else f"not a number: {raw}"
    try:
        value = float(raw)
    except ValueError:
        raise errors.invalid_argument(name, reason)
    if not math.isfinite(value) or (positive and value <= 0):
        raise errors.invalid_argument(name, reason)
    return value


def _resolve_progress(stderr: IO[str], verbose: bool, quiet: bool) -> ProgressReporter:
    if quiet:
        return ProgressReporter.silent()
    if verbose:
        return ProgressReporter(stderr, VERBOSE)
    return ProgressReporter(stderr, NORMAL)


def _expand_tilde(path: str) -> str:
    if path == "~" or path.startswith("~/"):
        return os.path.join(os.path.expanduser("~"), path[2:]) if path.startswith("~/") else os.path.expanduser("~")
    return path
