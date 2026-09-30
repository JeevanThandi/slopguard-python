"""The comment marker that switches mutants off.

Used for equivalent mutants — changes with no observable effect, which no test
can kill::

    if a > best:  # slopguard-ignore-mutant(boundary): equal values keep the same best

* ``slopguard-ignore-mutant`` ignores every mutant on its line.
* ``slopguard-ignore-mutant(boundary,logical)`` ignores only those operators
  (unknown ids are dropped).
* On a line holding only a comment, the marker applies to the next line.

Detection is a plain text search of the source lines, shared with every port.
"""

from __future__ import annotations

from typing import Dict, FrozenSet, Sequence, Tuple, Union

from .operators import OPERATOR_IDS

IGNORE_MARKER = "slopguard-ignore-mutant"

# Line prefixes that make a line comment-only.
COMMENT_PREFIXES: Tuple[str, ...] = ("#",)

# Sentinel scope: the marker switches off every operator.
ALL = "all"

IgnoreMap = Dict[int, Union[str, FrozenSet[str]]]


def parse_ignore_markers(lines: Sequence[str], comment_prefixes: Tuple[str, ...] = COMMENT_PREFIXES) -> IgnoreMap:
    """Ignored operators per 1-based line, found in ``lines`` (index 0 = line
    1). A line maps to :data:`ALL` or to a set of operator ids."""
    markers: IgnoreMap = {}
    for index, text in enumerate(lines):
        at = text.find(IGNORE_MARKER)
        if at < 0:
            continue
        scope = _marker_scope(text[at + len(IGNORE_MARKER) :])
        comment_only = text.lstrip().startswith(comment_prefixes)
        line = index + 2 if comment_only else index + 1
        existing = markers.get(line)
        if existing == ALL or scope == ALL:
            markers[line] = ALL
        else:
            markers[line] = frozenset(existing or ()) | scope
    return markers


def is_ignored(markers: IgnoreMap, line: int, operator: str) -> bool:
    scope = markers.get(line)
    if scope is None:
        return False
    return scope == ALL or operator in scope


def _marker_scope(rest: str) -> Union[str, FrozenSet[str]]:
    """``(a,b)`` right after the marker narrows it to those operators."""
    if not rest.startswith("("):
        return ALL
    close = rest.find(")")
    body = rest[1:] if close < 0 else rest[1:close]
    return frozenset(part.strip() for part in body.split(",") if part.strip() in OPERATOR_IDS)
