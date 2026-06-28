"""The single-pass Python AST complexity analyzer.

Produces one :class:`MethodMetric` per ``def``/``async def`` and one
:class:`TypeDecl` per ``class``, computing two complexity metrics on a single
walk. The counting rules mirror the Go/Kotlin/Swift/TypeScript siblings so the
numbers are comparable across the whole slopguard family.

Cyclomatic complexity (McCabe). Each function starts at 1. ``+1`` for: ``if``
(and each ``elif``), ``for`` / ``async for``, ``while``, each ``except``
handler, the ternary ``x if c else y``, each boolean operator in an ``and`` /
``or`` chain, each comprehension ``for`` clause and each comprehension ``if``
filter, and each non-wildcard ``match`` case (plus its guard). This mirrors the
established ``mccabe`` counting so the numbers fit the wider Python ecosystem.

Cognitive complexity (SonarSource 2023 spec). Each function starts at 0.
  * B. Structural (``+1 + nesting``, bumps nesting for inner code): ``if`` (head
    of an if/elif chain), ``for`` / ``while``, ``except``, the ternary, the
    whole ``match`` (one increment regardless of case count), and each
    comprehension ``for`` clause.
  * D. Hybrid (``+1`` flat or ``+0``, bumps nesting): a chained ``elif`` and a
    trailing ``else`` each add ``+1`` flat; ``lambda``s add ``+0`` but bump the
    nesting level for their body.
  * C. Fundamental (``+1`` flat, no nesting interaction): each run of like
    boolean operators (Python groups ``a and b and c`` into one node = one run;
    ``a and b or c`` is two nodes = two runs).

Ignored (cognitive ``+0``): the function itself, ``with`` / ``async with``,
``try`` / ``finally`` / ``else`` blocks, ``assert``, ``raise``, ``yield``, and
plain ``return`` / ``break`` / ``continue`` / ``pass`` (early exits per the
spec's "no other jumps cause an increment" rule). Nested ``def``s open their own
entry (named functions are their own unit, like the TypeScript port); only
anonymous ``lambda``s fold into the enclosing method.
"""

from __future__ import annotations

import ast
from typing import List, Optional, Tuple

from .crap import weighted_complexity
from .errors import parse_failed
from .models import (
    KIND_CLASS,
    KIND_CONSTRUCTOR,
    KIND_FUNCTION,
    KIND_GETTER,
    KIND_METHOD,
    KIND_SETTER,
    FileReport,
    MethodMetric,
    TypeDecl,
)

# Optional node types: ``match`` is 3.10+.
_Match = getattr(ast, "Match", None)
_MatchCase = getattr(ast, "match_case", None)


class _MethodFrame:
    __slots__ = (
        "name",
        "qualified_name",
        "type_name",
        "kind",
        "start_line",
        "end_line",
        "complexity",
        "cognitive",
        "nesting",
    )

    def __init__(self, name, qualified_name, type_name, kind, start_line, end_line):
        self.name = name
        self.qualified_name = qualified_name
        self.type_name = type_name
        self.kind = kind
        self.start_line = start_line
        self.end_line = end_line
        self.complexity = 1  # cyclomatic, base 1
        self.cognitive = 0  # cognitive, base 0
        self.nesting = 0


