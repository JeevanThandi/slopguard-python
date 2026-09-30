"""Mutant generation: every operator (positive and skip cases), positions in
code points, encodings, ids and sorting."""

import ast
import sys
import textwrap
import unittest

from slopguard.errors import ERR_INVALID_ARGUMENT, ERR_UNREADABLE_FILE, SlopguardError
from slopguard.mutation.models import MutantSite, mutant_id, site_sort_key
from slopguard.mutation.operators import OPERATOR_IDS, generate_mutants, parse_operators
from slopguard.mutation.source import SourceFile


def sites_of(data, path="t.py"):
    if isinstance(data, str):
        data = textwrap.dedent(data).encode("utf-8")
    source = SourceFile(data, path)
    return source, sorted(generate_mutants(source, ast.parse(source.text), path), key=site_sort_key)


def mutants(src, operator=None):
    """``(line, column, operator, original, replacement)`` tuples, sorted."""
    _, sites = sites_of(src)
    return [
        (s.line, s.column, s.operator, s.original, s.replacement)
        for s in sites
        if operator is None or s.operator == operator
    ]


class ArithmeticTests(unittest.TestCase):
    def test_binary_operators(self):
        got = mutants(
            "x = a + b\nx = a - b\nx = a * b\nx = a / b\nx = a % b\nx = a // b\n",
            "arithmetic",
        )
        self.assertEqual(
            got,
            [
                (1, 7, "arithmetic", "+", "-"),
                (2, 7, "arithmetic", "-", "+"),
                (3, 7, "arithmetic", "*", "/"),
                (4, 7, "arithmetic", "/", "*"),
                (5, 7, "arithmetic", "%", "*"),
                (6, 7, "arithmetic", "//", "*"),
            ],
        )

    def test_compound_assignments(self):
        got = mutants("a += 1\na -= 1\na *= 2\na /= 2\na %= 3\na //= 4\n", "arithmetic")
        self.assertEqual(
            [(g[3], g[4]) for g in got],
            [("+=", "-="), ("-=", "+="), ("*=", "/="), ("/=", "*="), ("%=", "*="), ("//=", "*=")],
        )

    def test_operator_between_parenthesized_operands(self):
        self.assertEqual(mutants("x = (a) + (b)\n", "arithmetic"), [(1, 9, "arithmetic", "+", "-")])

    def test_operator_on_a_continuation_line(self):
        got = mutants("x = (a\n     * b)\n", "arithmetic")
        self.assertEqual(got, [(2, 6, "arithmetic", "*", "/")])

    def test_unmutated_binary_operators(self):
        self.assertEqual(mutants("x = a ** b\ny = a @ b\nz = a << b\nw = a | b\n", "arithmetic"), [])

    def test_unmutated_compound_assignments(self):
        self.assertEqual(mutants("a **= 2\na |= 1\na <<= 1\na @= m\n", "arithmetic"), [])

    def test_unary_minus_is_not_arithmetic(self):
        self.assertEqual(mutants("x = -a\n", "arithmetic"), [])

    def test_string_concatenation_is_skipped(self):
        src = (
            'a = "s" + x\n'
            'b = x + "s"\n'
            'c = "s" + x + y\n'
            'd = ("s" + x) + y\n'
            'e = x + (y + "s")\n'
            'f = f"{x}" + y\n'
            'g = b"s" + y\n'
            's += "x"\n'
            's += f"{x}"\n'
            's += "a" + t\n'
        )
        self.assertEqual(mutants(src, "arithmetic"), [])

    def test_non_string_plus_next_to_concatenation_is_kept(self):
        # Only `+` chains with a stringy operand are concatenation.
        got = mutants('a = str(x + 1) + "s"\nb = x + y - z\n', "arithmetic")
        self.assertEqual(
            [(g[0], g[3]) for g in got],
            [(1, "+"), (2, "+"), (2, "-")],
        )

    def test_other_operators_with_string_operands_are_kept(self):
        got = mutants('a = "%d" % n\nb = "ab" * 3\ns -= "x"\n', "arithmetic")
        self.assertEqual([g[3] for g in got], ["%", "*", "-="])

    def test_long_concatenation_chain_does_not_recurse(self):
        chain = " + ".join(["'s'"] + [f"x{i}" for i in range(3000)])
        self.assertEqual(mutants(f"v = {chain}\n", "arithmetic"), [])


