"""Targeted branch coverage for the remaining defensive/edge paths.

Keeps the coverage floor (95%) comfortably cleared across Python versions —
coverage.py's measurement core differs between the legacy ``settrace`` (≤3.11)
and ``sys.monitoring`` (3.12+) backends, so a thin margin is fragile.
"""

import contextlib
import io
import json
import os
import sys
import tempfile
import unittest

from slopguard import errors
from slopguard.aggregator import aggregate
from slopguard.cli import _expand_tilde
from slopguard.cli import run as cli_run
import slopguard.cli as climod
from slopguard.complexity import analyze_source
from slopguard.coverage import MODE_PREBUILT, CoverageSource
from slopguard.coverage import run as run_pipeline
from slopguard.coverage.detection import _file_contains, detect_runner
from slopguard.coverage.index import CoverageIndex
from slopguard.coverage.report import parse_coverage_json
from slopguard.diranalyzer import AnalysisOptions, _enumerate, _relativize, analyze_tree
from slopguard.diranalyzer import default_analysis_options
from slopguard.errors import SlopguardError
from slopguard.formatting import pretty_report
from slopguard.models import KIND_FUNCTION, FileReport, MethodMetric


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


class _Prov:
    def __init__(self, pct):
        self._pct = pct

    def method_coverage(self, *a):
        return self._pct

    def file_coverage(self, *a):
        return self._pct


class EntryPointTests(unittest.TestCase):
    def test_main_runs_and_exits_zero(self):
        import slopguard.__main__ as m

        old = sys.argv
        sys.argv = ["slopguard-python", "version"]
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    m.main()
        finally:
            sys.argv = old
        self.assertEqual(ctx.exception.code, 0)


class CliBranchTests(unittest.TestCase):
    def _proj(self):
        d = tempfile.mkdtemp(prefix="slop-clib-")
        write(os.path.join(d, "x.py"), "def f(a):\n    return 1 if a else 0\n")
        return d

    def test_no_default_excludes_flag(self):
        d = self._proj()
        out, err = io.StringIO(), io.StringIO()
        code = cli_run(["analyze", "--path", d, "--no-coverage", "--no-default-excludes", "--json"], out, err)
        self.assertEqual(code, 0)

    def test_auto_mode_branch(self):
        out, err = io.StringIO(), io.StringIO()
        orig = climod.run_pipeline
        climod.run_pipeline = lambda **k: aggregate([], source_root="/x", threshold=30.0)
        try:
            code = cli_run(["analyze", "--path", ".", "--quiet"], out, err)
        finally:
            climod.run_pipeline = orig
        self.assertEqual(code, 0)

    def test_expand_tilde(self):
        self.assertEqual(_expand_tilde("~"), os.path.expanduser("~"))
        self.assertTrue(_expand_tilde("~/foo").endswith(os.sep + "foo"))
        self.assertEqual(_expand_tilde("/abs"), "/abs")


class PipelineBranchTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="slop-pipeb-")
        write(os.path.join(self.d, "pyproject.toml"), "[project]\nname='x'\n")
        write(os.path.join(self.d, "calc.py"), "def add(a, b):\n    return a + b\n")

    def test_prebuilt_empty_files_note_and_discovery(self):
        cov = os.path.join(self.d, "cov.json")
        write(cov, json.dumps({"files": {}}))
        # No project_dir -> _project_context discovers the root from source_path.
        report = run_pipeline(
            self.d,
            CoverageSource(mode=MODE_PREBUILT, coverage_file=cov),
            threshold=30.0,
            options=default_analysis_options(),
        )
        self.assertTrue(any("no per-file coverage data" in n for n in report["notes"]))

    def test_prebuilt_missing_file_raises(self):
        with self.assertRaises(SlopguardError) as ctx:
            run_pipeline(
                self.d,
                CoverageSource(mode=MODE_PREBUILT, coverage_file="/no/such/cov.json", project_dir=self.d),
                threshold=30.0,
                options=default_analysis_options(),
            )
        self.assertEqual(ctx.exception.code, errors.ERR_UNREADABLE_FILE)

    def test_runner_exception_cleans_up_and_reraises(self):
        def boom(runner, root, cov_dir, progress):
            raise SlopguardError(errors.ERR_TEST_RUN_FAILED, "boom")

        with self.assertRaises(SlopguardError) as ctx:
            run_pipeline(
                self.d,
                CoverageSource(),
                threshold=30.0,
                options=default_analysis_options(),
                run_tests_fn=boom,
            )
        self.assertEqual(ctx.exception.code, errors.ERR_TEST_RUN_FAILED)


class ReportBranchTests(unittest.TestCase):
    def test_top_level_not_object(self):
        with self.assertRaises(SlopguardError) as ctx:
            parse_coverage_json("[1, 2, 3]")
        self.assertEqual(ctx.exception.code, errors.ERR_COVERAGE_DECODE)

    def test_file_data_not_dict_skipped(self):
        parsed = parse_coverage_json(json.dumps({"files": {"a.py": "nope", "b.py": {"executed_lines": [1], "missing_lines": []}}}))
        self.assertNotIn("a.py", parsed)
        self.assertIn("b.py", parsed)

    def test_int_list_tolerates_missing_keys(self):
        parsed = parse_coverage_json(json.dumps({"files": {"a.py": {}}}))
        self.assertEqual(parsed["a.py"].executed_lines, [])
        self.assertEqual(parsed["a.py"].missing_lines, [])