class _Analyzer:
    def __init__(self, file: str) -> None:
        self.file = file
        self.methods: List[MethodMetric] = []
        self.types: List[TypeDecl] = []
        self._method_stack: List[_MethodFrame] = []
        self._scope_stack: List[str] = []  # enclosing class+function names
        self._type_stack: List[str] = []  # qualified names of enclosing classes

        # Nodes that open a new scope and fully manage their own recursion.
        self._scope_dispatch = {
            ast.FunctionDef: self._visit_function,
            ast.AsyncFunctionDef: self._visit_function,
            ast.ClassDef: self._visit_class,
            ast.Lambda: self._visit_lambda,
            ast.ListComp: self._visit_comprehension,
            ast.SetComp: self._visit_comprehension,
            ast.DictComp: self._visit_comprehension,
            ast.GeneratorExp: self._visit_comprehension,
        }
        # Nodes that increment metrics; each returns whether it bumped nesting,
        # then the generic walk recurses children and unwinds the bump.
        self._inc_dispatch = {
            ast.If: self._inc_if,
            ast.For: self._inc_loop,
            ast.AsyncFor: self._inc_loop,
            ast.While: self._inc_loop,
            ast.IfExp: self._inc_ternary,
            ast.BoolOp: self._inc_boolop,
            ast.ExceptHandler: self._inc_except,
        }
        if _Match is not None:
            self._inc_dispatch[_Match] = self._inc_match
        if _MatchCase is not None:
            self._inc_dispatch[_MatchCase] = self._inc_match_case

    def analyze(self, tree: ast.AST) -> Tuple[List[MethodMetric], List[TypeDecl]]:
        for node in ast.iter_child_nodes(tree):
            self._visit(node, tree)
        return self.methods, self.types

    def _visit(self, node: ast.AST, parent: ast.AST) -> None:
        scope = self._scope_dispatch.get(type(node))
        if scope is not None:
            scope(node, parent)
            return
        handler = self._inc_dispatch.get(type(node))
        bumped = handler(node, parent) if handler is not None else False
        for child in ast.iter_child_nodes(node):
            self._visit(child, node)
        self._exit_nesting(bumped)

    # -- scope helpers -----------------------------------------------------

    @property
    def _cur(self) -> Optional[_MethodFrame]:
        return self._method_stack[-1] if self._method_stack else None

    def _bump_cyclomatic(self, amount: int = 1) -> None:
        m = self._cur
        if m is not None:
            m.complexity += amount

    def _bump_cognitive(self, amount: int) -> None:
        if amount <= 0:
            return
        m = self._cur
        if m is not None:
            m.cognitive += amount

    def _enter_nesting(self) -> bool:
        m = self._cur
        if m is not None:
            m.nesting += 1
            return True
        return False

    def _exit_nesting(self, bumped: bool) -> None:
        if bumped and self._cur is not None:
            self._cur.nesting -= 1

    @property
    def _nesting(self) -> int:
        m = self._cur
        return m.nesting if m is not None else 0

    # -- increment handlers (return whether nesting was bumped) ------------

    def _inc_if(self, node: ast.If, parent: ast.AST) -> bool:
        self._bump_cyclomatic()
        if _is_elif(node, parent):
            self._bump_cognitive(1)  # chained elif, flat
        else:
            self._bump_cognitive(1 + self._nesting)  # head if, structural
        if node.orelse and not _orelse_is_elif(node):
            self._bump_cognitive(1)  # trailing plain else
        return self._enter_nesting()

    def _inc_loop(self, node: ast.AST, parent: ast.AST) -> bool:
        self._bump_cyclomatic()
        self._bump_cognitive(1 + self._nesting)
        return self._enter_nesting()

    def _inc_ternary(self, node: ast.IfExp, parent: ast.AST) -> bool:
        self._bump_cyclomatic()
        self._bump_cognitive(1 + self._nesting)
        return self._enter_nesting()

    def _inc_boolop(self, node: ast.BoolOp, parent: ast.AST) -> bool:
        self._bump_cyclomatic(len(node.values) - 1)
        self._bump_cognitive(1)  # one run per node (Python groups like ops)
        return False

    def _inc_except(self, node: ast.AST, parent: ast.AST) -> bool:
        self._bump_cyclomatic()
        self._bump_cognitive(1 + self._nesting)
        return self._enter_nesting()

    def _inc_match(self, node: ast.AST, parent: ast.AST) -> bool:
        # Whole match = one structural increment; cases drive cyclomatic only.
        self._bump_cognitive(1 + self._nesting)
        return self._enter_nesting()

    def _inc_match_case(self, node: ast.AST, parent: ast.AST) -> bool:
        if not _is_wildcard_case(node):
            self._bump_cyclomatic()
        if node.guard is not None:
            self._bump_cyclomatic()
        return False

    # -- scope handlers ----------------------------------------------------

    def _visit_function(self, node: ast.AST, parent: ast.AST) -> None:
        # Decorators and default argument values are evaluated in the enclosing
        # scope, so visit them against the current (outer) frame before opening
        # this function's own frame.
        for dec in node.decorator_list:
            self._visit(dec, node)
        args = node.args
        for d in list(args.defaults) + list(args.kw_defaults):
            if d is not None:
                self._visit(d, node)

        in_class = isinstance(parent, ast.ClassDef)
        type_name = self._type_stack[-1] if in_class and self._type_stack else None
        kind = self._function_kind(node, in_class)
        qualified = ".".join(self._scope_stack + [node.name])
        start, end = _line_range(node)

        frame = _MethodFrame(node.name, qualified, type_name, kind, start, end)
        self._method_stack.append(frame)
        self._scope_stack.append(node.name)
        for stmt in node.body:
            self._visit(stmt, node)
        self._scope_stack.pop()
        self._method_stack.pop()
        self._emit(frame)

    def _visit_class(self, node: ast.ClassDef, parent: ast.AST) -> None:
        for dec in node.decorator_list:
            self._visit(dec, node)
        qualified = ".".join(self._scope_stack + [node.name])
        start, end = _line_range(node)
        self.types.append(
            TypeDecl(kind=KIND_CLASS, name=qualified, file=self.file, start_line=start, end_line=end)
        )
        self._scope_stack.append(node.name)
        self._type_stack.append(qualified)
        for stmt in node.body:
            self._visit(stmt, node)
        self._type_stack.pop()
        self._scope_stack.pop()

    def _visit_lambda(self, node: ast.Lambda, parent: ast.AST) -> None:
        # Anonymous closure — D-Hybrid: +0 score, nesting bump only.
        bumped = self._enter_nesting()
        self._visit(node.body, node)
        self._exit_nesting(bumped)

    def _visit_comprehension(self, node: ast.AST, parent: ast.AST) -> None:
        # Visit generators first to establish loop nesting, then the element
        # expression(s) at the innermost level. Each ``for`` clause is a
        # structural loop; each ``if`` filter is a flat increment.
        bumps = 0
        for gen in node.generators:
            self._bump_cyclomatic()
            self._bump_cognitive(1 + self._nesting)
            self._bump_cyclomatic(len(gen.ifs))
            self._bump_cognitive(len(gen.ifs))
            self._visit(gen.iter, node)  # iterable is outside this loop's body
            if self._enter_nesting():
                bumps += 1
            for cond in gen.ifs:
                self._visit(cond, node)
        if isinstance(node, ast.DictComp):
            self._visit(node.key, node)
            self._visit(node.value, node)
        else:
            self._visit(node.elt, node)
        for _ in range(bumps):
            self._exit_nesting(True)

    def _function_kind(self, node: ast.AST, in_class: bool) -> str:
        if not in_class:
            return KIND_FUNCTION
        if node.name == "__init__":
            return KIND_CONSTRUCTOR
        for dec in node.decorator_list:
            if isinstance(dec, ast.Name) and dec.id == "property":
                return KIND_GETTER
            if isinstance(dec, ast.Attribute) and dec.attr == "setter":
                return KIND_SETTER
        return KIND_METHOD

    def _emit(self, frame: _MethodFrame) -> None:
        self.methods.append(
            MethodMetric(
                name=frame.name,
                qualified_name=frame.qualified_name,
                type_name=frame.type_name,
                kind=frame.kind,
                file=self.file,
                start_line=frame.start_line,
                end_line=frame.end_line,
                complexity=frame.complexity,
                cognitive_complexity=frame.cognitive,
                weighted_complexity=weighted_complexity(frame.complexity, frame.cognitive),
            )
        )


