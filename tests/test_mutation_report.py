"""The mutation report: score maths, summary sums, notes, and the text and
JSON renderings."""

import json
import unittest
from datetime import datetime, timezone

from slopguard.formatting import format_number, mutant_snippet, mutation_json_report, mutation_pretty_report
from slopguard.mutation.models import MutantSite, PlannedMutant, mutation_score, summarize
from slopguard.mutation.pipeline import _Execution, build_report

STATUSES = ["killed", "survived", "timeout", "no_coverage", "compile_error", "ignored", "pending"]


def planned(line, operator="boundary", original="<", replacement="<=", method="Store.add", file="store.py", column=5):
    site = MutantSite(file, line, column, operator, original, replacement, 0, len(original))
    return PlannedMutant(site=site, method=method, ignored=False)


def execution(statuses, notes=None, timeout=16, coverage=True):
    return _Execution("/abs/project", "pytest", timeout, coverage, list(notes or []), list(statuses))


WHEN = datetime(2026, 9, 30, 12, 0, 0, 123456, tzinfo=timezone.utc)


class ScoreTests(unittest.TestCase):
    def test_formula(self):
        self.assertEqual(mutation_score(killed=18, timed_out=1, survived=3, no_coverage=1), 19 / 23 * 100)
        self.assertEqual(mutation_score(1, 0, 0, 0), 100.0)
        self.assertEqual(mutation_score(0, 0, 4, 0), 0.0)

    def test_none_when_nothing_counts(self):
        self.assertIsNone(mutation_score(0, 0, 0, 0))

    def test_summary_counts_sum_to_mutant_count(self):
        mutants = [{"status": s} for s in STATUSES + ["killed", "survived", "killed"]]
        summary = summarize(mutants, file_count=3)
        counted = sum(
            summary[k]
            for k in ("killed", "survived", "timedOut", "noCoverage", "compileErrors", "ignored", "pending")
        )
        self.assertEqual(counted, summary["mutantCount"])
        self.assertEqual(summary["mutantCount"], 10)
        self.assertEqual(summary["fileCount"], 3)
        self.assertEqual((summary["killed"], summary["survived"], summary["timedOut"]), (3, 2, 1))
        self.assertEqual(summary["mutationScore"], 4 / 7 * 100)

    def test_compile_errors_ignored_and_pending_are_not_scored(self):
        summary = summarize([{"status": s} for s in ("compile_error", "ignored", "pending")], 1)
        self.assertIsNone(summary["mutationScore"])


class BuildReportTests(unittest.TestCase):
    def test_shape_and_values(self):
        plans = [planned(3), planned(4, operator="remove_call", original="foo()", replacement="pass")]
        report = build_report(
            "/abs/project/src", ["boundary", "remove_call"], 2, plans, ["killed", "survived"], execution(["killed", "survived"]), WHEN
        )
        self.assertEqual(
            sorted(report),
            sorted(
                [
                    "coverageAvailable",
                    "generatedAt",
                    "mutants",
                    "notes",
                    "operators",
                    "projectRoot",
                    "reportType",
                    "runner",
                    "schemaVersion",
                    "sourceRoot",
                    "summary",
                    "timeoutSeconds",
                    "tool",
                    "toolVersion",
                ]
            ),
        )
        self.assertEqual(report["generatedAt"], "2026-09-30T12:00:00.123Z")
        self.assertEqual(report["reportType"], "mutation")
        self.assertEqual(report["schemaVersion"], "1")
        self.assertEqual(report["tool"], "slopguard-python")
        self.assertEqual(report["projectRoot"], "/abs/project")
        self.assertEqual(report["runner"], "pytest")
        self.assertEqual(report["timeoutSeconds"], 16)
        self.assertTrue(report["coverageAvailable"])
        self.assertEqual(
            report["mutants"][0],
            {
                "column": 5,
                "file": "store.py",
                "id": "store.py:3:5:boundary",
                "line": 3,
                "method": "Store.add",
                "operator": "boundary",
                "original": "<",
                "replacement": "<=",
                "status": "killed",
            },
        )
        self.assertEqual(report["summary"]["mutationScore"], 50.0)

    def test_dry_run_report_has_nulls(self):
        report = build_report("/src", ["boundary"], 1, [planned(3)], ["pending"], None, WHEN)
        self.assertIsNone(report["projectRoot"])
        self.assertIsNone(report["runner"])
        self.assertIsNone(report["timeoutSeconds"])
        self.assertFalse(report["coverageAvailable"])
        self.assertIsNone(report["summary"]["mutationScore"])
        self.assertEqual(report["notes"], [])

    def test_notes_order_execution_then_results(self):
        statuses = ["survived", "compile_error", "compile_error"]
        report = build_report(
            "/src",
            ["boundary"],
            1,
            [planned(1), planned(2), planned(3)],
            statuses,
            execution(statuses, notes=["Restored /x.py, which an interrupted mutate run left mutated."]),
            WHEN,
        )
        self.assertEqual(
            report["notes"],
            [
                "Restored /x.py, which an interrupted mutate run left mutated.",
                "Every tested mutant survived. Check that the tests import the source under --path.",
                "2 mutant(s) did not compile and are excluded from the score.",
            ],
        )

    def test_no_all_survived_note_when_something_was_killed_or_timed_out(self):
        for statuses in (["survived", "killed"], ["survived", "timeout"], ["no_coverage"]):
            report = build_report(
                "/src", ["boundary"], 1, [planned(i + 1) for i in range(len(statuses))], statuses, execution(statuses), WHEN
            )
            self.assertEqual(report["notes"], [], statuses)


