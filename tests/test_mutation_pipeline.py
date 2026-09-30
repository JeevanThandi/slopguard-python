"""The ``mutate`` pipeline with injected collaborators: dry runs, the two
baselines, no-coverage and compile-error classification, restore on errors
and signals, and the report it builds."""

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

import slopguard.mutation.pipeline as pipeline_module
from slopguard.coverage.runner import TestOutcome
from slopguard.diranalyzer import default_analysis_options
from slopguard.errors import (
    ERR_BASELINE_FAILED,
    ERR_MUTATION_IN_PROGRESS,
    ERR_PROJECT_ROOT_MISSING,
    ERR_RUNNER_UNAVAILABLE,
    ERR_UNREADABLE_FILE,
    SlopguardError,
    runner_unavailable,
)
from slopguard.formatting import mutation_pretty_report
from slopguard.mutation.guard import JOURNAL, LOCK, ORIGINAL, WorkspaceGuard, guard_directory
from slopguard.mutation.models import MutantSite, PlannedMutant
from slopguard.mutation.pipeline import (
    NOTE_ALL_SURVIVED,
    NOTE_NO_COVERAGE,
    MutateOptions,
    MutationInterrupted,
    MutationPipeline,
    changed_file_note,
)
from slopguard.mutation.runner import CommandOutcome
from slopguard.progress import NORMAL, ProgressReporter

CALC = (
    "def smaller(a, b):\n"  # 1
    "    return a < b\n"  # 2
    "\n"  # 3
    "\n"  # 4
    "def flag():\n"  # 5
    "    return True  # slopguard-ignore-mutant\n"  # 6
    "\n"  # 7
    "\n"  # 8
    "def untested(x):\n"  # 9
    "    return -x\n"  # 10
)


_TEMP_DIRS = []


def temp_dir(prefix="slop-"):
    """A temp dir removed when this module's tests finish."""
    path = tempfile.mkdtemp(prefix=prefix)
    _TEMP_DIRS.append(path)
    return path


def tearDownModule():
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


def make_project(files):
    root = tempfile.mkdtemp(prefix="slop-mutproj-")
    write(os.path.join(root, "pyproject.toml"), "[project]\nname = 'x'\n")
    for rel, body in files.items():
        write(os.path.join(root, rel), body)
    return root


class FakeCommandRunner:
    """Answers the plain baseline, then decides each mutant run from the
    source on disk (the mutant is in place while it runs)."""

    def __init__(self, files, decide=None, baseline_exit=0, baseline_seconds=2.1, baseline_tail="1 failed"):
        self.files = files
        self.decide = decide or (lambda sources: 1)
        self.baseline_exit = baseline_exit
        self.baseline_seconds = baseline_seconds
        self.baseline_tail = baseline_tail
        self.calls = []
        self.killed = 0

    def run(self, argv, cwd, env, timeout, progress):
        self.calls.append({"argv": argv, "cwd": cwd, "env": env, "timeout": timeout})
        if len(self.calls) == 1:
            return CommandOutcome(self.baseline_exit, False, self.baseline_seconds, self.baseline_tail)
        sources = {}
        for path in self.files:
            with open(path, encoding="utf-8") as fh:
                sources[path] = fh.read()
        result = self.decide(sources)
        if isinstance(result, CommandOutcome):
            return result
        return CommandOutcome(result, False, 0.25, "")

    def kill_active(self):
        self.killed += 1


def coverage_json(project, path, executed, missing):
    """A ``coverage json`` report for one file. coverage.py records real
    paths (symlinks resolved), so the fake does too."""
    cov = os.path.join(temp_dir("slop-covjson-"), "coverage.json")
    with open(cov, "w") as fh:
        json.dump({"files": {os.path.realpath(path): {"executed_lines": executed, "missing_lines": missing}}}, fh)
    return cov


