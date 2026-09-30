"""The ``mutate`` orchestrator: plan mutants -> acquire the workspace guard ->
plain baseline (the exact mutant command, unmutated) -> coverage baseline ->
one test run per mutant -> report.

Mutants run one at a time, in report order, each written in place and
restored by the :class:`WorkspaceGuard` — including on SIGINT, SIGTERM and
SIGHUP, which kill the running test process group, restore the file, release
the guard and end the run with exit code 130 / 143 / 129. A file found edited
before one of its mutant writes is left alone: its remaining mutants stay
``pending`` and a note says so. An edit saved while a mutant is in place is
overwritten by the restore.
"""

from __future__ import annotations

import math
import os
import shutil
import signal
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from ..aggregator import format_generated_at
from ..coverage.detection import RUNNER_PYTEST, detect_runner, discover_project_root
from ..coverage.index import CoverageIndex
from ..coverage.pipeline import load_index
from ..coverage.runner import TestOutcome, run_tests
from ..diranalyzer import AnalysisOptions, default_analysis_options, list_files
from ..errors import (
    ERR_RUNNER_UNAVAILABLE,
    SlopguardError,
    baseline_failed,
    project_root_not_found,
    unreadable_file,
)
from ..fileanalyzer import is_generated
from ..formatting import format_number
from ..progress import ProgressReporter
from ..version import TOOL_NAME, VERSION
from .guard import SourceChanged, WorkspaceGuard
from .models import (
    MUTATION_SCHEMA_VERSION,
    STATUS_COMPILE_ERROR,
    STATUS_IGNORED,
    STATUS_NO_COVERAGE,
    STATUS_PENDING,
    PlannedMutant,
    mutant_entries,
    summarize,
)
from .operators import OPERATOR_IDS
from .planner import plan_file
from .runner import (
    TIMEOUT_GRACE_SECONDS,
    CommandOutcome,
    CommandRunner,
    classify,
    compiles,
    has_pytest_cov,
    mutant_command,
    mutant_env,
)
from .source import SourceFile

# Exit codes for the signals that interrupt a run (128 + signal number).
SIGNAL_EXIT_CODES: Dict[str, int] = {"SIGHUP": 129, "SIGINT": 130, "SIGTERM": 143}

NOTE_NO_COVERAGE = "The baseline test run produced no coverage data, so every mutant was run."
NOTE_ALL_SURVIVED = "Every tested mutant survived. Check that the tests import the source under --path."


class MutationInterrupted(BaseException):
    """SIGINT, SIGTERM or SIGHUP stopped the run. Raised from the signal
    handler after the running tests were killed, the mutated file restored and
    the guard released. A ``BaseException`` so no ``except Exception`` swallows
    it."""

    def __init__(self, signal_name: str, exit_code: int, restored: Optional[str]) -> None:
        super().__init__(f"interrupted by {signal_name}")
        self.signal_name = signal_name
        self.exit_code = exit_code
        self.restored = restored


@dataclass
class MutateOptions:
    """What to mutate and how to run the tests."""

    source_path: str  # directory or single source file
    analysis: AnalysisOptions = field(default_factory=default_analysis_options)
    operators: Sequence[str] = OPERATOR_IDS  # enabled operator ids
    runner: Optional[str] = None  # pytest | unittest (auto-detected when None)
    project_dir: Optional[str] = None  # where the tests run (discovered when None)
    coverage: bool = True  # classify mutants on untested lines as no_coverage
    timeout_seconds: Optional[float] = None  # computed from the baseline when None
    dry_run: bool = False  # list the mutants, run nothing
    python: Optional[str] = None  # interpreter for the tests (default: this one)


@dataclass
class _PlannedFile:
    absolute_path: str
    relative_path: str
    source: SourceFile
    mutants: List[PlannedMutant]


@dataclass
class _Execution:
    project_root: str
    runner: str
    timeout_seconds: float
    coverage_available: bool
    notes: List[str]
    statuses: List[str]
    files: Optional[List[_PlannedFile]] = None  # the plan the statuses belong to


