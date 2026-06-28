import json
import unittest

from slopguard.aggregator import aggregate
from slopguard.complexity import analyze_source
from slopguard.formatting import error_json, error_text_line, json_report, pretty_report


def sample_report():
    fr = analyze_source(
        "def tangled(a, b, c):\n"
        "    if a:\n        if b:\n            if c:\n                return 1\n    return 0\n",
        "calc.py",
    )
    return aggregate([fr], source_root="/proj", threshold=30.0)


class FormattingTests(unittest.TestCase):
    def test_pretty_has_sections(self):
        text = pretty_report(sample_report(), 20)
        self.assertIn("slopguard-python", text)
        self.assertIn("schema 2", text)
        self.assertIn("Summary", text)
        self.assertIn("Top methods by wCRAP", text)
        self.assertIn("tangled", text)
        self.assertIn("coverage:  unavailable", text)

    def test_pretty_marks_crappy(self):
        fr = analyze_source(
            "def f(a, b, c, d, e):\n"
            "    if a and b and c and d and e:\n"
            "        if a:\n            if b:\n                if c:\n                    return 1\n"
            "    return 0\n",
            "x.py",
        )
        report = aggregate([fr], source_root="/proj", threshold=5.0)
        text = pretty_report(report, 20)
        self.assertIn("! ", text)
        self.assertIn("above threshold", text)

    def test_pretty_empty(self):
        report = aggregate([], source_root="/proj", threshold=30.0)
        self.assertIn("No methods analyzed.", pretty_report(report, 20))

    def test_json_sorted_keys_and_whole_floats(self):
        out = json_report(sample_report())
        parsed = json.loads(out)
        self.assertEqual(parsed["tool"], "slopguard-python")
        # whole-number floats render without a trailing .0
        self.assertIn('"coverage": 0,', out)
        self.assertIn('"threshold": 30,', out)
        # keys are alphabetically sorted at the top level
        keys = list(parsed.keys())
        self.assertEqual(keys, sorted(keys))
        # method dict keys sorted too
        mkeys = list(parsed["methods"][0].keys())
        self.assertEqual(mkeys, sorted(mkeys))

    def test_json_typename_null_for_function(self):
        out = json.loads(json_report(sample_report()))
        self.assertIsNone(out["methods"][0]["typeName"])

    def test_error_envelopes(self):
        env = {"code": "file_not_found", "message": "nope"}
        self.assertEqual(error_text_line(env), "slopguard-python: [file_not_found] nope")
        self.assertEqual(json.loads(error_json(env)), {"error": env})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
