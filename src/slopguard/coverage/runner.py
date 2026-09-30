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
    """Where coverage landed after a test run, plus how the run ended."""

    __slots__ = ("coverage_json_path", "tests_passed", "exit_code", "output_tail")

    def __init__(
        self,
        coverage_json_path: Optional[str],
        tests_passed: bool,
        exit_code: int = 0,
        output_tail: str = "",
    ) -> None:
        self.coverage_json_path = coverage_json_path
        self.tests_passed = tests_passed
        self.exit_code = exit_code
        self.output_tail = output_tail


def run_tests(
    runner: str,
    project_root: str,
    coverage_dir: str,
    progress: ProgressReporter,
    python: Optional[str] = None,
    raise_on_failure: bool = True,
) -> TestOutcome:
    """Run the suite under coverage and return where coverage landed.

    A non-zero exit with a coverage report present means tests failed but
    coverage was still emitted — keep going. A non-zero exit with no report means
    the run itself broke (import/build error, missing coverage) — abort with the
    output tail, or, with ``raise_on_failure=False`` (the ``mutate`` coverage
    baseline), return an outcome with no coverage path instead.
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
        return TestOutcome(json_path if produced else None, True, exit_code, tail)
    if produced:
        return TestOutcome(json_path, False, exit_code, tail)
    if not raise_on_failure:
        return TestOutcome(None, False, exit_code, tail)
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
    try:
        for line in proc.stdout:
            progress.raw(line)
            chunks.append(line)
            size += len(line)
            while len(chunks) > 1 and size > _TAIL_LIMIT:
                size -= len(chunks.pop(0))
    except BaseException:
        # Interrupted (a signal, Ctrl-C): never leave the test run behind.
        proc.kill()
        proc.wait()
        raise
    finally:
        proc.stdout.close()
    code = proc.wait()
    return code, "".join(chunks)