class BooleanLiteralTests(unittest.TestCase):
    def test_true_and_false(self):
        self.assertEqual(
            mutants("a = True\nb = False\n"),
            [(1, 5, "boolean_literal", "True", "False"), (2, 5, "boolean_literal", "False", "True")],
        )

    def test_not_other_constants(self):
        self.assertEqual(mutants("a = None\nb = 1\nc = 'True'\n"), [])

    def test_never_in_annotations(self):
        src = (
            "def f(a: Literal[True] = False) -> Literal[False]:\n"
            "    x: Literal[True] = True\n"
            "    return x\n"
        )
        got = mutants(src, "boolean_literal")
        self.assertEqual([(g[0], g[1], g[3]) for g in got], [(1, 26, "False"), (2, 24, "True")])

    @unittest.skipIf(sys.version_info < (3, 10), "match is 3.10+")
    def test_match_singleton_pattern(self):
        got = mutants("match x:\n    case True:\n        pass\n", "boolean_literal")
        self.assertEqual(got, [(2, 10, "boolean_literal", "True", "False")])

    @unittest.skipIf(sys.version_info < (3, 12), "type aliases and type params are 3.12+")
    def test_never_in_type_aliases_or_type_params(self):
        src = "type X = Literal[True]\ndef f[T: Literal[-1]](a):\n    return -a\n"
        got = mutants(src)
        self.assertEqual(got, [(3, 12, "invert_negative", "-", "")])


class ComparisonTests(unittest.TestCase):
    def test_boundary(self):
        got = mutants("a < b\na <= b\na > b\na >= b\n", "boundary")
        self.assertEqual([(g[3], g[4]) for g in got], [("<", "<="), ("<=", "<"), (">", ">="), (">=", ">")])

    def test_negate_conditional(self):
        got = mutants(
            "a == b\na != b\na < b\na <= b\na > b\na >= b\na is b\na is not b\na in b\na not in b\n",
            "negate_conditional",
        )
        self.assertEqual(
            [(g[3], g[4]) for g in got],
            [
                ("==", "!="),
                ("!=", "=="),
                ("<", ">="),
                ("<=", ">"),
                (">", "<="),
                (">=", "<"),
                ("is", "is not"),
                ("is not", "is"),
                ("in", "not in"),
                ("not in", "in"),
            ],
        )

    def test_two_token_operators_keep_their_exact_text(self):
        got = mutants("x = (a is   not b)\ny = (a not\n     in b)\n", "negate_conditional")
        self.assertEqual(
            [(g[0], g[1], g[3]) for g in got],
            [(1, 8, "is   not"), (2, 8, "not\n     in")],
        )

    def test_comment_between_the_two_tokens(self):
        got = mutants("x = (a is  # why\n     not b)\n", "negate_conditional")
        self.assertEqual([(g[1], g[3], g[4]) for g in got], [(8, "is  # why\n     not", "is")])

    def test_chained_comparison_yields_one_mutant_per_token(self):
        got = mutants("ok = a < b <= c\n")
        self.assertEqual(
            [(g[1], g[2], g[3]) for g in got],
            [(8, "boundary", "<"), (8, "negate_conditional", "<"), (12, "boundary", "<="), (12, "negate_conditional", "<=")],
        )

    def test_not_in_operand_is_not_the_operator(self):
        got = mutants("x = a not in (not b)\n")
        self.assertEqual(
            [(g[1], g[2], g[3]) for g in got],
            [(7, "negate_conditional", "not in"), (15, "remove_not", "not ")],
        )