@dataclass
class _RunContext:
    argv: List[str]
    env: Dict[str, str]
    project_root: str
    timeout_seconds: float
    coverage: Optional[CoverageIndex]
    guard: WorkspaceGuard
    progress: ProgressReporter
    notes: List[str]


# Injectable for tests: the ``analyze`` coverage runner's signature.
RunTestsFn = Callable[..., TestOutcome]


class MutationPipeline:
    """Runs ``mutate``. The collaborators are injectable for tests."""

    def __init__(
        self,
        command_runner: Optional[CommandRunner] = None,
        run_tests_fn: Optional[RunTestsFn] = None,
        guard_root: Optional[str] = None,
        pytest_cov_probe: Callable[[str, str], bool] = has_pytest_cov,
    ) -> None:
        self.command_runner = command_runner or CommandRunner()
        self.run_tests_fn = run_tests_fn or run_tests
        self.guard_root = guard_root
        self.pytest_cov_probe = pytest_cov_probe

    def run(self, options: MutateOptions, progress: Optional[ProgressReporter] = None) -> Dict[str, Any]:
        """Plan and (unless it is a dry run) test every mutant. Returns the
        mutation report dict."""
        progress = progress or ProgressReporter.silent()
        source_path = os.path.abspath(options.source_path)
        operators = [op for op in OPERATOR_IDS if op in options.operators]

        progress.phase(f"walking {source_path}")
        files = _plan_files(source_path, options.analysis, operators)
        planned = [m for f in files for m in f.mutants]
        progress.phase(f"generated {len(planned)} mutant(s) in {len(files)} file(s)")

        if options.dry_run or all(p.ignored for p in planned):
            # Nothing to run (or asked not to): no guard, no baseline.
            return build_report(source_path, operators, len(files), planned, _unrun_statuses(planned), None)

        execution = self._execute(files, source_path, options, operators, progress)
        files = execution.files or files
        planned = [m for f in files for m in f.mutants]
        report = build_report(source_path, operators, len(files), planned, execution.statuses, execution)
        progress.phase(_done_line(report["summary"]))
        return report

    def _execute(
        self,
        files: List[_PlannedFile],
        source_path: str,
        options: MutateOptions,
        operators: Sequence[str],
        progress: ProgressReporter,
    ) -> _Execution:
        project_root = _project_root(options.project_dir, source_path)
        runner = options.runner
        if runner is None:
            progress.phase(f"detecting test runner in {project_root}")
            runner = detect_runner(project_root)
        python = options.python or sys.executable

        guard = WorkspaceGuard.acquire(project_root, tmp_root=self.guard_root)
        notes = list(guard.notes)
        uninstall = self._install_signal_handlers(guard, progress)
        try:
            if notes:
                # Recovery may have restored a file the plan was read from: plan again.
                files = _plan_files(source_path, options.analysis, operators)
            pytest_cov = runner == RUNNER_PYTEST and self.pytest_cov_probe(python, project_root)
            argv = mutant_command(runner, python, pytest_cov)
            env = mutant_env()
            progress.phase(f"running baseline tests ({runner}) in {project_root}")
            baseline = self.command_runner.run(argv, project_root, env, None, progress)
            if baseline.exit_code != 0:
                raise baseline_failed(baseline.exit_code, baseline.output_tail.strip() or "no output captured")
            timeout = options.timeout_seconds
            if timeout is None:
                timeout = math.ceil(baseline.seconds * 3) + TIMEOUT_GRACE_SECONDS
            progress.phase(
                f"baseline passed in {baseline.seconds:.1f}s; timeout is {format_number(timeout)}s per mutant"
            )
            coverage = None
            if options.coverage:
                coverage = self._coverage_baseline(runner, project_root, python, notes, progress)
            context = _RunContext(argv, env, project_root, timeout, coverage, guard, progress, notes)
            statuses = self._run_mutants(files, context)
            return _Execution(project_root, runner, timeout, coverage is not None, notes, statuses, files)
        finally:
            uninstall()
            guard.release()

    def _coverage_baseline(
        self,
        runner: str,
        project_root: str,
        python: str,
        notes: List[str],
        progress: ProgressReporter,
    ) -> Optional[CoverageIndex]:
        """The ``analyze`` coverage run. It only supplies line coverage — the
        plain baseline already decided pass/fail — so nothing it does is
        fatal: without usable data every mutant is run. (A signal still
        stops the run: ``MutationInterrupted`` is not an ``Exception``.)"""
        coverage_dir = tempfile.mkdtemp(prefix="slopguard-")
        try:
            try:
                outcome = self.run_tests_fn(
                    runner, project_root, coverage_dir, progress, python=python, raise_on_failure=False
                )
                index = None
                if outcome.coverage_json_path is not None:
                    index = load_index(outcome.coverage_json_path, project_root)
            except Exception as exc:  # noqa: BLE001 — coverage.py missing, a launch failure, a bad report
                message = exc.message if isinstance(exc, SlopguardError) else str(exc)
                progress.phase(f"no coverage data: {message}")
                notes.append(NOTE_NO_COVERAGE)
                return None
            if index is None or index.file_count() == 0:
                notes.append(NOTE_NO_COVERAGE)
                return None
            if outcome.exit_code != 0:
                notes.append(
                    f"The coverage run exited with code {outcome.exit_code}; its coverage data was still used."
                )
            return index
        finally:
            shutil.rmtree(coverage_dir, ignore_errors=True)

    def _run_mutants(self, files: List[_PlannedFile], context: _RunContext) -> List[str]:
        total = sum(len(f.mutants) for f in files)
        statuses: List[str] = []
        for f in files:
            changed = False
            for mutant in f.mutants:
                if changed and not mutant.ignored:
                    status, seconds = STATUS_PENDING, None  # the file changed under us
                else:
                    status, seconds = self._mutant_status(f, mutant, context)
                    changed = changed or status == STATUS_PENDING
                statuses.append(status)
                timing = "" if seconds is None else f" ({seconds:.1f}s)"
                index = str(len(statuses)).rjust(len(str(total)))
                site = mutant.site
                context.progress.phase(
                    f"[{index}/{total}] {status:<13} "
                    f"{site.file}:{site.line}:{site.column} {site.operator}{timing}"
                )
        return statuses

    def _mutant_status(
        self, f: _PlannedFile, mutant: PlannedMutant, context: _RunContext
    ) -> Tuple[str, Optional[float]]:
        if mutant.ignored:
            return STATUS_IGNORED, None
        site = mutant.site
        coverage = context.coverage
        if coverage is not None and coverage.method_coverage(f.absolute_path, site.line, site.line, exact=True) == 0:
            return STATUS_NO_COVERAGE, None
        text = f.source.splice(site.start, site.end, site.replacement)
        if not compiles(text, f.absolute_path):
            return STATUS_COMPILE_ERROR, None

        def run_tests_against_mutant() -> CommandOutcome:
            return self.command_runner.run(
                context.argv, context.project_root, context.env, context.timeout_seconds, context.progress
            )

        try:
            outcome = context.guard.with_mutant(
                f.absolute_path, f.source.data, f.source.encode(text), run_tests_against_mutant
            )
        except SourceChanged:
            # Edited during the run: leave the edit alone and skip the file.
            context.notes.append(changed_file_note(f.absolute_path))
            return STATUS_PENDING, None
        return classify(outcome), outcome.seconds

    def _install_signal_handlers(self, guard: WorkspaceGuard, progress: ProgressReporter) -> Callable[[], None]:
        """Restore, release and stop on SIGINT/SIGTERM/SIGHUP. Returns the
        uninstaller. Python runs signal handlers in the main thread only, so a
        run on another thread keeps the default handlers."""
        if threading.current_thread() is not threading.main_thread():
            return lambda: None
        codes = {}
        for name, code in SIGNAL_EXIT_CODES.items():
            signum = getattr(signal, name, None)
            if signum is not None:
                codes[signum] = (name, code)
        fired: List[bool] = []

        def handler(signum: int, frame: Any) -> None:
            if fired:
                return  # already stopping; let the cleanup finish
            fired.append(True)
            self.command_runner.kill_active()
            restored = None
            try:
                restored = guard.restore()
                guard.release()
            except SlopguardError as exc:
                progress.phase(exc.message)
            progress.phase("interrupted" if restored is None else f"interrupted — restored {restored}")
            name, code = codes[signum]
            raise MutationInterrupted(name, code, restored)

        previous = [(signum, signal.signal(signum, handler)) for signum in codes]

        def uninstall() -> None:
            for signum, old in previous:
                signal.signal(signum, old)

        return uninstall


