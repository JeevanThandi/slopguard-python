import unittest

from slopguard.aggregator import aggregate
from slopguard.complexity import analyze_source
from slopguard.crap import weighted_complexity
from slopguard.models import KIND_FUNCTION, FileReport, MethodMetric


class FakeCoverage:
    """Returns a fixed coverage for a single file; unknown files -> None."""

    def __init__(self, abs_file, pct):
        self._abs = abs_file
        self._pct = pct

    def method_coverage(self, absolute_path, line, end_line):
        return self._pct if absolute_path == self._abs else None

    def file_coverage(self, absolute_path):
        return self._pct if absolute_path == self._abs else None


def method(name, cyc, cog, type_name=None, qualified=None, line=1, end=5):
    return MethodMetric(
        name=name,
        qualified_name=qualified or name,
        type_name=type_name,
        kind=KIND_FUNCTION,
        file="m.py",
        start_line=line,
        end_line=end,
        complexity=cyc,
        cognitive_complexity=cog,
        weighted_complexity=weighted_complexity(cyc, cog),
    )


class AggregatorTests(unittest.TestCase):
    def test_no_coverage_reads_zero(self):
        fr = FileReport(path="m.py", methods=[method("tangled", 12, 8)])
        report = aggregate([fr], source_root="/proj", threshold=30.0)
        self.assertFalse(report["coverageAvailable"])
        self.assertIsNone(report["summary"]["weightedCoverage"])
        m = report["methods"][0]
        self.assertEqual(m["coverage"], 0.0)
        self.assertTrue(m["crap"] > 30)
        self.assertTrue(m["isCrappy"])
        self.assertEqual(report["summary"]["crappyMethodCount"], 1)

    def test_full_coverage_collapses_to_complexity(self):
        import os

        abs_file = os.path.join(os.path.abspath("/proj"), "m.py")
        fr = FileReport(path="m.py", methods=[method("tangled", 12, 8)])
        report = aggregate(
            [fr], source_root="/proj", threshold=30.0, coverage=FakeCoverage(abs_file, 100.0)
        )
        m = report["methods"][0]
        self.assertEqual(m["coverage"], 100.0)
        self.assertAlmostEqual(m["crap"], m["weightedComplexity"])
        self.assertEqual(report["summary"]["weightedCoverage"], 100.0)

    def test_methods_sorted_by_crap_desc(self):
        fr = FileReport(
            path="m.py",
            methods=[method("small", 1, 1, line=1, end=2), method("big", 10, 10, line=4, end=20)],
        )
        report = aggregate([fr], source_root="/proj", threshold=30.0)
        self.assertEqual([m["name"] for m in report["methods"]], ["big", "small"])
        self.assertEqual(report["summary"]["maxCrap"], report["methods"][0]["crap"])

    def test_type_rollup_lexical(self):
        report = analyze_source(
            "class C:\n"
            "    def a(self, x):\n        return x\n"
            "    def b(self, x, y):\n        if x and y:\n            return 1\n        return 0\n",
            "m.py",
        )
        out = aggregate([report], source_root="/proj", threshold=30.0)
        types = out["types"]
        self.assertEqual(len(types), 1)
        t = types[0]
        self.assertEqual(t["name"], "C")
        self.assertEqual(t["methodCount"], 2)
        self.assertEqual(t["kind"], "class")
        self.assertIn("aggregatedCrap", t)
        self.assertIn("sumCrap", t)

    def test_empty_analysis_has_stable_shape(self):
        report = aggregate([], source_root="/proj", threshold=30.0)
        self.assertEqual(report["methods"], [])
        self.assertEqual(report["types"], [])
        self.assertEqual(report["summary"]["methodCount"], 0)
        self.assertEqual(report["summary"]["averageCrap"], 0.0)
        self.assertEqual(report["tool"], "slopguard-python")
        self.assertEqual(report["schemaVersion"], "2")

    def test_schema_two_note_always_present(self):
        report = aggregate([], source_root="/proj", threshold=30.0, notes=["extra"])
        self.assertTrue(report["notes"][0].startswith("Score is wCRAP"))
        self.assertEqual(report["notes"][-1], "extra")

    def test_generated_at_format(self):
        report = aggregate([], source_root="/proj", threshold=30.0)
        # YYYY-MM-DDTHH:MM:SS.mmmZ
        self.assertRegex(report["generatedAt"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
