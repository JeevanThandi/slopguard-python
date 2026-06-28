"""Pinned cyclomatic + cognitive complexity values — the analyzer's contract.

These numbers are the cross-language contract shared with the Go/Kotlin/Swift/
TypeScript siblings. If you touch the analyzer, these tests are what locks the
behaviour down; update them deliberately.
"""

import ast
import textwrap
import unittest

from slopguard.complexity import analyze_source
from slopguard.models import (
    KIND_CONSTRUCTOR,
    KIND_FUNCTION,
    KIND_GETTER,
    KIND_METHOD,
    KIND_SETTER,
)


def one(src, qualified):
    report = analyze_source(src, "test.py")
    for m in report.methods:
        if m.qualified_name == qualified:
            return m
    raise AssertionError(
        f"method {qualified!r} not found; got {[m.qualified_name for m in report.methods]}"
    )


class ComplexityTests(unittest.TestCase):
    def test_simple_function_is_baseline(self):
        m = one("def add(a, b):\n    return a + b\n", "add")
        self.assertEqual((m.complexity, m.cognitive_complexity), (1, 0))
        self.assertEqual(m.kind, KIND_FUNCTION)
        self.assertEqual(m.weighted_complexity, 0.0)

    def test_if_elif_else_chain(self):
        src = (
            "def grade(n):\n"
            "    if n > 90:\n        return 'a'\n"
            "    elif n > 80:\n        return 'b'\n"
            "    else:\n        return 'c'\n"
        )
        m = one(src, "grade")
        self.assertEqual(m.complexity, 3, "head if + elif")
        self.assertEqual(m.cognitive_complexity, 3, "if 1 + elif 1 + else 1")

    def test_nesting_amplifies_cognitive(self):
        src = "def f(a, b):\n    if a:\n        if b:\n            print('x')\n"
        m = one(src, "f")
        self.assertEqual(m.complexity, 3)
        self.assertEqual(m.cognitive_complexity, 3, "outer 1 + inner 2")

    def test_loops(self):
        src = (
            "def s(xs):\n    t = 0\n"
            "    for i in range(len(xs)):\n        t += xs[i]\n"
            "    while t < 0:\n        t += 1\n"
            "    return t\n"
        )
        m = one(src, "s")
        self.assertEqual(m.complexity, 3, "base + for + while")
        self.assertEqual(m.cognitive_complexity, 2)

    def test_boolean_run_collapse(self):
        src = "def f(a, b, c):\n    if a and b and c:\n        print('x')\n"
        m = one(src, "f")
        self.assertEqual(m.complexity, 4, "base + if + 2 ands")
        self.assertEqual(m.cognitive_complexity, 2, "if 1 + one and-run 1")

    def test_mixed_boolean_runs_count_transitions(self):
        m = one("def f(a, b, c):\n    return a and b or c\n", "f")
        self.assertEqual(m.complexity, 3, "base + and + or")
        self.assertEqual(m.cognitive_complexity, 2, "and run + or run")

    def test_ternary_is_structural(self):
        src = "def f(a):\n    if a:\n        x = 1 if a else 2\n    return 0\n"
        m = one(src, "f")
        # if (cyc1, cog1+nest0) ; ternary at nesting 1 (cyc1, cog 1+1=2)
        self.assertEqual(m.complexity, 3)
        self.assertEqual(m.cognitive_complexity, 3)

    def test_except_handlers(self):
        src = (
            "def f():\n    try:\n        g()\n"
            "    except ValueError:\n        pass\n"
            "    except KeyError:\n        pass\n"
        )
        m = one(src, "f")
        self.assertEqual(m.complexity, 3, "base + 2 except")
        self.assertEqual(m.cognitive_complexity, 2, "two handlers at nesting 0")

    def test_comprehension(self):
        m = one("def f(xs):\n    return [x for x in xs if x > 0 if x < 10]\n", "f")
        self.assertEqual(m.complexity, 4, "base + for + 2 filter ifs")
        self.assertEqual(m.cognitive_complexity, 3, "for 1 + 2 filters")

    def test_lambda_bumps_nesting_no_entry(self):
        src = "def outer(xs):\n    run(lambda: (1 if xs else 0))\n"
        report = analyze_source(src, "test.py")
        self.assertEqual(len(report.methods), 1, "lambda must not get its own entry")
        m = report.methods[0]
        self.assertEqual(m.cognitive_complexity, 2, "ternary inside lambda at nesting 1")

    def test_nested_def_gets_own_entry(self):
        src = "def outer():\n    def inner(a):\n        if a:\n            pass\n"
        report = analyze_source(src, "test.py")
        names = sorted(m.qualified_name for m in report.methods)
        self.assertEqual(names, ["outer", "outer.inner"])
        inner = one(src, "outer.inner")
        self.assertEqual((inner.complexity, inner.cognitive_complexity), (2, 1))

    def test_method_kinds_and_type(self):
        src = (
            "class P:\n"
            "    def __init__(self):\n        self.x = 1\n"
            "    @property\n    def val(self):\n        return self.x\n"
            "    @val.setter\n    def val(self, v):\n        self.x = v\n"
            "    def parse(self, a, b):\n        if a and b:\n            return 1\n        return 0\n"
        )
        report = analyze_source(src, "test.py")
        self.assertEqual([(t.name, t.kind) for t in report.types], [("P", "class")])
        kinds = {m.qualified_name + "/" + m.kind for m in report.methods}
        self.assertIn("P.__init__/" + KIND_CONSTRUCTOR, kinds)
        self.assertIn("P.val/" + KIND_GETTER, kinds)
        self.assertIn("P.val/" + KIND_SETTER, kinds)
        self.assertIn("P.parse/" + KIND_METHOD, kinds)
        for m in report.methods:
            self.assertEqual(m.type_name, "P")

    def test_nested_class_qualified_names(self):
        src = "class Outer:\n    class Inner:\n        def bar(self):\n            return 1\n"
        report = analyze_source(src, "test.py")
        names = sorted(t.name for t in report.types)
        self.assertEqual(names, ["Outer", "Outer.Inner"])
        bar = one(src, "Outer.Inner.bar")
        self.assertEqual(bar.type_name, "Outer.Inner")
        self.assertEqual(bar.kind, KIND_METHOD)

    def test_async_function_and_loop(self):
        src = "async def f(xs):\n    async for x in xs:\n        await g(x)\n"
        m = one(src, "f")
        self.assertEqual(m.complexity, 2, "base + async for")
        self.assertEqual(m.cognitive_complexity, 1)

    def test_generated_file_skipped(self):
        # Direct analyze_source does not filter; the file analyzer does. Here we
        # just confirm a normal parse still produces methods.
        report = analyze_source("def f():\n    return 1\n", "x.py")
        self.assertEqual(len(report.methods), 1)

    def test_syntax_error_raises_parse_failed(self):
        from slopguard.errors import ERR_PARSE_FAILED, SlopguardError

        with self.assertRaises(SlopguardError) as ctx:
            analyze_source("def f(:\n", "bad.py")
        self.assertEqual(ctx.exception.code, ERR_PARSE_FAILED)

    @unittest.skipUnless(hasattr(ast, "Match"), "match is 3.10+")
    def test_match_statement(self):
        src = textwrap.dedent(
            """
            def classify(n):
                match n:
                    case 1:
                        return 'a'
                    case 2 if n > 0:
                        return 'b'
                    case _:
                        return '?'
            """
        )
        m = one(src, "classify")
        # whole match = cog 1; cyclomatic: 2 non-wildcard cases + 1 guard = base+3
        self.assertEqual(m.cognitive_complexity, 1)
        self.assertEqual(m.complexity, 4)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
