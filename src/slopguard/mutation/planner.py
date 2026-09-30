"""Plan the mutants for one source file. Pure — no I/O.

The planner generates the mutants, keeps the requested operators, sorts them,
names each mutant's enclosing method (via the complexity analyzer, so the names
match the CRAP report) and applies ignore markers.
"""

from __future__ import annotations

import ast
import tokenize
from typing import List, Optional, Sequence

from ..complexity import analyze_source
from ..errors import parse_failed
from ..models import MethodMetric
from .ignore import is_ignored, parse_ignore_markers
from .models import PlannedMutant, site_sort_key
from .operators import generate_mutants
from .source import SourceFile


def plan_file(source: SourceFile, reported_path: str, operators: Sequence[str]) -> List[PlannedMutant]:
    """The mutants for ``source``, sorted by line, column and operator id."""
    try:
        tree = ast.parse(source.text, filename=reported_path)
        sites = generate_mutants(source, tree, reported_path)
    except (SyntaxError, ValueError, tokenize.TokenError) as exc:
        raise parse_failed(reported_path, exc)
    methods = analyze_source(source.text, reported_path).methods
    markers = parse_ignore_markers(source.lines())
    kept = sorted((s for s in sites if s.operator in operators), key=site_sort_key)
    return [
        PlannedMutant(
            site=s,
            method=enclosing_method(methods, s.line),
            ignored=is_ignored(markers, s.line, s.operator),
        )
        for s in kept
    ]


def enclosing_method(methods: Sequence[MethodMetric], line: int) -> Optional[str]:
    """Qualified name of the innermost method whose line range contains
    ``line``: the smallest span wins, and on a tie the one that starts later.
    ``None`` for code outside every method."""
    best: Optional[MethodMetric] = None
    for method in methods:
        if line < method.start_line or line > method.end_line:
            continue
        if best is None or _is_innermost(method, best):
            best = method
    return best.qualified_name if best is not None else None


def _is_innermost(candidate: MethodMetric, best: MethodMetric) -> bool:
    candidate_span = candidate.end_line - candidate.start_line
    best_span = best.end_line - best.start_line
    return candidate_span < best_span or (
        candidate_span == best_span and candidate.start_line > best.start_line
    )
