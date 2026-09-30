"""Mutation operators and the mutant generator for Python sources.

The operator ids are shared with every slopguard port — in ``--operators``, in
the JSON ``operator`` field and in ignore markers — so keep them in step with
the siblings:

* ``arithmetic`` — ``+``↔``-``, ``*``→``/``, ``/``→``*``, ``%``→``*``,
  ``//``→``*`` and the compound assignments (``+=``↔``-=``, ``*=``↔``/=``,
  ``%=``→``*=``, ``//=``→``*=``). A ``+`` / ``+=`` with a "stringy" operand
  (a string or bytes literal, an f-string, or a ``+`` expression with a stringy
  operand) is concatenation, not arithmetic, and is skipped.
* ``boolean_literal`` — ``True``↔``False``.
* ``boundary`` — ``<``↔``<=``, ``>``↔``>=``.
* ``increment`` — ``++``↔``--``. Python has no such operator, so it yields no
  mutants, but the id stays valid.
* ``invert_negative`` — unary ``-x`` → ``x`` (``return-x`` → ``return x``:
  a removal that would join two identifier characters leaves one space).
* ``logical`` — ``and``↔``or``.
* ``negate_conditional`` — ``==``↔``!=``, ``<``→``>=``, ``<=``→``>``,
  ``>``→``<=``, ``>=``→``<``, ``is``↔``is not``, ``in``↔``not in``.
* ``remove_call`` — a statement that is only a call (optionally awaited)
  becomes ``pass``. Logging, printing and constructor delegation are kept.
* ``remove_not`` — ``not x`` → ``x`` (the keyword and the whitespace after it).

``ast`` gives positions for nodes but not for operator tokens, so each
operator token is found with ``tokenize`` between the positions of its
operands. ``ast`` columns are UTF-8 byte offsets; ``tokenize`` columns and the
report's columns count code points.

Nothing inside a type annotation, a ``type`` alias or an f-string is mutated:
annotations carry no runtime behaviour, and f-string internals tokenize
differently before Python 3.12, so skipping them keeps the mutant set the same
on every supported Python version.
"""

from __future__ import annotations

import ast
import bisect
import tokenize
from typing import Dict, List, Optional, Sequence, Tuple

from ..errors import invalid_argument
from .models import MutantSite
from .source import SourceFile

ARITHMETIC = "arithmetic"
BOOLEAN_LITERAL = "boolean_literal"
BOUNDARY = "boundary"
INCREMENT = "increment"
INVERT_NEGATIVE = "invert_negative"
LOGICAL = "logical"
NEGATE_CONDITIONAL = "negate_conditional"
REMOVE_CALL = "remove_call"
REMOVE_NOT = "remove_not"

# Every operator, sorted. The default when ``--operators`` is not given.
OPERATOR_IDS: Tuple[str, ...] = (
    ARITHMETIC,
    BOOLEAN_LITERAL,
    BOUNDARY,
    INCREMENT,
    INVERT_NEGATIVE,
    LOGICAL,
    NEGATE_CONDITIONAL,
    REMOVE_CALL,
    REMOVE_NOT,
)

# (operator token, replacement) per AST operator class.
_BINARY = {
    ast.Add: ("+", "-"),
    ast.Sub: ("-", "+"),
    ast.Mult: ("*", "/"),
    ast.Div: ("/", "*"),
    ast.Mod: ("%", "*"),
    ast.FloorDiv: ("//", "*"),
}
_COMPOUND = {
    ast.Add: ("+=", "-="),
    ast.Sub: ("-=", "+="),
    ast.Mult: ("*=", "/="),
    ast.Div: ("/=", "*="),
    ast.Mod: ("%=", "*="),
    ast.FloorDiv: ("//=", "*="),
}
_LOGICAL = {ast.And: ("and", "or"), ast.Or: ("or", "and")}

