import unittest

from slopguard.glob import glob_to_regex, matches_any


class GlobTests(unittest.TestCase):
    def test_star_crosses_separators(self):
        self.assertTrue(matches_any(["**/.venv/**"], "pkg/.venv/lib/foo.py"))
        self.assertTrue(matches_any(["**/.venv/**"], ".venv/lib/foo.py"))

    def test_leading_slash_variant(self):
        # gitignore-style: **/tests/** should hit a top-level tests/ dir.
        self.assertTrue(matches_any(["**/tests/**"], "tests/test_x.py"))

    def test_question_mark_matches_one(self):
        self.assertTrue(matches_any(["foo?.py"], "foo1.py"))
        self.assertFalse(matches_any(["foo?.py"], "foo12.py"))

    def test_char_class(self):
        self.assertTrue(matches_any(["foo[0-9].py"], "foo3.py"))
        self.assertFalse(matches_any(["foo[0-9].py"], "fooa.py"))

    def test_negated_char_class(self):
        self.assertTrue(matches_any(["foo[!0-9].py"], "fooa.py"))
        self.assertFalse(matches_any(["foo[!0-9].py"], "foo3.py"))

    def test_unterminated_class_is_literal(self):
        rx = glob_to_regex("a[b")
        self.assertTrue(rx.match("a[b"))

    def test_suffix_glob(self):
        self.assertTrue(matches_any(["**/*_pb2.py"], "gen/foo_pb2.py"))
        self.assertFalse(matches_any(["**/*_pb2.py"], "gen/foo.py"))

    def test_no_match(self):
        self.assertFalse(matches_any(["**/build/**"], "src/app.py"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
