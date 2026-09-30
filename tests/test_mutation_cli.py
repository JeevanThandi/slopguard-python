"""The ``mutate`` command: help, flag validation, report output, exit codes,
and real end-to-end runs (the sample app with its own unittest suite, a real
timeout, a real SIGINT).

The end-to-end runs use temporary copies, never the checked-in sample app, and
``--no-coverage`` so no ``coverage run`` nests inside a measured test process
(see ``test_runner_integration.py``); the coverage path is exercised by the CI
baseline step and by the unmeasured test below.
"""

import io
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

import slopguard.cli as climod
from slopguard.cli import run as cli_run
from slopguard.mutation.guard import guard_directory
from slopguard.mutation.pipeline import MutationInterrupted

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SRC = os.path.join(REPO, "src")
SAMPLE_APP = os.path.join(REPO, "sample-apps", "todolist")


class PrivateTempRoot(unittest.TestCase):
    """Point the OS temp dir — the guard's default root, and where the test
    projects live — at a private directory that is removed afterwards, so the
    suite leaves no guard directories behind."""

    def setUp(self):
        self.temp_root = tempfile.mkdtemp(prefix="slop-tmproot-")
        saved = tempfile.tempdir
        tempfile.tempdir = self.temp_root
        self.addCleanup(shutil.rmtree, self.temp_root, True)
        self.addCleanup(setattr, tempfile, "tempdir", saved)


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = cli_run(list(args), out, err)
    return out.getvalue(), err.getvalue(), code


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def make_project(files):
    root = tempfile.mkdtemp(prefix="slop-mutcli-")
    write(os.path.join(root, "pyproject.toml"), "[project]\nname = 'x'\n")
    for rel, body in files.items():
        write(os.path.join(root, rel), body)
    return root