# The token sequence of each comparison operator (``is not`` and ``not in``
# are two tokens), then its boundary and negated replacements.
_COMPARISON_TOKENS = {
    ast.Eq: ("==",),
    ast.NotEq: ("!=",),
    ast.Lt: ("<",),
    ast.LtE: ("<=",),
    ast.Gt: (">",),
    ast.GtE: (">=",),
    ast.Is: ("is",),
    ast.IsNot: ("is", "not"),
    ast.In: ("in",),
    ast.NotIn: ("not", "in"),
}
_BOUNDARY = {ast.Lt: "<=", ast.LtE: "<", ast.Gt: ">=", ast.GtE: ">"}
_NEGATED = {
    ast.Eq: "!=",
    ast.NotEq: "==",
    ast.Lt: ">=",
    ast.LtE: ">",
    ast.Gt: "<=",
    ast.GtE: "<",
    ast.Is: "is not",
    ast.IsNot: "is",
    ast.In: "not in",
    ast.NotIn: "in",
}

# Calls ``remove_call`` never removes: printing and logging (their removal is
# rarely observable), plus ``super(...)`` and ``__init__`` delegation.
_KEPT_CALLS = ("print", "super")
_LOGGER_NAMES = ("logging", "logger", "log")

# Subtrees with no runtime behaviour to mutate (``TemplateStr`` is 3.14+,
# ``TypeAlias`` 3.12+) and per-node fields that hold type annotations.
_SKIPPED_NODES = tuple(
    t for t in (ast.JoinedStr, getattr(ast, "TemplateStr", None), getattr(ast, "TypeAlias", None)) if t is not None
)
_SKIPPED_FIELDS: Dict[type, Tuple[str, ...]] = {
    ast.arg: ("annotation",),
    ast.FunctionDef: ("returns", "type_params"),
    ast.AsyncFunctionDef: ("returns", "type_params"),
    ast.ClassDef: ("type_params",),
    ast.AnnAssign: ("annotation",),
}
_STRING_NODES = tuple(t for t in (ast.JoinedStr, getattr(ast, "TemplateStr", None)) if t is not None)
_MatchSingleton = getattr(ast, "MatchSingleton", None)  # 3.10+
_SKIPPABLE_TOKENS = (tokenize.NL, tokenize.COMMENT)

_Pos = Tuple[int, int]  # (1-based line, 0-based code-point column)


def parse_operators(values: Sequence[str]) -> List[str]:
    """Resolve ``--operators`` values (comma-separated; the flag may repeat)
    into a sorted, de-duplicated list. No ids at all means every operator."""
    ids = [part.strip() for value in values for part in value.split(",")]
    ids = [i for i in ids if i]
    if not ids:
        return list(OPERATOR_IDS)
    unknown = [i for i in ids if i not in OPERATOR_IDS]
    if unknown:
        raise invalid_argument(
            "--operators",
            f"unknown operator(s): {', '.join(unknown)} (expected: {', '.join(OPERATOR_IDS)})",
        )
    return [op for op in OPERATOR_IDS if op in ids]


def generate_mutants(source: SourceFile, tree: ast.AST, reported_path: str) -> List[MutantSite]:
    """Every mutant the operator set defines for ``source`` (parsed as
    ``tree``), in walk order. ``reported_path`` is recorded as each mutant's
    ``file``."""
    return _Generator(source, reported_path).generate(tree)