class PipelineFixture(unittest.TestCase):
    def setUp(self):
        self.project = make_project({"pkg/calc.py": CALC, "tests/test_calc.py": "import unittest\n"})
        self.calc = os.path.join(self.project, "pkg", "calc.py")
        # A private guard root, so the suite leaves no guard directories behind.
        self.guard_root = tempfile.mkdtemp(prefix="slop-guards-")
        self.addCleanup(shutil.rmtree, self.guard_root, True)
        self.addCleanup(shutil.rmtree, self.project, True)
        with open(self.calc, "rb") as fh:
            self.original = fh.read()
        self.mtime_ns = os.stat(self.calc).st_mtime_ns

    def options(self, **kwargs):
        kwargs.setdefault("source_path", os.path.join(self.project, "pkg"))
        kwargs.setdefault("project_dir", self.project)
        kwargs.setdefault("runner", "unittest")
        kwargs.setdefault("coverage", False)
        return MutateOptions(**kwargs)

    def pipeline(self, runner=None, run_tests_fn=None, probe=None):
        return MutationPipeline(
            command_runner=runner or FakeCommandRunner([self.calc]),
            run_tests_fn=run_tests_fn,
            guard_root=self.guard_root,
            pytest_cov_probe=probe or (lambda python, cwd: False),
        )

    def assert_restored(self):
        with open(self.calc, "rb") as fh:
            self.assertEqual(fh.read(), self.original)
        self.assertEqual(os.stat(self.calc).st_mtime_ns, self.mtime_ns)
        self.assertFalse(os.path.exists(guard_directory(self.project, self.guard_root)))


class DryRunTests(PipelineFixture):
    def test_dry_run_lists_and_touches_nothing(self):
        runner = FakeCommandRunner([self.calc])
        report = self.pipeline(runner).run(self.options(dry_run=True))
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(os.path.join(self.guard_root, "slopguard-mutate")))
        statuses = [(m["line"], m["operator"], m["status"]) for m in report["mutants"]]
        self.assertEqual(
            statuses,
            [
                (2, "boundary", "pending"),
                (2, "negate_conditional", "pending"),
                (6, "boolean_literal", "ignored"),
                (10, "invert_negative", "pending"),
            ],
        )
        self.assertIsNone(report["runner"])
        self.assertIsNone(report["projectRoot"])
        self.assertIsNone(report["timeoutSeconds"])
        self.assertFalse(report["coverageAvailable"])
        self.assertIsNone(report["summary"]["mutationScore"])
        self.assertEqual(report["summary"]["fileCount"], 1)
        self.assertEqual(report["sourceRoot"], os.path.join(self.project, "pkg"))
        self.assertEqual(report["mutants"][0]["id"], "calc.py:2:14:boundary")
        self.assertEqual(report["mutants"][0]["method"], "smaller")

    def test_nothing_left_to_run_skips_the_baseline(self):
        write(self.calc, "def flag():\n    return True  # slopguard-ignore-mutant\n")
        runner = FakeCommandRunner([self.calc])
        report = self.pipeline(runner).run(self.options())
        self.assertEqual(runner.calls, [])
        self.assertIsNone(report["runner"])
        self.assertEqual([m["status"] for m in report["mutants"]], ["ignored"])

    def test_no_mutants_at_all_skips_the_baseline(self):
        write(self.calc, "VALUE = 1\n")
        runner = FakeCommandRunner([self.calc])
        report = self.pipeline(runner).run(self.options())
        self.assertEqual(runner.calls, [])
        self.assertEqual(report["summary"]["mutantCount"], 0)
        self.assertEqual(report["summary"]["fileCount"], 1)

    def test_operator_filter_and_generated_files(self):
        write(os.path.join(self.project, "pkg", "gen.py"), "# @generated by a tool\nx = a + b\n")
        report = self.pipeline().run(self.options(dry_run=True, operators=["boundary", "arithmetic"]))
        self.assertEqual(report["operators"], ["arithmetic", "boundary"])
        self.assertEqual([m["operator"] for m in report["mutants"]], ["boundary"])
        self.assertEqual(report["summary"]["fileCount"], 2)

    def test_unreadable_file(self):
        if os.name != "posix" or os.getuid() == 0:
            self.skipTest("needs file modes that apply")
        os.chmod(self.calc, 0)
        try:
            with self.assertRaises(SlopguardError) as ctx:
                self.pipeline().run(self.options(dry_run=True))
            self.assertEqual(ctx.exception.code, ERR_UNREADABLE_FILE)
        finally:
            os.chmod(self.calc, 0o644)


