"""Drive the project's own test suite under coverage.py to PRODUCE a report.

The analog of slopguard-swift driving ``xcodebuild test`` and
slopguard-typescript driving vitest/jest. We own that step: run the project's
tests with ``python -m coverage run`` so a ``coverage.json`` lands in a
slopguard-owned directory, then hand the path back for indexing.

slopguard-python itself has zero runtime dependencies; coverage.py is a tool in
the *target* project's environment (like ``go test`` or vitest), invoked through
the interpreter running slopguard. Run slopguard-python from the project's
environment so its tests and the package under test are importable.
"""

from __future__ import annotations

import os
import subprocess
import sys
from typing import List, Optional, Tuple

from ..errors import runner_unavailable, test_run_failed
from ..progress import ProgressReporter
from .detection import RUNNER_PYTEST

_TAIL_LIMIT = 8 * 1024


class TestOutcome:
    """Where coverage landed after a test run."""

    __slots__ = ("coverage_json_path", "tests_passed")

    def __init__(self, coverage_json_path: Optional[str], tests_passed: bool) -> None:
        self.coverage_json_path = coverage_json_path
        self.tests_passed = tests_passed


def run_tests(
    runner: str,
    project_root: str,
    coverage_dir: str,
    progress: ProgressReporter,
    python: Optional[str] = None,
) -> TestOutcome:
    """Run the suite under coverage and return where coverage landed.

    A non-zero exit with a coverage report present means tests failed but
    coverage was still emitted — keep going. A non-zero exit with no report means
    the run itself broke (import/build error, missing coverage) — abort with the
    output tail.
    """
    python = python or sys.executable
    _ensure_coverage_available(python)

    data_file = os.path.join(coverage_dir, ".coverage")
    json_path = os.path.join(coverage_dir, "coverage.json")
    env = dict(os.environ)
    env["COVERAGE_FILE"] = data_file
    env["CI"] = "1"

    argv = [python, "-m", "coverage", "run", "--source", project_root]
    if runner == RUNNER_PYTEST:
        argv += ["-m", "pytest"]
    else:
        argv += ["-m", "unittest", "discover"]

    progress.phase(
        f"running {runner} under coverage in {project_root} — this can take a while"
    )
    exit_code, tail = _spawn(argv, project_root, env, progress)

    produced = _produce_json(python, project_root, env, json_path, progress)

    if exit_code == 0:
        return TestOutcome(json_path if produced else None, True)
    if produced:
        return TestOutcome(json_path, False)
    raise test_run_failed(exit_code, tail.strip() or "no output captured")


def _produce_json(
    python: str,
    project_root: str,
    env: dict,
    json_path: str,
    progress: ProgressReporter,
) -> bool:
    """Convert the collected ``.coverage`` data into ``coverage.json``. Returns
    False (rather than erroring) when no data was collected — an empty run is a
    note, not a failure."""
    argv = [python, "-m", "coverage", "json", "-o", json_path]
    code, _ = _spawn(argv, project_root, env, progress)
    return code == 0 and os.path.exists(json_path)


def _ensure_coverage_available(python: str) -> None:
    try:
        proc = subprocess.run(
            [python, "-m", "coverage", "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise runner_unavailable(f"could not launch '{python}': {exc}")
    if proc.returncode != 0:
        raise runner_unavailable(
            f"coverage.py is not installed for {python}. Install it "
            f"(pip install coverage), run slopguard-python from the project's "
            f"environment, or pass --no-coverage."
        )


def _spawn(
    argv: List[str], cwd: str, env: dict, progress: ProgressReporter
) -> Tuple[int, str]:
    """Run ``argv`` in ``cwd``, draining output into a bounded tail (streamed
    through under --verbose). Returns ``(exit_code, output_tail)``."""
    try:
        proc = subprocess.Popen(
            argv,
            cwd=cwd,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
    except OSError as exc:
        raise runner_unavailable(f"could not launch {' '.join(argv)}: {exc}")

    chunks: List[str] = []
    size = 0
    assert proc.stdout is not None
    for line in proc.stdout:
        progress.raw(line)
        chunks.append(line)
        size += len(line)
        while len(chunks) > 1 and size > _TAIL_LIMIT:
            size -= len(chunks.pop(0))
    proc.stdout.close()
    code = proc.wait()
    return code, "".join(chunks)