class LogicalAndUnaryTests(unittest.TestCase):
    def test_logical(self):
        got = mutants("x = a and b and c\ny = a or b\n", "logical")
        self.assertEqual(
            got,
            [(1, 7, "logical", "and", "or"), (1, 13, "logical", "and", "or"), (2, 7, "logical", "or", "and")],
        )

    def test_remove_not_takes_the_following_whitespace(self):
        got = mutants("a = not x\nb = not  y\nc = not(z)\nd = (not\n     w)\n", "remove_not")
        self.assertEqual(
            [(g[0], g[1], g[3], g[4]) for g in got],
            [(1, 5, "not ", ""), (2, 5, "not  ", ""), (3, 5, "not", ""), (4, 6, "not", "")],
        )

    def test_invert_negative(self):
        got = mutants("a = -x\nb = a - -1\nc = (-y)\n", "invert_negative")
        self.assertEqual([(g[0], g[1], g[3]) for g in got], [(1, 5, "-"), (2, 9, "-"), (3, 6, "-")])

    def test_invert_negative_never_in_annotations(self):
        got = mutants("def f(a: Literal[-1] = -1) -> Literal[-2]:\n    pass\n", "invert_negative")
        self.assertEqual([(g[0], g[1]) for g in got], [(1, 24)])

    def test_removal_that_would_join_identifiers_leaves_a_space(self):
        # `return-x` without the minus must not become the name `returnx`.
        src = "def f(x, y):\n    if-x:\n        return-1\n    z = not-y\n    w = x in-y\n    return-é\n"
        source, sites = sites_of(src)
        removals = [s for s in sites if s.operator in ("invert_negative", "remove_not")]
        self.assertEqual(
            [(s.line, s.operator, s.original, s.replacement) for s in removals],
            [
                (2, "invert_negative", "-", " "),
                (3, "invert_negative", "-", " "),
                (4, "remove_not", "not", ""),
                (4, "invert_negative", "-", " "),
                (5, "invert_negative", "-", " "),
                (6, "invert_negative", "-", " "),
            ],
        )
        expected = {
            2: "    if x:",
            3: "        return 1",
            4: ("    z = -y", "    z = not y"),
            5: "    w = x in y",
            6: "    return é",
        }
        seen_line_4 = []
        for site in removals:
            mutated = source.splice(site.start, site.end, site.replacement)
            ast.parse(mutated)  # still valid Python
            line = mutated.splitlines()[site.line - 1]
            if site.line == 4:
                seen_line_4.append(line)
            else:
                self.assertEqual(line, expected[site.line])
        self.assertEqual(tuple(seen_line_4), expected[4])

    def test_removal_next_to_punctuation_or_space_leaves_nothing(self):
        got = mutants("a = -x\nb = f(-x)\nc = 2**-x\nd = not x\ne = (not(x))\nf = - x\n")
        self.assertEqual({g[4] for g in got if g[2] in ("invert_negative", "remove_not")}, {""})

    def test_remove_not_cannot_join_identifiers(self):
        # `not` is a keyword token, so an identifier character never touches it
        # on the left; its removal keeps whatever separated the two sides.
        for src in ("r = x or not y\n", "r = [not y]\n", "r = 1 if not y else 2\n", "r = (not\n     y)\n"):
            source, sites = sites_of(src)
            site = next(s for s in sites if s.operator == "remove_not")
            self.assertEqual(site.replacement, "")
            ast.parse(source.splice(site.start, site.end, site.replacement))

    def test_unary_plus_and_invert_are_not_mutated(self):
        self.assertEqual(mutants("a = +x\nb = ~x\n"), [])

    def test_increment_produces_nothing_in_python(self):
        self.assertEqual(mutants("x += 1\n", "increment"), [])
        self.assertIn("increment", OPERATOR_IDS)