class ExecutionTests(PipelineFixture):
    def test_statuses_timeout_and_progress(self):
        def decide(sources):
            text = sources[self.calc]
            if "a <= b" in text:
                return 0  # the boundary mutant survives
            if "return x" in text:
                return CommandOutcome(-9, True, 11.0, "")
            return 1

        runner = FakeCommandRunner([self.calc], decide=decide)
        err = io.StringIO()
        report = self.pipeline(runner).run(self.options(), ProgressReporter(err, NORMAL))
        self.assertEqual(
            [(m["operator"], m["status"]) for m in report["mutants"]],
            [("boundary", "survived"), ("negate_conditional", "killed"), ("boolean_literal", "ignored"), ("invert_negative", "timeout")],
        )
        self.assertEqual(report["runner"], "unittest")
        self.assertEqual(report["projectRoot"], self.project)
        self.assertEqual(report["timeoutSeconds"], 17)  # ceil(2.1 * 3) + 10
        self.assertEqual(report["summary"]["mutationScore"], 2 / 3 * 100)
        self.assertEqual(report["notes"], [])
        # The plain baseline runs the exact mutant command, with no timeout.
        self.assertEqual(runner.calls[0]["argv"], runner.calls[1]["argv"])
        self.assertEqual(runner.calls[0]["argv"][1:], ["-m", "unittest", "discover", "-f"])
        self.assertIsNone(runner.calls[0]["timeout"])
        self.assertEqual(runner.calls[1]["timeout"], 17)
        self.assertEqual(runner.calls[1]["env"]["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(runner.calls[1]["cwd"], self.project)
        lines = err.getvalue().splitlines()
        self.assertIn("slopguard: running baseline tests (unittest) in " + self.project, lines)
        self.assertIn("slopguard: baseline passed in 2.1s; timeout is 17s per mutant", lines)
        self.assertIn("slopguard: [1/4] survived      calc.py:2:14 boundary (0.2s)", lines)
        self.assertIn("slopguard: [3/4] ignored       calc.py:6:12 boolean_literal", lines)
        self.assertIn("slopguard: [4/4] timeout       calc.py:10:12 invert_negative (11.0s)", lines)
        self.assertEqual(
            lines[-1], "slopguard: done — 1 killed, 1 timeout, 1 survived, 0 no_coverage, 0 compile_error, 1 ignored"
        )
        self.assert_restored()

    def test_progress_index_is_right_aligned(self):
        write(self.calc, "".join(f"def f{i}():\n    return True\n\n\n" for i in range(10)))
        err = io.StringIO()
        self.pipeline().run(self.options(), ProgressReporter(err, NORMAL))
        self.assertIn("slopguard: [ 1/10] killed        calc.py:2:12 boolean_literal (0.2s)\n", err.getvalue())
        self.assertIn("slopguard: [10/10] killed", err.getvalue())

    def test_given_timeout_is_reported_as_given(self):
        runner = FakeCommandRunner([self.calc])
        report = self.pipeline(runner).run(self.options(timeout_seconds=2.5))
        self.assertEqual(report["timeoutSeconds"], 2.5)
        self.assertEqual(runner.calls[1]["timeout"], 2.5)

    def test_all_survived_note(self):
        report = self.pipeline(FakeCommandRunner([self.calc], decide=lambda s: 0)).run(self.options())
        self.assertEqual(report["notes"], [NOTE_ALL_SURVIVED])
        self.assertEqual(report["summary"]["mutationScore"], 0.0)

    def test_baseline_failure_is_fatal_and_touches_nothing(self):
        runner = FakeCommandRunner([self.calc], baseline_exit=1, baseline_tail="  FAILED test_x  \n")
        with self.assertRaises(SlopguardError) as ctx:
            self.pipeline(runner).run(self.options())
        self.assertEqual(ctx.exception.code, ERR_BASELINE_FAILED)
        self.assertEqual(
            ctx.exception.message,
            "The test suite fails without any mutation (exit 1). Fix the failing tests first: FAILED test_x",
        )
        self.assertEqual(len(runner.calls), 1)
        self.assert_restored()

    def test_baseline_failure_without_output(self):
        runner = FakeCommandRunner([self.calc], baseline_exit=5, baseline_tail="  ")
        with self.assertRaises(SlopguardError) as ctx:
            self.pipeline(runner).run(self.options())
        self.assertIn("(exit 5)", ctx.exception.message)
        self.assertIn("no output captured", ctx.exception.message)

    def test_pytest_runner_asks_for_no_cov_when_pytest_cov_is_there(self):
        runner = FakeCommandRunner([self.calc])
        probes = []

        def probe(python, cwd):
            probes.append(cwd)
            return True

        self.pipeline(runner, probe=probe).run(self.options(runner="pytest"))
        self.assertEqual(probes, [self.project])
        self.assertEqual(
            runner.calls[0]["argv"][1:], ["-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", "--no-cov"]
        )

    def test_detects_the_runner_and_the_project_root(self):
        runner = FakeCommandRunner([self.calc])
        err = io.StringIO()
        report = self.pipeline(runner).run(
            MutateOptions(source_path=os.path.join(self.project, "pkg"), coverage=False), ProgressReporter(err, NORMAL)
        )
        self.assertEqual(os.path.realpath(report["projectRoot"]), os.path.realpath(self.project))
        self.assertEqual(report["runner"], "unittest")
        self.assertIn("detecting test runner in", err.getvalue())

    def test_missing_project_root(self):
        original = pipeline_module.discover_project_root
        pipeline_module.discover_project_root = lambda path: ("", False)
        try:
            with self.assertRaises(SlopguardError) as ctx:
                self.pipeline().run(MutateOptions(source_path=self.calc, coverage=False))
        finally:
            pipeline_module.discover_project_root = original
        self.assertEqual(ctx.exception.code, ERR_PROJECT_ROOT_MISSING)
        self.assertIn("Pass --project-dir", ctx.exception.message)
        self.assertNotIn("--no-coverage", ctx.exception.message)

    def test_another_live_run_blocks_before_any_test_run(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            directory = guard_directory(self.project, self.guard_root)
            os.makedirs(directory)
            write(os.path.join(directory, LOCK), str(holder.pid))
            runner = FakeCommandRunner([self.calc])
            with self.assertRaises(SlopguardError) as ctx:
                self.pipeline(runner).run(self.options())
            self.assertEqual(ctx.exception.code, ERR_MUTATION_IN_PROGRESS)
            self.assertEqual(runner.calls, [])
        finally:
            holder.kill()
            holder.wait()

    def test_recovery_notes_reach_the_report(self):
        mutated = self.original.replace(b"a < b", b"a <= b")
        with open(self.calc, "wb") as fh:
            fh.write(mutated)
        directory = guard_directory(self.project, self.guard_root)
        os.makedirs(directory)
        dead = subprocess.Popen([sys.executable, "-c", "pass"])
        dead.wait()
        write(os.path.join(directory, LOCK), str(dead.pid))
        import hashlib

        write(os.path.join(directory, JOURNAL), json.dumps({"file": self.calc, "mutantSha256": hashlib.sha256(mutated).hexdigest()}))
        with open(os.path.join(directory, ORIGINAL), "wb") as fh:
            fh.write(self.original)
        report = self.pipeline().run(self.options())
        # The plan was read from the mutated file; after the recovery it is made
        # again from the restored one, so nothing looks "changed during the run".
        self.assertEqual(report["notes"], [f"Restored {self.calc}, which an interrupted mutate run left mutated."])
        self.assertEqual([m["original"] for m in report["mutants"]][:2], ["<", "<"])
        self.assertNotIn("pending", [m["status"] for m in report["mutants"]])
        with open(self.calc, "rb") as fh:
            self.assertEqual(fh.read(), self.original)


class ChangedFileTests(PipelineFixture):
    """Someone edits a source file while mutate runs: its remaining mutants
    are not run and the edit survives."""

    OTHER = "def neg(x):\n    return -x\n\n\ndef yes():\n    return True\n"

    def test_a_runner_hook_edits_the_next_file_between_two_mutant_runs(self):
        other = os.path.join(self.project, "pkg", "other.py")
        write(other, self.OTHER)
        edited = self.OTHER + "# edited while mutate ran\n"

        def decide(sources):
            if sources[other] == self.OTHER:  # the first mutant run of calc.py
                with open(other, "w", encoding="utf-8") as fh:
                    fh.write(edited)
            return 1

        runner = FakeCommandRunner([self.calc, other], decide=decide)
        report = self.pipeline(runner).run(self.options())
        self.assertEqual(
            [(m["file"], m["operator"], m["status"]) for m in report["mutants"]],
            [
                ("calc.py", "boundary", "killed"),
                ("calc.py", "negate_conditional", "killed"),
                ("calc.py", "boolean_literal", "ignored"),
                ("calc.py", "invert_negative", "killed"),
                ("other.py", "invert_negative", "pending"),
                ("other.py", "boolean_literal", "pending"),
            ],
        )
        self.assertEqual(report["notes"], [changed_file_note(other)])
        self.assertEqual(
            report["notes"][0], f"{other} changed while mutate was running, so its remaining mutants were not run."
        )
        self.assertEqual(report["summary"]["pending"], 2)
        self.assertEqual(report["summary"]["mutationScore"], 100.0)  # pending is not scored
        self.assertEqual(len(runner.calls), 4)  # the baseline and calc.py's three runs
        with open(other, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), edited)
        self.assert_restored()
        text = mutation_pretty_report(report)
        self.assertIn("  pending:        2\n", text)
        self.assertIn("Mutants (2, not run)\n  other.py:2:12  invert_negative  `-` → ``  neg\n", text)

    def test_an_edit_between_two_mutants_of_the_same_file(self):
        edited_text = CALC + "# edited while mutate ran\n"

        class EditAfterFirstRestore(WorkspaceGuard):
            edits = 0

            def restore(self):
                restored = super().restore()
                if restored is not None and EditAfterFirstRestore.edits == 0:
                    EditAfterFirstRestore.edits += 1
                    with open(restored, "w", encoding="utf-8") as fh:
                        fh.write(edited_text)
                return restored

        original = pipeline_module.WorkspaceGuard
        pipeline_module.WorkspaceGuard = EditAfterFirstRestore
        try:
            runner = FakeCommandRunner([self.calc])
            report = self.pipeline(runner).run(self.options())
        finally:
            pipeline_module.WorkspaceGuard = original
        self.assertEqual(
            [(m["operator"], m["status"]) for m in report["mutants"]],
            [("boundary", "killed"), ("negate_conditional", "pending"), ("boolean_literal", "ignored"), ("invert_negative", "pending")],
        )
        self.assertEqual(report["notes"], [changed_file_note(self.calc)])  # once per file
        self.assertEqual(len(runner.calls), 2)  # the baseline and the first mutant
        with open(self.calc, encoding="utf-8") as fh:
            self.assertEqual(fh.read(), edited_text)
        self.assertFalse(os.path.exists(guard_directory(self.project, self.guard_root)))


class ErrorPathTests(PipelineFixture):
    def test_restore_on_a_thrown_error(self):
        def decide(sources):
            raise RuntimeError("the runner blew up")

        with self.assertRaises(RuntimeError):
            self.pipeline(FakeCommandRunner([self.calc], decide=decide)).run(self.options())
        self.assert_restored()

    def test_runner_that_cannot_launch_restores_first(self):
        seen = []

        def decide(sources):
            seen.append(sources[self.calc] != self.original.decode())
            raise runner_unavailable("could not launch python: gone")

        with self.assertRaises(SlopguardError) as ctx:
            self.pipeline(FakeCommandRunner([self.calc], decide=decide)).run(self.options())
        self.assertEqual(ctx.exception.code, ERR_RUNNER_UNAVAILABLE)
        self.assertEqual(seen, [True])  # the mutant was in place during the run
        self.assert_restored()

    def test_compile_error_is_classified_without_a_test_run(self):
        def broken_plan(source, reported_path, operators):
            site = MutantSite(reported_path, 2, 14, "boundary", "<", "<(", source.text.index("<"), source.text.index("<") + 1)
            return [PlannedMutant(site=site, method="smaller", ignored=False)]

        original = pipeline_module.plan_file
        pipeline_module.plan_file = broken_plan
        try:
            runner = FakeCommandRunner([self.calc])
            report = self.pipeline(runner).run(self.options())
        finally:
            pipeline_module.plan_file = original
        self.assertEqual([m["status"] for m in report["mutants"]], ["compile_error"])
        self.assertEqual(len(runner.calls), 1)  # the baseline only
        self.assertIn("1 mutant(s) did not compile and are excluded from the score.", report["notes"])
        self.assertIsNone(report["summary"]["mutationScore"])
        self.assert_restored()


class CoverageBaselineTests(PipelineFixture):
    def run_with(self, outcome_or_error, **kwargs):
        calls = []

        def run_tests_fn(runner, project_root, coverage_dir, progress, python=None, raise_on_failure=True):
            calls.append((runner, project_root, python, raise_on_failure))
            if isinstance(outcome_or_error, BaseException):
                raise outcome_or_error
            return outcome_or_error

        err = io.StringIO()
        report = self.pipeline(run_tests_fn=run_tests_fn).run(self.options(coverage=True, **kwargs), ProgressReporter(err, NORMAL))
        return report, calls, err.getvalue()

    def test_uncovered_lines_are_no_coverage_and_unknown_lines_run(self):
        # Line 2 covered, line 10 missed; line 6 is ignored anyway.
        cov = coverage_json(self.project, self.calc, executed=[1, 2, 5, 6, 9], missing=[10])
        report, calls, _ = self.run_with(TestOutcome(cov, True, 0, ""))
        self.assertEqual(calls, [("unittest", self.project, sys.executable, False)])
        self.assertTrue(report["coverageAvailable"])
        self.assertEqual(
            [(m["line"], m["status"]) for m in report["mutants"]],
            [(2, "killed"), (2, "killed"), (6, "ignored"), (10, "no_coverage")],
        )
        self.assertEqual(report["summary"]["mutationScore"], 2 / 3 * 100)
        self.assertEqual(report["notes"], [])

    def test_relative_report_paths_under_a_symlinked_root(self):
        # coverage.py writes files under the project as relative paths, which
        # the index joins to the root as given; the temp dir may be a symlink
        # (/var -> /private/var on macOS). The lookup compares real paths.
        link = os.path.join(temp_dir("slop-covlink-"), "proj")
        os.symlink(self.project, link)
        cov = os.path.join(temp_dir(), "coverage.json")
        write(cov, json.dumps({"files": {"pkg/calc.py": {"executed_lines": [1, 2, 5, 6, 9], "missing_lines": [10]}}}))
        report, _, _ = self.run_with(TestOutcome(cov, True, 0, ""), project_dir=link)
        self.assertEqual(report["mutants"][-1]["status"], "no_coverage")

    def test_another_file_with_the_same_name_is_not_used(self):
        # The analyze index falls back to a basename match for CI path drift;
        # mutate only trusts the file's own entry, so these mutants all run.
        other = os.path.join(self.project, "vendored", "calc.py")
        write(other, CALC)
        cov = coverage_json(self.project, other, executed=[], missing=[1, 2, 5, 6, 9, 10])
        report, _, _ = self.run_with(TestOutcome(cov, True, 0, ""))
        self.assertTrue(report["coverageAvailable"])
        self.assertNotIn("no_coverage", [m["status"] for m in report["mutants"]])

    def test_failing_coverage_run_with_data_adds_a_note(self):
        cov = coverage_json(self.project, self.calc, executed=[2, 10], missing=[])
        report, _, _ = self.run_with(TestOutcome(cov, False, 3, "coverage threshold"))
        self.assertTrue(report["coverageAvailable"])
        self.assertEqual(report["notes"], ["The coverage run exited with code 3; its coverage data was still used."])

    def test_no_coverage_data_runs_every_mutant(self):
        report, _, _ = self.run_with(TestOutcome(None, False, 1, "boom"))
        self.assertFalse(report["coverageAvailable"])
        self.assertEqual(report["notes"], [NOTE_NO_COVERAGE])
        self.assertNotIn("no_coverage", [m["status"] for m in report["mutants"]])

    def test_empty_coverage_data_runs_every_mutant(self):
        cov = os.path.join(temp_dir(), "coverage.json")
        write(cov, json.dumps({"files": {}}))
        report, _, _ = self.run_with(TestOutcome(cov, True, 0, ""))
        self.assertEqual(report["notes"], [NOTE_NO_COVERAGE])

    def test_undecodable_coverage_data_runs_every_mutant(self):
        cov = os.path.join(temp_dir(), "coverage.json")
        write(cov, "{not json")
        report, _, err = self.run_with(TestOutcome(cov, True, 0, ""))
        self.assertEqual(report["notes"], [NOTE_NO_COVERAGE])
        self.assertIn("slopguard: no coverage data: Failed to decode coverage data", err)

    def test_missing_coverage_tool_runs_every_mutant(self):
        report, _, err = self.run_with(runner_unavailable("coverage.py is not installed"))
        self.assertFalse(report["coverageAvailable"])
        self.assertEqual(report["notes"], [NOTE_NO_COVERAGE])
        self.assertIn("coverage.py is not installed", err)

    def test_any_coverage_run_error_only_costs_the_shortcut(self):
        report, _, err = self.run_with(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte"))
        self.assertEqual(report["notes"], [NOTE_NO_COVERAGE])
        self.assertIn("slopguard: no coverage data: 'utf-8' codec can't decode", err)
        self.assert_restored()

    def test_an_interrupt_during_the_coverage_run_still_stops_the_run(self):
        with self.assertRaises(MutationInterrupted):
            self.run_with(MutationInterrupted("SIGINT", 130, None))
        self.assert_restored()

    def test_no_coverage_flag_skips_the_coverage_run(self):
        calls = []
        report = self.pipeline(run_tests_fn=lambda *a, **k: calls.append(a)).run(self.options(coverage=False))
        self.assertEqual(calls, [])
        self.assertFalse(report["coverageAvailable"])
        self.assertEqual(report["notes"], [])


@unittest.skipUnless(hasattr(signal, "SIGTERM") and os.name == "posix", "POSIX signals")
class SignalTests(PipelineFixture):
    """The handler runs in this (main) thread, raised from a real signal."""

    def interrupt_during_a_mutant(self, signame):
        signum = getattr(signal, signame)
        seen = []

        def decide(sources):
            seen.append(sources[self.calc])
            os.kill(os.getpid(), signum)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                time.sleep(0.01)  # the handler raises in here
            return 1

        runner = FakeCommandRunner([self.calc], decide=decide)
        err = io.StringIO()
        # A harmless fallback in case the pipeline failed to install its own.
        def fallback(*args):
            return None

        previous = signal.signal(signum, fallback)
        try:
            with self.assertRaises(MutationInterrupted) as ctx:
                self.pipeline(runner).run(self.options(), ProgressReporter(err, NORMAL))
        finally:
            after_run = signal.signal(signum, previous)
        self.assertIs(after_run, fallback)  # the pipeline put the previous handler back
        self.assertNotEqual(seen[0], self.original.decode())
        self.assertGreaterEqual(runner.killed, 1)
        self.assert_restored()
        self.assertIn(f"slopguard: interrupted — restored {self.calc}\n", err.getvalue())
        self.assertEqual(ctx.exception.restored, self.calc)
        return ctx.exception

    def test_sigterm(self):
        exc = self.interrupt_during_a_mutant("SIGTERM")
        self.assertEqual((exc.signal_name, exc.exit_code), ("SIGTERM", 143))

    def test_sigint(self):
        exc = self.interrupt_during_a_mutant("SIGINT")
        self.assertEqual(exc.exit_code, 130)

    @unittest.skipUnless(hasattr(signal, "SIGHUP"), "no SIGHUP here")
    def test_sighup(self):
        exc = self.interrupt_during_a_mutant("SIGHUP")
        self.assertEqual(exc.exit_code, 129)

    def test_interrupt_between_mutants_restores_nothing(self):
        class InterruptingRunner(FakeCommandRunner):
            def run(self, argv, cwd, env, timeout, progress):
                outcome = super().run(argv, cwd, env, timeout, progress)
                if len(self.calls) == 1:  # right after the baseline
                    os.kill(os.getpid(), signal.SIGTERM)
                    deadline = time.monotonic() + 30
                    while time.monotonic() < deadline:
                        time.sleep(0.01)
                return outcome

        err = io.StringIO()
        previous = signal.signal(signal.SIGTERM, lambda *a: None)
        try:
            with self.assertRaises(MutationInterrupted) as ctx:
                self.pipeline(InterruptingRunner([self.calc])).run(self.options(), ProgressReporter(err, NORMAL))
        finally:
            signal.signal(signal.SIGTERM, previous)
        self.assertIsNone(ctx.exception.restored)
        self.assertIn("slopguard: interrupted\n", err.getvalue())
        self.assert_restored()

    def test_a_second_signal_while_stopping_is_ignored(self):
        def decide(sources):
            handler = signal.getsignal(signal.SIGTERM)
            try:
                handler(signal.SIGTERM, None)
            except MutationInterrupted:
                self.assertIsNone(handler(signal.SIGINT, None))
                raise
            return 1  # pragma: no cover — the handler always raises

        with self.assertRaises(MutationInterrupted) as ctx:
            self.pipeline(FakeCommandRunner([self.calc], decide=decide)).run(self.options())
        self.assertEqual(ctx.exception.exit_code, 143)
        self.assert_restored()

    def test_restore_failure_inside_the_handler_is_reported(self):
        import stat

        from slopguard.errors import ERR_RESTORE_FAILED
        from slopguard.mutation.guard import WorkspaceGuard

        def decide(sources):
            os.chmod(self.calc, stat.S_IRUSR)
            if os.access(self.calc, os.W_OK):
                self.skipTest("running with privileges that ignore file modes")
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            return 1  # pragma: no cover — the handler always raises

        err = io.StringIO()
        try:
            with self.assertRaises(SlopguardError) as ctx:
                self.pipeline(FakeCommandRunner([self.calc], decide=decide)).run(self.options(), ProgressReporter(err, NORMAL))
            self.assertEqual(ctx.exception.code, ERR_RESTORE_FAILED)
            self.assertIn("slopguard: Could not restore", err.getvalue())
            self.assertIn("slopguard: interrupted\n", err.getvalue())
        finally:
            os.chmod(self.calc, stat.S_IRUSR | stat.S_IWUSR)
        # The guard kept its files, so the next run puts the original back.
        guard = WorkspaceGuard.acquire(self.project, tmp_root=self.guard_root)
        self.assertEqual(guard.notes, [f"Restored {self.calc}, which an interrupted mutate run left mutated."])
        guard.release()
        with open(self.calc, "rb") as fh:
            self.assertEqual(fh.read(), self.original)

    def test_handlers_are_uninstalled_after_a_run(self):
        before = signal.getsignal(signal.SIGTERM)
        self.pipeline().run(self.options())
        self.assertIs(signal.getsignal(signal.SIGTERM), before)

    def test_no_handlers_off_the_main_thread(self):
        import threading

        results = []

        def work():
            results.append(self.pipeline().run(self.options()))

        worker = threading.Thread(target=work)
        worker.start()
        worker.join(120)
        self.assertEqual(len(results), 1)
        self.assert_restored()


class DefaultAnalysisOptionsTests(unittest.TestCase):
    def test_mutate_options_default_to_the_analyze_excludes(self):
        options = MutateOptions(source_path=".")
        self.assertEqual(options.analysis.exclude_globs, default_analysis_options().exclude_globs)
        self.assertTrue(options.coverage)
        self.assertFalse(options.dry_run)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
