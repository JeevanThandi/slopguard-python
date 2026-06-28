import io
import json
import os
import tempfile
import unittest

from slopguard.cli import run


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    code = run(list(args), out, err)
    return out.getvalue(), err.getvalue(), code


def temp_project():
    d = tempfile.mkdtemp(prefix="slop-cli-")
    with open(os.path.join(d, "x.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "def tangled(a, b, c):\n"
            "    if a:\n        if b:\n            if c:\n                return 1\n    return 0\n"
        )
    return d


class CliTests(unittest.TestCase):
    def test_version_command(self):
        out, _, code = run_cli("version")
        self.assertEqual(code, 0)
        payload = json.loads(out)
        self.assertEqual(payload["name"], "slopguard-python")
        self.assertTrue(payload["version"])

    def test_help(self):
        out, _, code = run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("CRAP", out)
        self.assertIn("analyze", out)

    def test_bare_version_flag(self):
        out, _, code = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertEqual(out.strip(), "0.1.0")

    def test_analyze_no_coverage_text(self):
        d = temp_project()
        out, _, code = run_cli("analyze", "--path", d, "--no-coverage")
        self.assertEqual(code, 0)
        self.assertIn("tangled", out)
        self.assertIn("Summary", out)

    def test_analyze_json(self):
        d = temp_project()
        out, _, code = run_cli("analyze", "--path", d, "--no-coverage", "--json")
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report["tool"], "slopguard-python")
        self.assertEqual(report["summary"]["methodCount"], 1)

    def test_default_command_is_analyze(self):
        d = temp_project()
        out, _, code = run_cli("--path", d, "--no-coverage")
        self.assertEqual(code, 0)
        self.assertIn("tangled", out)

    def test_fail_over_exits_two(self):
        d = temp_project()
        _, err, code = run_cli("analyze", "--path", d, "--no-coverage", "--fail-over", "5")
        self.assertEqual(code, 2)
        self.assertIn("fail-over", err)

    def test_fail_over_passes(self):
        d = temp_project()
        _, _, code = run_cli("analyze", "--path", d, "--no-coverage", "--fail-over", "10000")
        self.assertEqual(code, 0)

    def test_invalid_threshold(self):
        _, _, code = run_cli("analyze", "--threshold", "notanumber", "--no-coverage")
        self.assertEqual(code, 1)

    def test_error_as_json(self):
        _, err, code = run_cli("analyze", "--path", "/no/such/dir/xyz", "--no-coverage", "--json")
        self.assertEqual(code, 1)
        self.assertIn('"error"', err)
        self.assertIn("file_not_found", err)

    def test_error_as_text(self):
        _, err, code = run_cli("analyze", "--path", "/no/such/dir/xyz", "--no-coverage")
        self.assertEqual(code, 1)
        self.assertIn("slopguard-python:", err)
        self.assertIn("file_not_found", err)

    def test_quiet_silences_progress(self):
        d = temp_project()
        _, err, code = run_cli("analyze", "--path", d, "--no-coverage", "--quiet")
        self.assertEqual(code, 0)
        self.assertEqual(err, "")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