def run(options: MutateOptions, progress: Optional[ProgressReporter] = None) -> Dict[str, Any]:
    """Run ``mutate`` with the default collaborators."""
    return MutationPipeline().run(options, progress)


def build_report(
    source_root: str,
    operators: Sequence[str],
    file_count: int,
    planned: Sequence[PlannedMutant],
    statuses: Sequence[str],
    execution: Optional[_Execution],
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """The mutation report dict (camelCase keys) — it *is* the JSON model
    shared with every port."""
    mutants = mutant_entries(planned, statuses)
    summary = summarize(mutants, file_count)
    notes = (list(execution.notes) if execution is not None else []) + _result_notes(summary)
    return {
        "schemaVersion": MUTATION_SCHEMA_VERSION,
        "reportType": "mutation",
        "tool": TOOL_NAME,
        "toolVersion": VERSION,
        "generatedAt": format_generated_at(generated_at),
        "sourceRoot": source_root,
        "projectRoot": execution.project_root if execution is not None else None,
        "runner": execution.runner if execution is not None else None,
        "timeoutSeconds": execution.timeout_seconds if execution is not None else None,
        "coverageAvailable": execution.coverage_available if execution is not None else False,
        "operators": list(operators),
        "notes": notes,
        "summary": summary,
        "mutants": mutants,
    }


def changed_file_note(path: str) -> str:
    """The note for a file someone edited while ``mutate`` was running."""
    return f"{path} changed while mutate was running, so its remaining mutants were not run."


def _unrun_statuses(planned: Sequence[PlannedMutant]) -> List[str]:
    return [STATUS_IGNORED if p.ignored else STATUS_PENDING for p in planned]


def _done_line(s: Dict[str, Any]) -> str:
    return (
        f"done — {s['killed']} killed, {s['timedOut']} timeout, {s['survived']} survived, "
        f"{s['noCoverage']} no_coverage, {s['compileErrors']} compile_error, {s['ignored']} ignored"
    )


def _result_notes(summary: Dict[str, Any]) -> List[str]:
    notes: List[str] = []
    if summary["survived"] > 0 and summary["killed"] + summary["timedOut"] == 0:
        notes.append(NOTE_ALL_SURVIVED)
    if summary["compileErrors"] > 0:
        notes.append(f"{summary['compileErrors']} mutant(s) did not compile and are excluded from the score.")
    return notes


def _plan_files(source_path: str, options: AnalysisOptions, operators: Sequence[str]) -> List[_PlannedFile]:
    planned: List[_PlannedFile] = []
    for absolute_path, relative_path in list_files(source_path, options):
        try:
            with open(absolute_path, "rb") as fh:
                data = fh.read()
        except OSError as exc:
            raise unreadable_file(absolute_path, exc)
        source = SourceFile(data, absolute_path)
        # Generated files are scanned (they count in fileCount) but not mutated.
        mutants = [] if is_generated(source.text) else plan_file(source, relative_path, operators)
        planned.append(_PlannedFile(absolute_path, relative_path, source, mutants))
    return planned


def _project_root(project_dir: Optional[str], source_path: str) -> str:
    if project_dir:
        return os.path.abspath(project_dir)
    root, found = discover_project_root(source_path)
    if not found:
        raise project_root_not_found(source_path, hint="Pass --project-dir to point at the project root.")
    return root