class RemoveCallTests(unittest.TestCase):
    def test_call_statements(self):
        src = (
            "async def f():\n"
            "    foo()\n"
            "    await bar(1)\n"
            "    obj.method(1,\n"
            "               2)\n"
            "    (paren())\n"
        )
        got = mutants(src, "remove_call")
        self.assertEqual(
            got,
            [
                (2, 5, "remove_call", "foo()", "pass"),
                (3, 5, "remove_call", "await bar(1)", "pass"),
                (4, 5, "remove_call", "obj.method(1,\n               2)", "pass"),
                (6, 5, "remove_call", "(paren())", "pass"),
            ],
        )

    def test_one_line_bodies_are_statement_lists_too(self):
        got = mutants("if x: foo(); bar()\n", "remove_call")
        self.assertEqual([(g[1], g[3]) for g in got], [(7, "foo()"), (14, "bar()")])

    def test_calls_whose_result_is_used_are_kept(self):
        self.assertEqual(mutants("x = foo()\nreturn_value = foo() + 1\nfoo() or bar()\n", "remove_call"), [])

    def test_logging_printing_and_delegation_are_kept(self):
        src = (
            "print('x')\n"
            "logging.info('x')\n"
            "logging.getLogger(__name__).warning('x')\n"
            "logger.debug('x')\n"
            "log.error('x')\n"
            "self.logger.info('x')\n"
            "self._log.debug('x')\n"
            "LOGGER.warning('x')\n"
            "super().__init__()\n"
            "super(C, self).__init__(1)\n"
            "Base.__init__(self)\n"
            "super()\n"
        )
        self.assertEqual(mutants(src, "remove_call"), [])

    def test_other_calls_on_attributes_are_removed(self):
        got = mutants("self.save()\nitems[0].clear()\nfactory()()\nsuper().save()\n", "remove_call")
        self.assertEqual([g[3] for g in got], ["self.save()", "items[0].clear()", "factory()()", "super().save()"])


class SkippedTextTests(unittest.TestCase):
    def test_comments_strings_and_docstrings(self):
        src = '"""a + b and not c"""\n# x = a + b\ns = "a < b"\n'
        self.assertEqual(mutants(src), [])

    def test_f_string_internals_are_skipped(self):
        self.assertEqual(mutants('s = f"{a + b} {not c} {d < e} {True}"\n'), [])


class PositionTests(unittest.TestCase):
    def test_columns_count_code_points(self):
        # "é" is 2 UTF-8 bytes, "日本" 6, "😀" 4 (and 2 UTF-16 units): the
        # column still counts one per character.
        src = 's = "é"; t = "日本"; u = "😀"; v = a + b\n'
        got = mutants(src, "arithmetic")
        self.assertEqual(got, [(1, 35, "arithmetic", "+", "-")])
        self.assertEqual(src[34], "+")
        self.assertEqual(src.encode("utf-8").index(b"+"), 42)  # the parser's byte column

    def test_offsets_splice_the_right_text(self):
        source, sites = sites_of('x = "日本" if a < b else "😀"; y = not z\n')
        for site in sites:
            self.assertEqual(source.text[site.start : site.end], site.original)
        mutated = source.splice(sites[-1].start, sites[-1].end, sites[-1].replacement)
        self.assertEqual(mutated, 'x = "日本" if a < b else "😀"; y = z\n')

    def test_pep263_latin1_file(self):
        data = "# -*- coding: latin-1 -*-\ns = 'é'; v = a + b\n".encode("latin-1")
        source, sites = sites_of(data)
        self.assertEqual(source.encoding, "iso-8859-1")
        self.assertEqual([(s.line, s.column, s.original) for s in sites], [(2, 16, "+")])
        site = sites[0]
        mutated = source.encode(source.splice(site.start, site.end, site.replacement))
        self.assertEqual(mutated, "# -*- coding: latin-1 -*-\ns = 'é'; v = a - b\n".encode("latin-1"))

    def test_utf8_bom_and_crlf_survive_a_splice(self):
        data = b"\xef\xbb\xbfx = 1\r\ny = a < b\r\n"
        source, sites = sites_of(data)
        site = next(s for s in sites if s.operator == "boundary")
        self.assertEqual((site.line, site.column), (2, 7))
        mutated = source.encode(source.splice(site.start, site.end, site.replacement))
        self.assertEqual(mutated, b"\xef\xbb\xbfx = 1\r\ny = a <= b\r\n")

    def test_bare_cr_line_endings(self):
        _, sites = sites_of(b"x = 1\ry = a + b\r")
        self.assertEqual([(s.line, s.column) for s in sites], [(2, 7)])

    def test_form_feed_is_not_a_line_break(self):
        source = SourceFile(b"x = 1\n\x0cy = a + b\n", "t.py")
        self.assertEqual(source.line_count, 3)
        self.assertEqual(source.line(2), "\x0cy = a + b")

    def test_undecodable_file_is_unreadable(self):
        with self.assertRaises(SlopguardError) as ctx:
            SourceFile(b"x = '\xff'\n", "bad.py")
        self.assertEqual(ctx.exception.code, ERR_UNREADABLE_FILE)

    def test_unknown_declared_encoding_is_unreadable(self):
        with self.assertRaises(SlopguardError) as ctx:
            SourceFile(b"# -*- coding: no-such-codec -*-\nx = 1\n", "bad.py")
        self.assertEqual(ctx.exception.code, ERR_UNREADABLE_FILE)

    def test_text_that_does_not_encode_back_is_unreadable(self):
        # cp932 has two byte sequences for "≒"; the NEC one decodes but encodes
        # back to the JIS one, so a mutant could not keep the other bytes intact.
        data = b"# -*- coding: cp932 -*-\ns = '\x87\x90'\n"
        with self.assertRaises(SlopguardError) as ctx:
            SourceFile(data, "odd.py")
        self.assertEqual(ctx.exception.code, ERR_UNREADABLE_FILE)