def coverage_importable():
    try:
        proc = subprocess.run([sys.executable, "-m", "coverage", "--version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except OSError:  # pragma: no cover
        return False
    return proc.returncode == 0


# A project whose one test passes for the original but never checks the value.
WEAK_TESTS = {
    "calc.py": "def smaller(a, b):\n    return a < b\n",
    "test_calc.py": "import unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
    "    def test_runs(self):\n        calc.smaller(1, 2)\n",
}


class HelpAndValidationTests(unittest.TestCase):
    def test_top_level_help_lists_mutate(self):
        out, _, code = run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("mutate [flags]", out)
        self.assertIn("mutate    Mutate the source", out)

    def test_mutate_help(self):
        out, _, code = run_cli("mutate", "--help")
        self.assertEqual(code, 0)
        for text in ("--operators", "--fail-under", "--dry-run", "--timeout", "remove_call", "slopguard-ignore-mutant"):
            self.assertIn(text, out)

    def test_invalid_numbers_and_operators(self):
        cases = [
            (["--timeout", "0"], "Invalid argument '--timeout': not a positive number: 0"),
            (["--timeout", "-2"], "not a positive number: -2"),
            (["--timeout", "soon"], "not a positive number: soon"),
            (["--timeout", "inf"], "not a positive number: inf"),
            (["--timeout", "nan"], "not a positive number: nan"),
            (["--fail-under", "high"], "Invalid argument '--fail-under': not a number: high"),
            (["--operators", "boundary,bogus"], "unknown operator(s): bogus"),
        ]
        for flags, message in cases:
            _, err, code = run_cli("mutate", "--dry-run", *flags)
            self.assertEqual(code, 1, flags)
            self.assertIn("slopguard-python: [invalid_argument]", err)
            self.assertIn(message, err)

    def test_invalid_argument_as_json(self):
        _, err, code = run_cli("mutate", "--timeout", "0", "--json")
        self.assertEqual(code, 1)
        envelope = json.loads(err)
        self.assertEqual(envelope["error"]["code"], "invalid_argument")

    def test_analyze_only_flags_are_rejected(self):
        for flags in (["--threshold", "5"], ["--fail-over", "5"], ["--coverage-file", "c.json"], ["--runner", "jest"]):
            _, err, code = run_cli("mutate", "--dry-run", *flags)
            self.assertEqual(code, 1, flags)
            self.assertIn("usage:", err)

    def test_missing_path_is_an_error_envelope(self):
        _, err, code = run_cli("mutate", "--path", "/no/such/dir/xyz", "--dry-run", "--json", "--quiet")
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(err)["error"]["code"], "file_not_found")


class DryRunCliTests(PrivateTempRoot):
    def setUp(self):
        super().setUp()
        self.project = make_project(WEAK_TESTS)

    def test_text(self):
        out, err, code = run_cli("mutate", "--path", self.project, "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("Mutants (2, not run)", out)
        self.assertIn("calc.py:2:14  boundary  `<` → `<=`  smaller", out)
        self.assertIn("runner:    (not run)", out)
        self.assertIn("slopguard: generated 2 mutant(s) in 1 file(s)", err)

    def test_json_and_quiet(self):
        out, err, code = run_cli("mutate", "--path", self.project, "--dry-run", "--json", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")
        report = json.loads(out)
        self.assertEqual(report["reportType"], "mutation")
        self.assertEqual({m["status"] for m in report["mutants"]}, {"pending"})
        self.assertEqual(report["summary"]["fileCount"], 1)  # test_calc.py is excluded by default

    def test_include_exclude_and_default_excludes(self):
        out, _, _ = run_cli("mutate", "--path", self.project, "--dry-run", "--json", "--no-default-excludes")
        self.assertEqual(json.loads(out)["summary"]["fileCount"], 2)
        out, _, _ = run_cli("mutate", "--path", self.project, "--dry-run", "--json", "--exclude", "calc.py")
        self.assertEqual(json.loads(out)["summary"]["fileCount"], 0)
        out, _, _ = run_cli(
            "mutate", "--path", self.project, "--dry-run", "--json", "--no-default-excludes", "--include", "test_*"
        )
        self.assertEqual(json.loads(out)["summary"]["fileCount"], 1)

    def test_operators_flag_repeats(self):
        out, _, _ = run_cli(
            "mutate", "--path", self.project, "--dry-run", "--json", "--operators", "negate_conditional", "--operators", "increment"
        )
        report = json.loads(out)
        self.assertEqual(report["operators"], ["increment", "negate_conditional"])
        self.assertEqual([m["operator"] for m in report["mutants"]], ["negate_conditional"])

    def test_fail_under_is_ignored_in_a_dry_run(self):
        _, _, code = run_cli("mutate", "--path", self.project, "--dry-run", "--fail-under", "100")
        self.assertEqual(code, 0)

    def test_null_score_never_fails(self):
        project = make_project({"calc.py": "def f():\n    return True  # slopguard-ignore-mutant\n"})
        out, _, code = run_cli("mutate", "--path", project, "--fail-under", "100", "--json", "--quiet")
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(out)["summary"]["mutationScore"])


class ExitCodeTests(PrivateTempRoot):
    def test_interrupt_maps_to_its_exit_code(self):
        original = climod.run_mutation

        def interrupted(options, progress):
            raise MutationInterrupted("SIGTERM", 143, None)

        climod.run_mutation = interrupted
        try:
            _, _, code = run_cli("mutate", "--path", ".")
        finally:
            climod.run_mutation = original
        self.assertEqual(code, 143)

    def test_fail_under_exit_two_on_a_real_run(self):
        project = make_project(WEAK_TESTS)
        out, err, code = run_cli(
            "mutate", "--path", os.path.join(project, "calc.py"), "--project-dir", project, "--no-coverage", "--fail-under", "50"
        )
        self.assertEqual(code, 2)
        self.assertIn("slopguard-python: mutation score 0.00% is below --fail-under 50.00\n", err)
        self.assertIn("Survived (2) — tests still pass with these changes", out)
        self.assertIn("Every tested mutant survived.", out)

    def test_fail_under_met(self):
        project = make_project(WEAK_TESTS)
        _, _, code = run_cli(
            "mutate", "--path", os.path.join(project, "calc.py"), "--project-dir", project, "--no-coverage", "--fail-under", "0"
        )
        self.assertEqual(code, 0)

    def test_real_timeout(self):
        project = make_project(
            {
                "calc.py": "def flag():\n    return True\n",
                "test_calc.py": "import time, unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
                "    def test_flag(self):\n        if not calc.flag():\n            time.sleep(120)\n"
                "        self.assertTrue(calc.flag())\n",
            }
        )
        started = time.monotonic()
        out, _, code = run_cli(
            "mutate", "--path", os.path.join(project, "calc.py"), "--project-dir", project,
            "--no-coverage", "--timeout", "3", "--json", "--quiet",
        )
        self.assertEqual(code, 0)
        self.assertLess(time.monotonic() - started, 90)
        report = json.loads(out)
        self.assertEqual([m["status"] for m in report["mutants"]], ["timeout"])
        self.assertEqual(report["timeoutSeconds"], 3)
        self.assertEqual(report["summary"]["mutationScore"], 100)


class SampleAppEndToEndTests(PrivateTempRoot):
    """The checked-in sample app, copied, with its real unittest suite."""

    def setUp(self):
        super().setUp()
        self.copy = os.path.join(tempfile.mkdtemp(prefix="slop-todolist-"), "todolist")
        shutil.copytree(SAMPLE_APP, self.copy, ignore=shutil.ignore_patterns("__pycache__"))
        self.package = os.path.join(self.copy, "todolist")

    def snapshot(self):
        state = {}
        for name in sorted(os.listdir(self.package)):
            path = os.path.join(self.package, name)
            if name.endswith(".py"):
                with open(path, "rb") as fh:
                    state[name] = (fh.read(), os.stat(path).st_mtime_ns)
        return state

    def assert_every_mutant_killed(self, report):
        summary = report["summary"]
        self.assertEqual(summary["mutantCount"], 12)
        self.assertEqual(summary["killed"], 12)
        self.assertEqual(summary["survived"], 0)
        self.assertEqual(summary["mutationScore"], 100)
        self.assertEqual(report["runner"], "unittest")
        self.assertEqual(report["notes"], [])

    def test_every_mutant_is_killed(self):
        before = self.snapshot()
        out, _, code = run_cli(
            "mutate", "--path", self.package, "--project-dir", self.copy, "--no-coverage", "--json", "--quiet", "--fail-under", "100"
        )
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assert_every_mutant_killed(report)
        self.assertFalse(report["coverageAvailable"])
        self.assertEqual(self.snapshot(), before)
        self.assertFalse(os.path.exists(guard_directory(self.copy)))
        self.assertTrue(guard_directory(self.copy).startswith(self.temp_root))
        # Every run set PYTHONDONTWRITEBYTECODE, so no mutant bytecode was cached.
        self.assertFalse(os.path.exists(os.path.join(self.package, "__pycache__")))

    @unittest.skipIf("coverage" in sys.modules, "would nest coverage.py inside a measured test run")
    @unittest.skipUnless(coverage_importable(), "coverage.py not importable by this interpreter")
    def test_every_mutant_is_killed_with_the_coverage_baseline(self):  # pragma: no cover — skipped under coverage
        out, _, code = run_cli("mutate", "--path", self.package, "--project-dir", self.copy, "--json", "--quiet")
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assert_every_mutant_killed(report)
        self.assertTrue(report["coverageAvailable"])


@unittest.skipIf("coverage" in sys.modules, "would nest coverage.py inside a measured test run")
@unittest.skipUnless(coverage_importable(), "coverage.py not importable by this interpreter")
class RealCoverageTests(PrivateTempRoot):  # pragma: no cover — skipped under coverage
    def test_untested_lines_are_no_coverage(self):
        # coverage.py reports files under the project as relative paths; the
        # temp dir is a symlink on macOS (/var -> /private/var).
        project = make_project(
            {
                "pkg/__init__.py": "",
                "pkg/calc.py": "def tested(a, b):\n    return a < b\n\n\ndef untested(x):\n    return -x\n",
                "test_calc.py": "import unittest\nfrom pkg.calc import tested\n\n\nclass T(unittest.TestCase):\n"
                "    def test_it(self):\n        self.assertTrue(tested(1, 2))\n        self.assertFalse(tested(2, 2))\n",
            }
        )
        out, _, code = run_cli("mutate", "--path", os.path.join(project, "pkg"), "--project-dir", project, "--json", "--quiet")
        self.assertEqual(code, 0, out)
        report = json.loads(out)
        self.assertTrue(report["coverageAvailable"])
        self.assertEqual(
            [(m["method"], m["operator"], m["status"]) for m in report["mutants"]],
            [
                ("tested", "boundary", "killed"),
                ("tested", "negate_conditional", "killed"),
                ("untested", "invert_negative", "no_coverage"),
            ],
        )


@unittest.skipUnless(os.name == "posix", "POSIX signals")
class RealSignalTests(PrivateTempRoot):
    def test_sigint_restores_and_exits_130(self):
        project = make_project(
            {
                "calc.py": "def flag():\n    return True\n",
                "test_calc.py": "import os, time, unittest\nimport calc\n\n\nclass T(unittest.TestCase):\n"
                "    def test_flag(self):\n"
                "        if not calc.flag():\n"
                "            with open(os.path.join(os.path.dirname(__file__), 'sleeper.pid'), 'w') as fh:\n"
                "                fh.write(str(os.getpid()))\n"
                "            time.sleep(120)\n"
                "        self.assertTrue(calc.flag())\n",
            }
        )
        calc = os.path.join(project, "calc.py")
        pid_file = os.path.join(project, "sleeper.pid")
        with open(calc, "rb") as fh:
            original = fh.read()
        mtime = os.stat(calc).st_mtime_ns
        env = dict(os.environ)
        env["PYTHONPATH"] = SRC
        env["TMPDIR"] = self.temp_root  # the child's guard root
        proc = subprocess.Popen(
            [sys.executable, "-m", "slopguard", "mutate", "--path", calc, "--project-dir", project, "--no-coverage"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 90
            while not (os.path.exists(pid_file) and os.path.getsize(pid_file) > 0):
                if proc.poll() is not None or time.monotonic() > deadline:
                    self.fail(f"the mutant run never started: {proc.communicate()}")
                time.sleep(0.05)
            with open(pid_file) as fh:
                sleeper = int(fh.read())
            with open(calc, "rb") as fh:
                self.assertNotEqual(fh.read(), original)  # the mutant is in place
            proc.send_signal(signal.SIGINT)
            _, err = proc.communicate(timeout=90)
        finally:
            if proc.poll() is None:  # pragma: no cover — only when the test already failed
                proc.kill()
                proc.communicate()
        self.assertEqual(proc.returncode, 130, err)
        self.assertIn(f"slopguard: interrupted — restored {calc}", err.decode())
        with open(calc, "rb") as fh:
            self.assertEqual(fh.read(), original)
        self.assertEqual(os.stat(calc).st_mtime_ns, mtime)
        self.assertFalse(os.path.exists(guard_directory(project, self.temp_root)))
        deadline = time.monotonic() + 30
        while True:
            try:
                os.kill(sleeper, 0)
            except ProcessLookupError:
                break
            self.assertLess(time.monotonic(), deadline, "the mutant's test process outlived the interrupt")
            time.sleep(0.05)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