def _is_elif(node: ast.If, parent: ast.AST) -> bool:
    """True when ``node`` is the chained ``elif`` of ``parent`` (the sole
    statement in the parent's ``orelse``). Python's AST can't distinguish
    ``elif`` from ``else: if`` — treating a sole-``If`` ``orelse`` as ``elif`` is
    the standard convention shared with cognitive-complexity tooling."""
    return isinstance(parent, ast.If) and len(parent.orelse) == 1 and parent.orelse[0] is node


def _orelse_is_elif(node: ast.If) -> bool:
    return len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If)


def _is_wildcard_case(case: ast.AST) -> bool:
    """True for a bare ``case _:`` (the default) — a ``MatchAs`` with neither a
    sub-pattern nor a capture name."""
    pat = case.pattern
    MatchAs = getattr(ast, "MatchAs", None)
    return MatchAs is not None and isinstance(pat, MatchAs) and pat.pattern is None and pat.name is None


def _line_range(node: ast.AST) -> Tuple[int, int]:
    start = getattr(node, "lineno", 1)
    end = getattr(node, "end_lineno", None) or start
    return start, end


def analyze_source(src: str, reported_path: str) -> FileReport:
    """Analyze Python source held in memory and return per-method metrics."""
    try:
        tree = ast.parse(src, filename=reported_path)
    except SyntaxError as exc:
        raise parse_failed(reported_path, exc)
    analyzer = _Analyzer(reported_path)
    methods, types = analyzer.analyze(tree)
    return FileReport(path=reported_path, methods=methods, types=types)
