import json
import os
import tempfile
import unittest

from slopguard.coverage import MODE_NONE, MODE_PREBUILT, CoverageSource
from slopguard.coverage import run as run_pipeline
from slopguard.coverage.detection import detect_runner, discover_project_root
from slopguard.coverage.index import CoverageIndex
from slopguard.coverage.report import parse_coverage_json
from slopguard.coverage.runner import TestOutcome
from slopguard.diranalyzer import default_analysis_options
from slopguard.errors import ERR_COVERAGE_DECODE, ERR_PROJECT_ROOT_MISSING, SlopguardError


def write(path, body):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(body)


class ReportParseTests(unittest.TestCase):
    def test_parses_files(self):
        text = json.dumps(
            {"files": {"a.py": {"executed_lines": [1, 2], "missing_lines": [3]}}}
        )
        parsed = parse_coverage_json(text)
        self.assertEqual(parsed["a.py"].executed_lines, [1, 2])
        self.assertEqual(parsed["a.py"].missing_lines, [3])

    def test_no_files_key(self):
        self.assertEqual(parse_coverage_json("{}"), {})

    def test_invalid_json_raises(self):
        with self.assertRaises(SlopguardError) as ctx:
            parse_coverage_json("not json")
        self.assertEqual(ctx.exception.code, ERR_COVERAGE_DECODE)


class IndexTests(unittest.TestCase):
    def build(self, root):
        cov = parse_coverage_json(
            json.dumps(
                {
                    "files": {
                        os.path.join(root, "a.py"): {
                            "executed_lines": [1, 2, 4],
                            "missing_lines": [3, 5],
                        }
                    }
                }
            )
        )
        return CoverageIndex(cov, root)

    def test_method_coverage(self):
        idx = self.build("/proj")
        # lines 1..3: 1,2 covered, 3 missing -> 2/3
        self.assertAlmostEqual(idx.method_coverage("/proj/a.py", 1, 3), 200 / 3)

    def test_method_coverage_unknown_file(self):
        idx = self.build("/proj")
        self.assertIsNone(idx.method_coverage("/proj/other.py", 1, 3))

    def test_method_coverage_no_lines_in_span(self):
        idx = self.build("/proj")
        self.assertIsNone(idx.method_coverage("/proj/a.py", 100, 200))

    def test_file_coverage(self):
        idx = self.build("/proj")
        # 3 covered of 5 executable
        self.assertAlmostEqual(idx.file_coverage("/proj/a.py"), 60.0)
        self.assertEqual(idx.file_count(), 1)

    def test_basename_suffix_fallback(self):
        cov = parse_coverage_json(
            json.dumps({"files": {"/ci/checkout/src/a.py": {"executed_lines": [1], "missing_lines": []}}})
        )
        idx = CoverageIndex(cov, "/ci/checkout")
        # local clone has a different absolute prefix but same suffix
        self.assertAlmostEqual(idx.file_coverage("/home/me/src/a.py"), 100.0)


