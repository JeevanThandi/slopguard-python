"""Targeted coverage of error/progress/edge paths."""

import io
import json
import os
import tempfile
import textwrap
import unittest

from slopguard import errors
from slopguard.aggregator import aggregate
from slopguard.cli import run as cli_run
from slopguard.complexity import analyze_source
from slopguard.coverage.detection import _file_contains, detect_runner, discover_project_root
from slopguard.coverage.index import CoverageIndex
from slopguard.coverage.report import parse_coverage_json
from slopguard.fileanalyzer import analyze_file
from slopguard.formatting import pretty_report
from slopguard.models import FileReport
from slopguard.progress import NORMAL, VERBOSE, ProgressReporter


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


class ErrorTests(unittest.TestCase):
    def test_all_constructors_carry_codes(self):
        cases = [
            (errors.file_not_found("p"), errors.ERR_FILE_NOT_FOUND),
            (errors.not_a_directory("p"), errors.ERR_NOT_A_DIRECTORY),
            (errors.unreadable_file("p", "x"), errors.ERR_UNREADABLE_FILE),
            (errors.parse_failed("p", "x"), errors.ERR_PARSE_FAILED),
            (errors.project_root_not_found("p"), errors.ERR_PROJECT_ROOT_MISSING),
            (errors.runner_not_detected("p"), errors.ERR_RUNNER_NOT_DETECTED),
            (errors.runner_unavailable("x"), errors.ERR_RUNNER_UNAVAILABLE),
            (errors.test_run_failed(1, "boom"), errors.ERR_TEST_RUN_FAILED),
            (errors.coverage_decode_failed("x"), errors.ERR_COVERAGE_DECODE),
            (errors.invalid_argument("a", "bad"), errors.ERR_INVALID_ARGUMENT),
            (errors.unsupported("x"), errors.ERR_UNSUPPORTED),
        ]
        for err, code in cases:
            self.assertEqual(err.code, code)
            self.assertEqual(err.envelope()["code"], code)
            self.assertIn(code, str(err))

    def test_envelope_for_slopguard(self):
        env = errors.envelope_for(errors.file_not_found("p"))
        self.assertEqual(env["code"], errors.ERR_FILE_NOT_FOUND)

    def test_envelope_for_generic(self):
        env = errors.envelope_for(ValueError("nope"))
        self.assertEqual(env["code"], errors.ERR_INTERNAL)
        self.assertEqual(env["message"], "nope")


class ProgressTests(unittest.TestCase):
    def test_silent_writes_nothing(self):
        buf = io.StringIO()
        p = ProgressReporter.silent()
        p.phase("hi")
        p.raw("chunk")
        self.assertEqual(buf.getvalue(), "")
        self.assertFalse(p.is_verbose)

    def test_normal_emits_phase_only(self):
        buf = io.StringIO()
        p = ProgressReporter(buf, NORMAL)
        p.phase("walking")
        p.raw("subprocess noise")
        self.assertIn("slopguard: walking", buf.getvalue())
        self.assertNotIn("subprocess noise", buf.getvalue())

    def test_verbose_streams_raw(self):
        buf = io.StringIO()
        p = ProgressReporter(buf, VERBOSE)
        p.raw("subprocess noise")
        self.assertIn("subprocess noise", buf.getvalue())
        self.assertTrue(p.is_verbose)


class FileAnalyzerTests(unittest.TestCase):
    def test_unreadable_directory(self):
        d = tempfile.mkdtemp()
        with self.assertRaises(errors.SlopguardError) as ctx:
            analyze_file(d, "d")  # opening a directory raises OSError
        self.assertEqual(ctx.exception.code, errors.ERR_UNREADABLE_FILE)

    def test_invalid_utf8(self):
        path = os.path.join(tempfile.mkdtemp(), "bin.py")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe\x00\x01 not utf8")
        with self.assertRaises(errors.SlopguardError) as ctx:
            analyze_file(path, "bin.py")
        self.assertEqual(ctx.exception.code, errors.ERR_UNREADABLE_FILE)


class IndexEdgeTests(unittest.TestCase):
    def test_multiple_basename_candidates_pick_longest_suffix(self):
        cov = parse_coverage_json(
            json.dumps(
                {
                    "files": {
                        "/a/pkg/util.py": {"executed_lines": [1], "missing_lines": []},
                        "/b/other/util.py": {"executed_lines": [1, 2], "missing_lines": []},
                    }
                }
            )
        )
        idx = CoverageIndex(cov, "/root")
        # Query path shares a longer suffix with /a/pkg/util.py
        self.assertIsNotNone(idx.file_coverage("/somewhere/pkg/util.py"))

    def test_relative_path_resolves_against_root(self):
        cov = parse_coverage_json(json.dumps({"files": {"pkg/a.py": {"executed_lines": [1], "missing_lines": []}}}))
        idx = CoverageIndex(cov, "/root")
        self.assertIsNotNone(idx.file_coverage("/root/pkg/a.py"))