class _Generator:
    def __init__(self, source: SourceFile, reported_path: str) -> None:
        self.source = source
        self.file = reported_path
        self.tokens = source.tokens()
        self.starts = [t.start for t in self.tokens]
        self.sites: List[MutantSite] = []
        self._stringy: Dict[int, bool] = {}
        self._visitors = {
            ast.BinOp: self._binary,
            ast.AugAssign: self._compound,
            ast.UnaryOp: self._unary,
            ast.BoolOp: self._logical,
            ast.Compare: self._comparison,
            ast.Constant: self._boolean,
            ast.Expr: self._call_statement,
        }
        if _MatchSingleton is not None:
            self._visitors[_MatchSingleton] = self._boolean

    def generate(self, tree: ast.AST) -> List[MutantSite]:
        # An explicit stack: long ``a + b + c + ...`` chains nest deeper than
        # the recursion limit allows.
        stack = [tree]
        while stack:
            node = stack.pop()
            if isinstance(node, _SKIPPED_NODES):
                continue
            visit = self._visitors.get(type(node))
            if visit is not None:
                visit(node)
            stack.extend(_runtime_children(node))
        return self.sites

    # -- operators ------------------------------------------------------------

    def _binary(self, node: ast.BinOp) -> None:
        pair = _BINARY.get(type(node.op))
        if pair is None:
            return
        if isinstance(node.op, ast.Add) and (self._is_stringy(node.left) or self._is_stringy(node.right)):
            return
        self._emit_between(ARITHMETIC, node.left, node.right, (pair[0],), pair[1])

    def _compound(self, node: ast.AugAssign) -> None:
        pair = _COMPOUND.get(type(node.op))
        if pair is None:
            return
        if isinstance(node.op, ast.Add) and self._is_stringy(node.value):
            return
        self._emit_between(ARITHMETIC, node.target, node.value, (pair[0],), pair[1])

    def _logical(self, node: ast.BoolOp) -> None:
        token, replacement = _LOGICAL[type(node.op)]
        for left, right in zip(node.values, node.values[1:]):
            self._emit_between(LOGICAL, left, right, (token,), replacement)

    def _comparison(self, node: ast.Compare) -> None:
        operands = [node.left] + list(node.comparators)
        for i, op in enumerate(node.ops):
            span = self._find(self._end(operands[i]), self._start(operands[i + 1]), _COMPARISON_TOKENS[type(op)])
            if span is None:  # pragma: no cover — defensive: ast positions bracket every operator
                continue
            boundary = _BOUNDARY.get(type(op))
            if boundary is not None:
                self._emit(BOUNDARY, span, boundary)
            self._emit(NEGATE_CONDITIONAL, span, _NEGATED[type(op)])

    def _unary(self, node: ast.UnaryOp) -> None:
        if isinstance(node.op, ast.Not):
            token = self._token_at(self._start(node), "not")
            if token is not None:
                self._emit_removal(REMOVE_NOT, (token.start, self._skip_blanks(token.end)))
        elif isinstance(node.op, ast.USub):
            token = self._token_at(self._start(node), "-")
            if token is not None:
                self._emit_removal(INVERT_NEGATIVE, (token.start, token.end))

    def _boolean(self, node: ast.AST) -> None:
        value = node.value
        if value is not True and value is not False:
            return
        token = self._token_at(self._start(node), "True" if value else "False")
        if token is not None:
            self._emit(BOOLEAN_LITERAL, (token.start, token.end), "False" if value else "True")

    def _call_statement(self, node: ast.Expr) -> None:
        call = node.value
        while isinstance(call, ast.Await):
            call = call.value
        if isinstance(call, ast.Call) and not _is_kept_call(call):
            self._emit(REMOVE_CALL, (self._start(node), self._end(node)), "pass")

    # -- positions and tokens ---------------------------------------------------

    def _start(self, node: ast.AST) -> _Pos:
        return node.lineno, self.source.column_of_byte(node.lineno, node.col_offset)

    def _end(self, node: ast.AST) -> _Pos:
        return node.end_lineno, self.source.column_of_byte(node.end_lineno, node.end_col_offset)

    def _emit_between(self, operator: str, left: ast.AST, right: ast.AST, tokens: Tuple[str, ...], replacement: str) -> None:
        span = self._find(self._end(left), self._start(right), tokens)
        if span is not None:
            self._emit(operator, span, replacement)

    def _find(self, low: _Pos, high: _Pos, strings: Tuple[str, ...]) -> Optional[Tuple[_Pos, _Pos]]:
        """The span of the first token sequence ``strings`` that starts in
        ``[low, high)``. Between two operands only brackets, comments and the
        operator itself can appear, so the first match is the operator."""
        i = bisect.bisect_left(self.starts, low)
        while i < len(self.tokens) and self.starts[i] < high:
            if self.tokens[i].string == strings[0]:
                last = self._follow(i, strings[1:])
                if last is not None:
                    return self.tokens[i].start, self.tokens[last].end
            i += 1
        return None  # pragma: no cover — defensive: ast positions bracket every operator

    def _follow(self, i: int, rest: Tuple[str, ...]) -> Optional[int]:
        """Index of the last token when ``rest`` follows token ``i`` (skipping
        line breaks and comments inside brackets), else ``None``."""
        for expected in rest:
            i += 1
            while i < len(self.tokens) and self.tokens[i].type in _SKIPPABLE_TOKENS:
                i += 1
            if i >= len(self.tokens) or self.tokens[i].string != expected:  # pragma: no cover — defensive
                return None
        return i

    def _token_at(self, pos: _Pos, expected: str) -> Optional[tokenize.TokenInfo]:
        i = bisect.bisect_left(self.starts, pos)
        if i < len(self.tokens) and self.starts[i] == pos and self.tokens[i].string == expected:
            return self.tokens[i]
        return None  # pragma: no cover — defensive: a unary operator or literal starts its node

    def _skip_blanks(self, pos: _Pos) -> _Pos:
        """Move past the spaces and tabs that follow ``pos`` on its line."""
        line, column = pos
        text = self.source.line(line)
        while column < len(text) and text[column] in " \t":
            column += 1
        return line, column

    def _emit_removal(self, operator: str, span: Tuple[_Pos, _Pos]) -> None:
        """Remove a token. The replacement is one space instead of empty text
        when the removal would join two identifier characters: ``return-x``
        must become ``return x``, not the name ``returnx``."""
        start = self.source.offset(*span[0])
        stop = self.source.offset(*span[1])
        text = self.source.text
        before = text[start - 1] if start > 0 else ""
        after = text[stop] if stop < len(text) else ""
        joins = _is_identifier_char(before) and _is_identifier_char(after)
        self._emit(operator, span, " " if joins else "")

    def _emit(self, operator: str, span: Tuple[_Pos, _Pos], replacement: str) -> None:
        (line, column), end = span
        start = self.source.offset(line, column)
        stop = self.source.offset(*end)
        self.sites.append(
            MutantSite(
                file=self.file,
                line=line,
                column=column + 1,
                operator=operator,
                original=self.source.text[start:stop],
                replacement=replacement,
                start=start,
                end=stop,
            )
        )

    def _is_stringy(self, node: ast.AST) -> bool:
        """A string/bytes literal or f-string, or a ``+`` expression with a
        stringy operand (parentheses are not nodes, so they are seen through).
        Memoized and iterative: a long concatenation chain is one deep tree."""
        pending = [node]
        while pending:
            current = pending[-1]
            if id(current) in self._stringy:
                pending.pop()
            elif not (isinstance(current, ast.BinOp) and isinstance(current.op, ast.Add)):
                self._stringy[id(current)] = _is_string_literal(current)
                pending.pop()
            elif id(current.left) in self._stringy and id(current.right) in self._stringy:
                self._stringy[id(current)] = self._stringy[id(current.left)] or self._stringy[id(current.right)]
                pending.pop()
            else:
                pending.extend((current.left, current.right))
        return self._stringy[id(node)]