class JsonFormattingTests(unittest.TestCase):
    def test_sorted_keys_and_whole_floats(self):
        statuses = ["killed"]
        report = build_report("/src", ["boundary"], 3, [planned(16)], statuses, execution(statuses, timeout=16), WHEN)
        out = mutation_json_report(report)
        parsed = json.loads(out)
        self.assertEqual(list(parsed), sorted(parsed))
        self.assertEqual(list(parsed["summary"]), sorted(parsed["summary"]))
        self.assertEqual(list(parsed["mutants"][0]), sorted(parsed["mutants"][0]))
        self.assertIn('"mutationScore": 100,', out)
        self.assertIn('"timeoutSeconds": 16,', out)
        self.assertIn('"projectRoot": "/abs/project",', out)

    def test_fractional_values_keep_their_decimals(self):
        statuses = ["killed", "killed", "survived"]
        report = build_report(
            "/src", ["boundary"], 1, [planned(1), planned(2), planned(3)], statuses, execution(statuses, timeout=2.5), WHEN
        )
        parsed = json.loads(mutation_json_report(report))
        self.assertEqual(parsed["timeoutSeconds"], 2.5)
        self.assertAlmostEqual(parsed["summary"]["mutationScore"], 200 / 3)

    def test_nulls_in_a_dry_run(self):
        report = build_report("/src", ["boundary"], 1, [planned(1)], ["pending"], None, WHEN)
        out = mutation_json_report(report)
        self.assertIn('"mutationScore": null', out)
        self.assertIn('"runner": null', out)
        self.assertIn('"coverageAvailable": false', out)


class TextFormattingTests(unittest.TestCase):
    def test_full_layout(self):
        plans = [
            planned(16, operator="negate_conditional", original="==", replacement="!=", method="Store.toggle", column=25),
            planned(12, operator="remove_call", original="notify(x)", replacement="pass", method=None, file="filter.py", column=3),
            planned(20, operator="boundary", original="<", replacement="<=", method="Store.size", column=9),
            planned(21),
            planned(22),
        ]
        statuses = ["survived", "no_coverage", "timeout", "killed", "ignored"]
        report = build_report("/abs/project/src", ["boundary"], 3, plans, statuses, execution(statuses, notes=["A note."]), WHEN)
        self.assertEqual(
            mutation_pretty_report(report),
            "slopguard-python 0.2.0 — mutation report (schema 1)\n"
            "source:    /abs/project/src\n"
            "project:   /abs/project\n"
            "runner:    pytest\n"
            "timeout:   16s per mutant\n"
            "\n"
            "Notes\n"
            "  • A note.\n"
            "\n"
            "Summary\n"
            "  files:          3\n"
            "  mutants:        5\n"
            "  killed:         1\n"
            "  timed out:      1\n"
            "  survived:       1\n"
            "  no coverage:    1\n"
            "  compile errors: 0\n"
            "  ignored:        1\n"
            "  score:          50.00%\n"
            "\n"
            "Survived (1) — tests still pass with these changes\n"
            "  store.py:16:25  negate_conditional  `==` → `!=`  Store.toggle\n"
            "\n"
            "No coverage (1) — no test runs these lines\n"
            "  filter.py:12:3  remove_call  `notify(x)` → `pass`\n"
            "\n"
            "Timed out (1) — counted as killed\n"
            "  store.py:20:9  boundary  `<` → `<=`  Store.size\n",
        )

    def test_dry_run_layout(self):
        plans = [planned(3, operator="remove_not", original="not ", replacement="", method=None)]
        report = build_report("/src", ["remove_not"], 1, plans, ["pending"], None, WHEN)
        self.assertEqual(
            mutation_pretty_report(report),
            "slopguard-python 0.2.0 — mutation report (schema 1)\n"
            "source:    /src\n"
            "project:   (not run)\n"
            "runner:    (not run)\n"
            "timeout:   (not run)\n"
            "\n"
            "Summary\n"
            "  files:          1\n"
            "  mutants:        1\n"
            "  killed:         0\n"
            "  timed out:      0\n"
            "  survived:       0\n"
            "  no coverage:    0\n"
            "  compile errors: 0\n"
            "  ignored:        0\n"
            "  pending:        1\n"
            "  score:          n/a\n"
            "\n"
            "Mutants (1, not run)\n"
            "  store.py:3:5  remove_not  `not ` → ``\n",
        )

    def test_fractional_timeout_in_the_header(self):
        report = build_report("/src", ["boundary"], 1, [], [], execution([], timeout=2.5), WHEN)
        self.assertIn("timeout:   2.5s per mutant\n", mutation_pretty_report(report))

    def test_snippets_collapse_whitespace_and_truncate(self):
        self.assertEqual(mutant_snippet("obj.method(1,\n               2)"), "obj.method(1, 2)")
        exactly_40 = "x" * 40
        self.assertEqual(mutant_snippet(exactly_40), exactly_40)
        long = "a" * 39 + "日本語"
        self.assertEqual(mutant_snippet(long), "a" * 39 + "…")
        self.assertEqual(len(mutant_snippet("é" * 100)), 40)

    def test_long_statement_in_listing(self):
        original = "self.register(\n    first_argument,\n    second_argument,\n)"
        plans = [planned(7, operator="remove_call", original=original, replacement="pass", method="App.start")]
        report = build_report("/src", ["remove_call"], 1, plans, ["survived"], execution(["survived"]), WHEN)
        self.assertIn(
            "  store.py:7:5  remove_call  `self.register( first_argument, second_a…` → `pass`  App.start\n",
            mutation_pretty_report(report),
        )

    def test_format_number(self):
        self.assertEqual(format_number(16), "16")
        self.assertEqual(format_number(16.0), "16")
        self.assertEqual(format_number(2.5), "2.5")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