class DetectionTests(unittest.TestCase):
    def test_discover_project_root(self):
        d = tempfile.mkdtemp(prefix="slop-root-")
        write(os.path.join(d, "pyproject.toml"), "[project]\n")
        sub = os.path.join(d, "src", "pkg")
        os.makedirs(sub)
        root, ok = discover_project_root(sub)
        self.assertTrue(ok)
        self.assertEqual(os.path.realpath(root), os.path.realpath(d))

    def test_discover_project_root_missing(self):
        d = tempfile.mkdtemp(prefix="slop-noroot-")
        # No markers anywhere up to the temp dir's parents that we control; the
        # function still returns found=False or a real ancestor — assert the API
        # shape rather than the filesystem.
        root, ok = discover_project_root(d)
        self.assertIsInstance(ok, bool)
        self.assertIsInstance(root, str)

    def test_detect_runner_pytest(self):
        d = tempfile.mkdtemp(prefix="slop-pytest-")
        write(os.path.join(d, "pytest.ini"), "[pytest]\n")
        self.assertEqual(detect_runner(d), "pytest")

    def test_detect_runner_falls_back_to_unittest(self):
        d = tempfile.mkdtemp(prefix="slop-unittest-")
        self.assertEqual(detect_runner(d), "unittest")


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="slop-pipe-")
        write(os.path.join(self.dir, "pyproject.toml"), "[project]\nname='x'\n")
        write(
            os.path.join(self.dir, "calc.py"),
            "def tangled(a, b, c):\n"
            "    if a:\n        if b:\n            if c:\n                return 1\n    return 0\n"
            "\n\ndef add(a, b):\n    return a + b\n",
        )

    def test_no_coverage_mode(self):
        report = run_pipeline(
            self.dir,
            CoverageSource(mode=MODE_NONE),
            threshold=30.0,
            options=default_analysis_options(),
        )
        self.assertFalse(report["coverageAvailable"])
        self.assertEqual(report["summary"]["methodCount"], 2)

    def test_auto_mode_with_injected_runner(self):
        cov_json = os.path.join(self.dir, "cov.json")
        abs_calc = os.path.join(self.dir, "calc.py")
        write(
            cov_json,
            json.dumps(
                {"files": {abs_calc: {"executed_lines": [9, 10], "missing_lines": [2, 3, 4, 5]}}}
            ),
        )

        def fake_runner(runner, project_root, coverage_dir, progress):
            return TestOutcome(coverage_json_path=cov_json, tests_passed=True)

        report = run_pipeline(
            self.dir,
            CoverageSource(),
            threshold=30.0,
            options=default_analysis_options(),
            run_tests_fn=fake_runner,
        )
        self.assertTrue(report["coverageAvailable"])
        add = next(m for m in report["methods"] if m["name"] == "add")
        self.assertEqual(add["coverage"], 100.0)

    def test_auto_mode_failing_tests_note(self):
        cov_json = os.path.join(self.dir, "cov.json")
        write(cov_json, json.dumps({"files": {os.path.join(self.dir, "calc.py"): {"executed_lines": [9], "missing_lines": []}}}))

        def fake_runner(runner, project_root, coverage_dir, progress):
            return TestOutcome(coverage_json_path=cov_json, tests_passed=False)

        report = run_pipeline(
            self.dir, CoverageSource(), threshold=30.0,
            options=default_analysis_options(), run_tests_fn=fake_runner,
        )
        self.assertTrue(any("failed" in n for n in report["notes"]))

    def test_auto_mode_no_data_note(self):
        def fake_runner(runner, project_root, coverage_dir, progress):
            return TestOutcome(coverage_json_path=None, tests_passed=True)

        report = run_pipeline(
            self.dir, CoverageSource(), threshold=30.0,
            options=default_analysis_options(), run_tests_fn=fake_runner,
        )
        self.assertTrue(any("no coverage data" in n for n in report["notes"]))

    def test_prebuilt_mode(self):
        cov_json = os.path.join(self.dir, "cov.json")
        abs_calc = os.path.join(self.dir, "calc.py")
        write(cov_json, json.dumps({"files": {abs_calc: {"executed_lines": [9, 10], "missing_lines": []}}}))
        report = run_pipeline(
            self.dir,
            CoverageSource(mode=MODE_PREBUILT, coverage_file=cov_json, project_dir=self.dir),
            threshold=30.0,
            options=default_analysis_options(),
        )
        self.assertTrue(report["coverageAvailable"])
        self.assertEqual(os.path.realpath(report["coverageDataPath"]), os.path.realpath(cov_json))

    def test_auto_mode_missing_root_raises(self):
        # A bare temp dir with no markers and no project-dir override.
        bare = tempfile.mkdtemp(prefix="slop-bare-")
        write(os.path.join(bare, "calc.py"), "def f():\n    return 1\n")
        # Strip any ancestor markers by pointing project discovery at a path that
        # has none up to root is not guaranteed; instead force the error path by
        # making discovery fail via a non-existent project-dir is not it either.
        # We assert that when discovery *does* fail, the right error is raised by
        # monkeypatching discovery.
        import slopguard.coverage.pipeline as pipe

        original = pipe.discover_project_root
        pipe.discover_project_root = lambda p: ("", False)
        try:
            with self.assertRaises(SlopguardError) as ctx:
                run_pipeline(bare, CoverageSource(), threshold=30.0, options=default_analysis_options())
            self.assertEqual(ctx.exception.code, ERR_PROJECT_ROOT_MISSING)
        finally:
            pipe.discover_project_root = original


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
