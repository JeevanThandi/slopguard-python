"""The analyze -> coverage-join -> CRAP-report orchestrator."""

from __future__ import annotations

import os
import shutil
import tempfile
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from ..aggregator import aggregate
from ..diranalyzer import AnalysisOptions, analyze_tree
from ..errors import project_root_not_found, unreadable_file
from ..progress import ProgressReporter
from .detection import detect_runner, discover_project_root
from .index import CoverageIndex
from .report import parse_coverage_json
from .runner import TestOutcome, run_tests

# How the pipeline obtains its coverage signal.
MODE_AUTO = "auto"  # drive the project's tests (default)
MODE_PREBUILT = "prebuilt"  # ingest an existing coverage.py JSON report
MODE_NONE = "none"  # skip coverage entirely; every method reads 0%


@dataclass
class CoverageSource:
    """The user's coverage choice and its parameters."""

    mode: str = MODE_AUTO
    coverage_file: Optional[str] = None  # prebuilt report (MODE_PREBUILT)
    project_dir: Optional[str] = None  # overrides project-root discovery
    runner: Optional[str] = None  # pytest | unittest (auto-detected when None)


# Injectable for tests: (runner, project_root, coverage_dir, progress) -> TestOutcome.
RunTestsFn = Callable[[str, str, str, ProgressReporter], TestOutcome]


def run(
    source_path: str,
    coverage: CoverageSource,
    threshold: float,
    options: AnalysisOptions,
    progress: Optional[ProgressReporter] = None,
    run_tests_fn: Optional[RunTestsFn] = None,
) -> Dict[str, Any]:
    """Execute the full analyze -> coverage -> report pipeline against a
    directory or single Python source file."""
    progress = progress or ProgressReporter.silent()
    run_tests_fn = run_tests_fn or run_tests
    source_path = os.path.abspath(source_path)

    progress.phase(f"walking {source_path}")
    file_reports = analyze_tree(source_path, options)
    method_count = sum(len(fr.methods) for fr in file_reports)
    progress.phase(
        f"{_plural(len(file_reports), 'source file')}, "
        f"{_plural(method_count, 'method')} parsed"
    )

    resolved = _resolve_coverage(coverage, source_path, progress, run_tests_fn)
    try:
        provider = None
        coverage_data_path: Optional[str] = None
        notes: List[str] = list(resolved.notes)

        if resolved.json_path:
            progress.phase("parsing coverage data")
            index = _load_index(resolved.json_path, resolved.project_root)
            if index.file_count() == 0:
                notes.append(
                    "The test run produced no per-file coverage data — check that the "
                    "project contains tests slopguard can discover. All methods are being "
                    "reported at 0%."
                )
            else:
                provider = index
                if not resolved.ephemeral:
                    coverage_data_path = os.path.abspath(resolved.json_path)

        return aggregate(
            file_reports=file_reports,
            source_root=source_path,
            threshold=threshold,
            coverage=provider,
            coverage_data_path=coverage_data_path,
            notes=notes,
        )
    finally:
        resolved.cleanup()


@dataclass
class _Resolved:
    json_path: Optional[str]
    ephemeral: bool
    project_root: str
    notes: List[str]
    cleanup: Callable[[], None]


def _resolve_coverage(
    coverage: CoverageSource,
    source_path: str,
    progress: ProgressReporter,
    run_tests_fn: RunTestsFn,
) -> _Resolved:
    noop: Callable[[], None] = lambda: None

    if coverage.mode == MODE_NONE:
        return _Resolved(None, False, "", [], noop)

    if coverage.mode == MODE_PREBUILT:
        root = _project_context(coverage.project_dir, source_path)
        return _Resolved(coverage.coverage_file, False, root, [], noop)

    # MODE_AUTO
    root = coverage.project_dir
    if not root:
        discovered, ok = discover_project_root(source_path)
        if not ok:
            raise project_root_not_found(source_path)
        root = discovered
    root = os.path.abspath(root)
    runner = coverage.runner or detect_runner(root)

    temp_dir = tempfile.mkdtemp(prefix="slopguard-")

    def cleanup() -> None:
        shutil.rmtree(temp_dir, ignore_errors=True)

    try:
        outcome = run_tests_fn(runner, root, temp_dir, progress)
    except Exception:
        cleanup()
        raise

    notes: List[str] = []
    if not outcome.tests_passed:
        notes.append(
            "Some tests failed during the coverage run — coverage reflects the failing run."
        )
    if outcome.coverage_json_path is None:
        notes.append(
            "The test run completed but no coverage data was produced — either no tests "
            "were discovered or coverage tooling is missing. All methods are being "
            "reported at 0%."
        )
    return _Resolved(outcome.coverage_json_path, True, root, notes, cleanup)


def _project_context(project_dir: Optional[str], source_path: str) -> str:
    if project_dir:
        return os.path.abspath(project_dir)
    discovered, ok = discover_project_root(source_path)
    if ok:
        return discovered
    return os.path.abspath(source_path)


def _load_index(json_path: str, project_root: str) -> CoverageIndex:
    try:
        with open(json_path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except OSError as exc:
        raise unreadable_file(json_path, exc)
    coverage_map = parse_coverage_json(text)
    return CoverageIndex(coverage_map, project_root)


def _plural(n: int, singular: str) -> str:
    word = singular if n == 1 else singular + "s"
    return f"{n} {word}"