class IdAndSortTests(unittest.TestCase):
    def test_id_format(self):
        self.assertEqual(mutant_id("pkg/a.py", 3, 7, "boundary"), "pkg/a.py:3:7:boundary")
        site = MutantSite("pkg/a.py", 3, 7, "boundary", "<", "<=", 10, 11)
        self.assertEqual(site.id, "pkg/a.py:3:7:boundary")

    def test_ids_are_unique(self):
        _, sites = sites_of("if a < b and not c:\n    foo(a - -b, True)\n")
        ids = [s.id for s in sites]
        self.assertEqual(len(ids), len(set(ids)))

    def test_sort_order_file_line_column_operator(self):
        def site(file, line, column, operator):
            return MutantSite(file, line, column, operator, "x", "y", 0, 1)

        unsorted = [
            site("b.py", 1, 1, "logical"),
            site("a.py", 2, 1, "logical"),
            site("a.py", 1, 5, "boundary"),
            site("a.py", 1, 5, "arithmetic"),
            site("a.py", 1, 2, "remove_not"),
            site("a/z.py", 9, 9, "logical"),
            site("é.py", 1, 1, "logical"),
        ]
        ordered = [(s.file, s.line, s.column, s.operator) for s in sorted(unsorted, key=site_sort_key)]
        self.assertEqual(
            ordered,
            [
                ("a.py", 1, 2, "remove_not"),
                ("a.py", 1, 5, "arithmetic"),
                ("a.py", 1, 5, "boundary"),
                ("a.py", 2, 1, "logical"),
                ("a/z.py", 9, 9, "logical"),  # "." (0x2E) sorts before "/" (0x2F)
                ("b.py", 1, 1, "logical"),
                ("é.py", 1, 1, "logical"),
            ],
        )


class ParseOperatorsTests(unittest.TestCase):
    def test_default_is_every_operator(self):
        self.assertEqual(parse_operators([]), list(OPERATOR_IDS))
        self.assertEqual(parse_operators([" , "]), list(OPERATOR_IDS))

    def test_comma_separated_and_repeated_values_accumulate_sorted(self):
        self.assertEqual(
            parse_operators(["remove_call, boundary", "logical", "boundary"]),
            ["boundary", "logical", "remove_call"],
        )

    def test_unknown_id_is_invalid_argument(self):
        with self.assertRaises(SlopguardError) as ctx:
            parse_operators(["boundary,nope"])
        self.assertEqual(ctx.exception.code, ERR_INVALID_ARGUMENT)
        self.assertIn("unknown operator(s): nope", ctx.exception.message)
        self.assertIn("--operators", ctx.exception.message)

    def test_ids_are_sorted_and_snake_case(self):
        self.assertEqual(list(OPERATOR_IDS), sorted(OPERATOR_IDS))
        for op in OPERATOR_IDS:
            self.assertRegex(op, r"^[a-z]+(_[a-z]+)*$")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