class DirAnalyzerBranchTests(unittest.TestCase):
    def test_enumerate_on_non_directory_raises(self):
        f = os.path.join(tempfile.mkdtemp(), "file.py")
        write(f, "x = 1\n")
        with self.assertRaises(SlopguardError) as ctx:
            _enumerate(f, default_analysis_options())
        self.assertEqual(ctx.exception.code, errors.ERR_UNREADABLE_FILE)

    def test_symlink_not_followed(self):
        d = tempfile.mkdtemp(prefix="slop-link-")
        write(os.path.join(d, "real.py"), "def f():\n    return 1\n")
        try:
            os.symlink(os.path.join(d, "real.py"), os.path.join(d, "link.py"))
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable")
        paths = sorted(r.path for r in analyze_tree(d, AnalysisOptions()))
        self.assertIn("real.py", paths)
        self.assertNotIn("link.py", paths)

    def test_include_filter_excludes_nonmatch(self):
        d = tempfile.mkdtemp(prefix="slop-inc-")
        write(os.path.join(d, "app.py"), "def a():\n    return 1\n")
        write(os.path.join(d, "other.py"), "def b():\n    return 1\n")
        opts = AnalysisOptions(include_globs=["**/app.py"])  # no default excludes
        paths = sorted(r.path for r in analyze_tree(d, opts))
        self.assertEqual(paths, ["app.py"])

    def test_relativize_root_equals_path(self):
        self.assertEqual(_relativize("/a/b", "/a/b"), "b")


class DetectionBranchTests(unittest.TestCase):
    def test_conftest_signals_pytest(self):
        d = tempfile.mkdtemp()
        write(os.path.join(d, "conftest.py"), "import pytest\n")
        self.assertEqual(detect_runner(d), "pytest")

    def test_pyproject_tool_pytest_section(self):
        d = tempfile.mkdtemp()
        write(os.path.join(d, "pyproject.toml"), "[tool.pytest.ini_options]\naddopts = '-q'\n")
        self.assertEqual(detect_runner(d), "pytest")

    def test_file_contains_handles_non_utf8(self):
        path = os.path.join(tempfile.mkdtemp(), "setup.cfg")
        with open(path, "wb") as fh:
            fh.write(b"\xff\xfe[tool:pytest]")
        self.assertFalse(_file_contains(path, "pytest"))


class IndexBranchTests(unittest.TestCase):
    def test_file_coverage_unknown_file(self):
        idx = CoverageIndex(parse_coverage_json('{"files": {}}'), "/p")
        self.assertIsNone(idx.file_coverage("/p/unknown.py"))

    def test_file_coverage_no_executable_lines(self):
        cov = parse_coverage_json(json.dumps({"files": {"/p/a.py": {"executed_lines": [], "missing_lines": []}}}))
        idx = CoverageIndex(cov, "/p")
        self.assertIsNone(idx.file_coverage("/p/a.py"))


class AggregatorBranchTests(unittest.TestCase):
    def test_coverage_for_both_none_reads_zero(self):
        class NoneProv:
            def method_coverage(self, *a):
                return None

            def file_coverage(self, *a):
                return None

        fr = analyze_source("def f(a):\n    if a:\n        return 1\n    return 0\n", "m.py")
        out = aggregate([fr], source_root="/p", threshold=30.0, coverage=NoneProv())
        self.assertTrue(out["coverageAvailable"])
        self.assertEqual(out["methods"][0]["coverage"], 0.0)

    def test_empty_class_weighted_coverage(self):
        fr = analyze_source("class Empty:\n    pass\n", "m.py")
        out = aggregate([fr], source_root="/p", threshold=30.0)
        empty = [t for t in out["types"] if t["name"] == "Empty"]
        self.assertTrue(empty)
        self.assertEqual(empty[0]["methodCount"], 0)
        self.assertEqual(empty[0]["weightedCoverage"], 0.0)

    def test_absolute_file_path(self):
        m = MethodMetric(
            name="f", qualified_name="f", type_name=None, kind=KIND_FUNCTION,
            file="/abs/m.py", start_line=1, end_line=2,
            complexity=1, cognitive_complexity=0, weighted_complexity=0.0,
        )
        out = aggregate([FileReport(path="/abs/m.py", methods=[m])], source_root="/root", threshold=30.0)
        self.assertEqual(out["summary"]["methodCount"], 1)


class FormattingBranchTests(unittest.TestCase):
    def _covered_report(self, data_path):
        fr = analyze_source("def f(a):\n    if a:\n        return 1\n    return 0\n", "m.py")
        return aggregate([fr], source_root="/p", threshold=30.0, coverage=_Prov(80.0), coverage_data_path=data_path)

    def test_header_shows_coverage_data_path(self):
        text = pretty_report(self._covered_report("/p/cov.json"), 20)
        self.assertIn("/p/cov.json", text)
        self.assertIn("coverage:  80.0%", text)

    def test_header_shows_ephemeral_coverage(self):
        text = pretty_report(self._covered_report(None), 20)
        self.assertIn("generated by the coverage run", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
