"""Coverage for the test runner.

The runner's branch logic is exercised with white-box patches; its real
subprocess plumbing (``_spawn``, ``_produce_json``, ``_ensure_coverage_available``)
is exercised with *plain* subprocesses. We deliberately avoid running
``coverage run`` nested inside this (already coverage-measured) test process —
that nesting is fragile under coverage.py's ``sys.monitoring`` core on Python
3.12+. The real end-to-end ``coverage run -m unittest`` path is validated
non-nested by the dogfood/baseline CI job instead.
"""

import os
import subprocess
import sys
import tempfile
import unittest

import slopguard.coverage.runner as runner
from slopguard.coverage.runner import (
    _ensure_coverage_available,
    _produce_json,
    _spawn,
    run_tests,
)
from slopguard.errors import ERR_RUNNER_UNAVAILABLE, ERR_TEST_RUN_FAILED, SlopguardError
from slopguard.progress import NORMAL, ProgressReporter


def _coverage_available():
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "coverage", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc.returncode == 0
    except OSError:
        return False


class RunnerUnavailableTests(unittest.TestCase):
    def test_bad_python_raises_runner_unavailable(self):
        with self.assertRaises(SlopguardError) as ctx:
            run_tests("unittest", ".", tempfile.mkdtemp(), ProgressReporter.silent(), python="/no/such/python-xyz")
        self.assertEqual(ctx.exception.code, ERR_RUNNER_UNAVAILABLE)


class SpawnTests(unittest.TestCase):
    """Exercise the real subprocess plumbing with plain (non-coverage) commands."""

    def _env(self):
        return dict(os.environ)

    def test_captures_stdout_and_exit_zero(self):
        code, tail = _spawn(
            [sys.executable, "-c", "print('hello-from-child')"],
            tempfile.mkdtemp(),
            self._env(),
            ProgressReporter.silent(),
        )
        self.assertEqual(code, 0)
        self.assertIn("hello-from-child", tail)

    def test_nonzero_exit_code(self):
        code, _ = _spawn(
            [sys.executable, "-c", "import sys; sys.exit(3)"],
            tempfile.mkdtemp(),
            self._env(),
            ProgressReporter.silent(),
        )
        self.assertEqual(code, 3)

    def test_bad_command_raises_runner_unavailable(self):
        with self.assertRaises(SlopguardError) as ctx:
            _spawn(["/no/such/binary-xyz"], tempfile.mkdtemp(), self._env(), ProgressReporter.silent())
        self.assertEqual(ctx.exception.code, ERR_RUNNER_UNAVAILABLE)

    def test_large_output_is_tail_trimmed_and_streamed(self):
        # >8 KB of output forces the bounded-tail trim; verbose streams it.
        import io

        buf = io.StringIO()
        code, tail = _spawn(
            [sys.executable, "-c", "for _ in range(4000): print('x' * 80)"],
            tempfile.mkdtemp(),
            self._env(),
            ProgressReporter(buf, NORMAL),  # NORMAL discards raw; just exercise drain
        )
        self.assertEqual(code, 0)
        self.assertLessEqual(len(tail), 8 * 1024 + 200)


class RunnerLogicTests(unittest.TestCase):
    """White-box tests of the exit-code -> outcome logic, no real subprocess."""

    def _patch(self, exit_code, produced):
        self._orig = (runner._spawn, runner._produce_json, runner._ensure_coverage_available)
        runner._ensure_coverage_available = lambda python: None
        runner._spawn = lambda argv, cwd, env, progress: (exit_code, "captured output")
        runner._produce_json = lambda *a, **k: produced

    def tearDown(self):
        if hasattr(self, "_orig"):
            runner._spawn, runner._produce_json, runner._ensure_coverage_available = self._orig

    def test_nonzero_exit_no_data_raises(self):
        self._patch(exit_code=1, produced=False)
        with self.assertRaises(SlopguardError) as ctx:
            run_tests("unittest", ".", tempfile.mkdtemp(), ProgressReporter.silent())
        self.assertEqual(ctx.exception.code, ERR_TEST_RUN_FAILED)

    def test_zero_exit_no_data_returns_none(self):
        self._patch(exit_code=0, produced=False)
        outcome = run_tests("pytest", ".", tempfile.mkdtemp(), ProgressReporter.silent())
        self.assertTrue(outcome.tests_passed)
        self.assertIsNone(outcome.coverage_json_path)

    def test_nonzero_exit_with_data_keeps_going(self):
        self._patch(exit_code=1, produced=True)
        outcome = run_tests("unittest", ".", tempfile.mkdtemp(), ProgressReporter.silent())
        self.assertFalse(outcome.tests_passed)
        self.assertIsNotNone(outcome.coverage_json_path)

    def test_zero_exit_with_data_passes(self):
        self._patch(exit_code=0, produced=True)
        outcome = run_tests("pytest", ".", tempfile.mkdtemp(), ProgressReporter.silent())
        self.assertTrue(outcome.tests_passed)
        self.assertIsNotNone(outcome.coverage_json_path)

    def test_coverage_not_installed_raises(self):
        class _Proc:
            returncode = 1

        orig = runner.subprocess.run
        runner.subprocess.run = lambda *a, **k: _Proc()
        try:
            with self.assertRaises(SlopguardError) as ctx:
                _ensure_coverage_available("python")
            self.assertEqual(ctx.exception.code, ERR_RUNNER_UNAVAILABLE)
        finally:
            runner.subprocess.run = orig


@unittest.skipUnless(_coverage_available(), "coverage.py not importable by this interpreter")
class CoverageToolTests(unittest.TestCase):
    """Exercise the real coverage.py invocations without nesting a measured run."""

    def test_ensure_available_passes(self):
        # Should not raise when coverage.py is importable.
        self.assertIsNone(_ensure_coverage_available(sys.executable))

    def test_produce_json_returns_false_without_data(self):
        # `coverage json` with no collected data exits non-zero -> False, not an error.
        d = tempfile.mkdtemp(prefix="slop-nodata-")
        env = dict(os.environ)
        env["COVERAGE_FILE"] = os.path.join(d, ".coverage")  # nonexistent -> no data
        produced = _produce_json(sys.executable, d, env, os.path.join(d, "out.json"), ProgressReporter.silent())
        self.assertFalse(produced)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