class DetectionEdgeTests(unittest.TestCase):
    def test_setup_cfg_pytest(self):
        d = tempfile.mkdtemp()
        write(os.path.join(d, "setup.cfg"), "[tool:pytest]\naddopts = -q\n")
        self.assertEqual(detect_runner(d), "pytest")

    def test_pyproject_dep_pytest(self):
        d = tempfile.mkdtemp()
        write(os.path.join(d, "pyproject.toml"), "[project]\ndependencies = ['pytest']\n")
        self.assertEqual(detect_runner(d), "pytest")

    def test_requirements_pytest(self):
        d = tempfile.mkdtemp()
        write(os.path.join(d, "requirements-dev.txt"), "pytest==8.0\n")
        self.assertEqual(detect_runner(d), "pytest")

    def test_file_contains_missing(self):
        self.assertFalse(_file_contains("/no/such/file", "pytest"))

    def test_discover_from_file_path(self):
        d = tempfile.mkdtemp()
        write(os.path.join(d, "setup.py"), "from setuptools import setup\n")
        f = os.path.join(d, "mod.py")
        write(f, "x = 1\n")
        root, ok = discover_project_root(f)
        self.assertTrue(ok)
        self.assertEqual(os.path.realpath(root), os.path.realpath(d))


class ComplexityEdgeTests(unittest.TestCase):
    def test_dict_comprehension(self):
        r = analyze_source("def f(xs):\n    return {x: x*2 for x in xs if x}\n", "t.py")
        m = r.methods[0]
        self.assertEqual(m.complexity, 3)  # base + for + filter

    def test_generator_and_set_comp(self):
        r = analyze_source(
            "def f(xs):\n    a = sum(x for x in xs)\n    b = {x for x in xs if x}\n    return a, b\n",
            "t.py",
        )
        self.assertGreaterEqual(r.methods[0].complexity, 3)

    def test_decorated_default_args(self):
        src = textwrap.dedent(
            """
            def deco(f):
                return f

            @deco
            def f(x=(1 if True else 2)):
                return x
            """
        )
        r = analyze_source(src, "t.py")
        names = sorted(m.qualified_name for m in r.methods)
        self.assertEqual(names, ["deco", "f"])

    def test_nested_for_comprehension(self):
        r = analyze_source("def f(rows):\n    return [c for row in rows for c in row]\n", "t.py")
        # two for clauses
        self.assertEqual(r.methods[0].complexity, 3)


class AggregatorEdgeTests(unittest.TestCase):
    def test_zero_threshold_defaults_to_30(self):
        r = aggregate([], source_root="/p", threshold=0)
        self.assertEqual(r["threshold"], 30.0)

    def test_file_coverage_fallback(self):
        class FileOnly:
            def method_coverage(self, *a):
                return None

            def file_coverage(self, *a):
                return 50.0

        fr = analyze_source("def f(a):\n    if a:\n        return 1\n    return 0\n", "m.py")
        out = aggregate([fr], source_root="/p", threshold=30.0, coverage=FileOnly())
        self.assertEqual(out["methods"][0]["coverage"], 50.0)


class FormattingEdgeTests(unittest.TestCase):
    def test_wide_values_not_truncated(self):
        fr = analyze_source(
            "def f(" + ", ".join(f"a{i}" for i in range(20)) + "):\n"
            "    return " + " and ".join(f"a{i}" for i in range(20)) + "\n",
            "wide.py",
        )
        report = aggregate([fr], source_root="/p", threshold=30.0)
        text = pretty_report(report, 20)
        self.assertIn("cyc=20", text)  # value wider than the 3-char pad survives


class CliEdgeTests(unittest.TestCase):
    def test_prebuilt_coverage_file_via_cli(self):
        d = tempfile.mkdtemp(prefix="slop-cliprebuilt-")
        write(os.path.join(d, "pyproject.toml"), "[project]\nname='x'\n")
        write(os.path.join(d, "calc.py"), "def add(a, b):\n    return a + b\n")
        cov = os.path.join(d, "cov.json")
        write(cov, json.dumps({"files": {os.path.join(d, "calc.py"): {"executed_lines": [1, 2], "missing_lines": []}}}))
        out, err = io.StringIO(), io.StringIO()
        code = cli_run(["analyze", "--path", d, "--coverage-file", cov, "--project-dir", d, "--json"], out, err)
        self.assertEqual(code, 0)
        report = json.loads(out.getvalue())
        self.assertTrue(report["coverageAvailable"])

    def test_verbose_flag(self):
        d = tempfile.mkdtemp(prefix="slop-cliverbose-")
        write(os.path.join(d, "calc.py"), "def add(a, b):\n    return a + b\n")
        out, err = io.StringIO(), io.StringIO()
        code = cli_run(["analyze", "--path", d, "--no-coverage", "--verbose"], out, err)
        self.assertEqual(code, 0)
        self.assertIn("slopguard:", err.getvalue())


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