def _runtime_children(node: ast.AST) -> List[ast.AST]:
    """The child nodes of ``node``, minus the fields that hold type annotations."""
    skipped = _SKIPPED_FIELDS.get(type(node), ())
    children: List[ast.AST] = []
    for name, value in ast.iter_fields(node):
        if name in skipped:
            continue
        if isinstance(value, ast.AST):
            children.append(value)
        elif isinstance(value, list):
            children.extend(item for item in value if isinstance(item, ast.AST))
    return children


def _is_identifier_char(char: str) -> bool:
    """Whether ``char`` can continue a Python identifier (letters, digits,
    ``_`` and their Unicode relatives)."""
    return char != "" and ("_" + char).isidentifier()


def _is_string_literal(node: ast.AST) -> bool:
    if isinstance(node, _STRING_NODES):
        return True
    return isinstance(node, ast.Constant) and isinstance(node.value, (str, bytes))


def _is_kept_call(call: ast.Call) -> bool:
    """``print(...)``, ``super(...)``, ``X.__init__(...)`` and logging calls:
    ``logging.*``, ``logger.*`` and ``log.*``, also when the logger is an
    attribute (``self.logger.info``, ``self._log.debug``, ``LOGGER.warning``)."""
    func = call.func
    if isinstance(func, ast.Name):
        return func.id in _KEPT_CALLS
    if not isinstance(func, ast.Attribute):
        return False
    if func.attr == "__init__":
        return True
    receiver = func.value
    name = receiver.id if isinstance(receiver, ast.Name) else getattr(receiver, "attr", None)
    if name is not None and name.lstrip("_").lower() in _LOGGER_NAMES:
        return True
    root = receiver
    while isinstance(root, (ast.Attribute, ast.Call, ast.Subscript)):
        root = root.func if isinstance(root, ast.Call) else root.value
    return isinstance(root, ast.Name) and root.id in _LOGGER_NAMES
