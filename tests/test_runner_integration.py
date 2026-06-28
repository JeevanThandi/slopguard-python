"""Real subprocess integration for the coverage runner.

These drive an actual ``coverage run`` and are skipped when coverage.py isn't
importable by the interpreter running the tests (so the suite still passes on a
bare stdlib Python). CI runs them inside the dev environment where coverage is
installed.
"""

import os
import subprocess
import sys
import tempfile
import unittest

from slopguard.coverage.index import CoverageIndex
from slopguard.coverage.report import parse_coverage_json
from slopguard.coverage.runner import run_tests
from slopguard.errors import ERR_RUNNER_UNAVAILABLE, ERR_TEST_RUN_FAILED, SlopguardError
from slopguard.progress import ProgressReporter


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


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


class RunnerUnavailableTests(unittest.TestCase):
    def test_bad_python_raises_runner_unavailable(self):
        with self.assertRaises(SlopguardError) as ctx:
            run_tests("unittest", ".", tempfile.mkdtemp(), ProgressReporter.silent(), python="/no/such/python-xyz")
        self.assertEqual(ctx.exception.code, ERR_RUNNER_UNAVAILABLE)


class RunnerLogicTests(unittest.TestCase):
    """White-box tests of the exit-code -> outcome logic, no real subprocess."""

    def _patch(self, exit_code, produced):
        import slopguard.coverage.runner as r

        self._orig = (r._spawn, r._produce_json, r._ensure_coverage_available)
        r._ensure_coverage_available = lambda python: None
        r._spawn = lambda argv, cwd, env, progress: (exit_code, "captured output")
        r._produce_json = lambda *a, **k: produced
        return r

    def tearDown(self):
        if hasattr(self, "_orig"):
            import slopguard.coverage.runner as r

            r._spawn, r._produce_json, r._ensure_coverage_available = self._orig

    def test_nonzero_exit_no_data_raises(self):
        r = self._patch(exit_code=1, produced=False)
        with self.assertRaises(SlopguardError) as ctx:
            r.run_tests("unittest", ".", tempfile.mkdtemp(), ProgressReporter.silent())
        self.assertEqual(ctx.exception.code, ERR_TEST_RUN_FAILED)

    def test_zero_exit_no_data_returns_none(self):
        r = self._patch(exit_code=0, produced=False)
        outcome = r.run_tests("pytest", ".", tempfile.mkdtemp(), ProgressReporter.silent())
        self.assertTrue(outcome.tests_passed)
        self.assertIsNone(outcome.coverage_json_path)

    def test_nonzero_exit_with_data_keeps_going(self):
        r = self._patch(exit_code=1, produced=True)
        outcome = r.run_tests("unittest", ".", tempfile.mkdtemp(), ProgressReporter.silent())
        self.assertFalse(outcome.tests_passed)
        self.assertIsNotNone(outcome.coverage_json_path)

    def test_coverage_not_installed_raises(self):
        import slopguard.coverage.runner as r

        class _Proc:
            returncode = 1

        orig = r.subprocess.run
        r.subprocess.run = lambda *a, **k: _Proc()
        try:
            with self.assertRaises(SlopguardError) as ctx:
                r._ensure_coverage_available("python")
            self.assertEqual(ctx.exception.code, ERR_RUNNER_UNAVAILABLE)
        finally:
            r.subprocess.run = orig


@unittest.skipUnless(_coverage_available(), "coverage.py not importable by this interpreter")
class RealRunnerTests(unittest.TestCase):
    def setUp(self):
        self.proj = tempfile.mkdtemp(prefix="slop-real-")
        write(
            os.path.join(self.proj, "calc.py"),
            "def add(a, b):\n    return a + b\n\n\ndef unused(a):\n    if a:\n        return 1\n    return 0\n",
        )

    def test_unittest_runner_produces_coverage(self):
        write(
            os.path.join(self.proj, "test_calc.py"),
            "import unittest, calc\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 3)\n",
        )
        cov_dir = tempfile.mkdtemp(prefix="slop-realcov-")
        outcome = run_tests("unittest", self.proj, cov_dir, ProgressReporter.silent())
        self.assertTrue(outcome.tests_passed)
        self.assertIsNotNone(outcome.coverage_json_path)

        with open(outcome.coverage_json_path, encoding="utf-8") as fh:
            index = CoverageIndex(parse_coverage_json(fh.read()), self.proj)
        cov = index.file_coverage(os.path.join(self.proj, "calc.py"))
        self.assertIsNotNone(cov)
        self.assertGreater(cov, 0.0)

    def test_failing_tests_still_emit_coverage(self):
        # A failing test yields a non-zero exit, but coverage is still emitted —
        # a partial run is useful, so we keep going (tests_passed False).
        write(
            os.path.join(self.proj, "test_calc.py"),
            "import unittest, calc\n"
            "class T(unittest.TestCase):\n"
            "    def test_add(self):\n        self.assertEqual(calc.add(1, 2), 99)\n",
        )
        cov_dir = tempfile.mkdtemp(prefix="slop-realcov-")
        outcome = run_tests("unittest", self.proj, cov_dir, ProgressReporter.silent())
        self.assertFalse(outcome.tests_passed)
        self.assertIsNotNone(outcome.coverage_json_path)

    def test_no_tests_discovered_produces_no_failure(self):
        # No test files: unittest discovers nothing and exits 0; coverage may be
        # empty, which is a note (None path), not an error.
        cov_dir = tempfile.mkdtemp(prefix="slop-realcov-")
        outcome = run_tests("unittest", self.proj, cov_dir, ProgressReporter.silent())
        self.assertTrue(outcome.tests_passed)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
