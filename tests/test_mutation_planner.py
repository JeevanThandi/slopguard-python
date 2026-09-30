"""Ignore markers (all three forms) and the planner: operator filter, sort
order, enclosing-method names and parse errors."""

import textwrap
import unittest

from slopguard.errors import ERR_PARSE_FAILED, SlopguardError
from slopguard.models import MethodMetric
from slopguard.mutation.ignore import ALL, is_ignored, parse_ignore_markers
from slopguard.mutation.operators import OPERATOR_IDS
from slopguard.mutation.planner import enclosing_method, plan_file
from slopguard.mutation.source import SourceFile


def plan(src, operators=OPERATOR_IDS, path="t.py"):
    source = SourceFile(textwrap.dedent(src).encode("utf-8"), path)
    return plan_file(source, path, operators)


def method(qualified, start, end):
    return MethodMetric(
        name=qualified.split(".")[-1],
        qualified_name=qualified,
        type_name=None,
        kind="function",
        file="t.py",
        start_line=start,
        end_line=end,
        complexity=1,
        cognitive_complexity=0,
    )


class IgnoreMarkerTests(unittest.TestCase):
    def test_bare_marker_ignores_every_operator_on_its_line(self):
        markers = parse_ignore_markers(["x = a < b  # slopguard-ignore-mutant"])
        self.assertEqual(markers, {1: ALL})
        self.assertTrue(is_ignored(markers, 1, "boundary"))
        self.assertTrue(is_ignored(markers, 1, "negate_conditional"))
        self.assertFalse(is_ignored(markers, 2, "boundary"))

    def test_listed_operators_only(self):
        markers = parse_ignore_markers(["x = a < b  # slopguard-ignore-mutant( boundary , logical ): same max"])
        self.assertEqual(markers, {1: frozenset({"boundary", "logical"})})
        self.assertTrue(is_ignored(markers, 1, "boundary"))
        self.assertFalse(is_ignored(markers, 1, "negate_conditional"))

    def test_unknown_ids_are_dropped(self):
        markers = parse_ignore_markers(["x  # slopguard-ignore-mutant(nope,boundary)"])
        self.assertEqual(markers, {1: frozenset({"boundary"})})
        markers = parse_ignore_markers(["x  # slopguard-ignore-mutant(nope)"])
        self.assertFalse(is_ignored(markers, 1, "boundary"))

    def test_unclosed_list_reads_to_the_end_of_the_line(self):
        markers = parse_ignore_markers(["x  # slopguard-ignore-mutant(boundary, logical"])
        self.assertEqual(markers, {1: frozenset({"boundary", "logical"})})

    def test_comment_only_line_applies_to_the_next_line(self):
        lines = [
            "    # slopguard-ignore-mutant(boundary): equal values keep the same best",
            "    if a > best:",
        ]
        markers = parse_ignore_markers(lines)
        self.assertEqual(markers, {2: frozenset({"boundary"})})
        self.assertFalse(is_ignored(markers, 1, "boundary"))
        self.assertTrue(is_ignored(markers, 2, "boundary"))

    def test_markers_for_the_same_line_merge(self):
        lines = [
            "# slopguard-ignore-mutant(boundary)",
            "x = a < b  # slopguard-ignore-mutant(logical)",
            "# slopguard-ignore-mutant(logical)",
            "y = 1  # slopguard-ignore-mutant",
        ]
        markers = parse_ignore_markers(lines)
        self.assertEqual(markers[2], frozenset({"boundary", "logical"}))
        self.assertEqual(markers[4], ALL)

    def test_all_wins_over_a_list(self):
        markers = parse_ignore_markers(["# slopguard-ignore-mutant", "x = 1  # slopguard-ignore-mutant(boundary)"])
        self.assertEqual(markers, {2: ALL})

    def test_marker_text_inside_a_string_counts(self):
        # A plain text search, like every port.
        markers = parse_ignore_markers(['s = "slopguard-ignore-mutant"'])
        self.assertEqual(markers, {1: ALL})


class PlannerTests(unittest.TestCase):
    SRC = """\
        def top(a, b):
            if a < b:  # slopguard-ignore-mutant(boundary)
                return True
            return a + b

        class Store:
            def add(self, x):
                # slopguard-ignore-mutant
                self.n += x
                return not x

            def outer(self):
                def inner(y):
                    return -y
                return inner

        LIMIT = 3 * 4
        """

    def test_filter_sort_method_and_ignores(self):
        planned = plan(self.SRC)
        rows = [(p.site.line, p.site.operator, p.method, p.ignored) for p in planned]
        self.assertEqual(
            rows,
            [
                (2, "boundary", "top", True),
                (2, "negate_conditional", "top", False),
                (3, "boolean_literal", "top", False),
                (4, "arithmetic", "top", False),
                (9, "arithmetic", "Store.add", True),
                (10, "remove_not", "Store.add", False),
                (14, "invert_negative", "Store.outer.inner", False),
                (17, "arithmetic", None, False),
            ],
        )

    def test_operator_filter(self):
        planned = plan(self.SRC, operators=["arithmetic"])
        self.assertEqual({p.site.operator for p in planned}, {"arithmetic"})
        self.assertEqual(len(planned), 3)

    def test_syntax_error_is_parse_failed(self):
        with self.assertRaises(SlopguardError) as ctx:
            plan("def broken(:\n    pass\n")
        self.assertEqual(ctx.exception.code, ERR_PARSE_FAILED)

    def test_null_byte_is_parse_failed(self):
        with self.assertRaises(SlopguardError) as ctx:
            plan("x = 1\x00\n")
        self.assertEqual(ctx.exception.code, ERR_PARSE_FAILED)


class EnclosingMethodTests(unittest.TestCase):
    def test_innermost_span_wins(self):
        methods = [method("outer", 1, 10), method("outer.inner", 3, 5)]
        self.assertEqual(enclosing_method(methods, 4), "outer.inner")
        self.assertEqual(enclosing_method(methods, 8), "outer")
        self.assertIsNone(enclosing_method(methods, 11))

    def test_tie_goes_to_the_later_start(self):
        methods = [method("first", 1, 3), method("second", 3, 5)]
        self.assertEqual(enclosing_method(methods, 3), "second")
        self.assertEqual(enclosing_method(list(reversed(methods)), 3), "second")

    def test_no_methods(self):
        self.assertIsNone(enclosing_method([], 1))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
