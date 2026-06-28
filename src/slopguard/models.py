"""Analysis-phase data models.

These hold the pure-syntactic output of the AST analyzer. Coverage is joined
later by the aggregator, so these types stay useful in no-coverage modes. The
final report (methods/types/summary) is a plain ``dict`` built by
:mod:`slopguard.aggregator` — that dict *is* the schema-2 JSON model.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

# MethodKind values. Python has named ``def``s rather than Go's free
# functions/methods split, so the set is richer: constructors (``__init__``),
# property getters/setters, plain methods, and free functions. ``lambda``s do
# not get their own entry — their branches fold into the enclosing method with a
# cognitive nesting bump.
KIND_FUNCTION = "function"
KIND_METHOD = "method"
KIND_CONSTRUCTOR = "constructor"
KIND_GETTER = "getter"
KIND_SETTER = "setter"

# TypeKind values. Python's only declared type is the class (``Enum`` and
# friends are classes with a base, not a keyword).
KIND_CLASS = "class"


@dataclass
class MethodMetric:
    """Pure-syntactic analysis output for one function or method."""

    name: str
    qualified_name: str
    type_name: Optional[str]
    kind: str
    file: str
    start_line: int
    end_line: int
    complexity: int  # cyclomatic (McCabe), base 1
    cognitive_complexity: int  # SonarSource 2023 spec, base 0
    weighted_complexity: float = 0.0  # sqrt(complexity * cognitive_complexity)


@dataclass
class TypeDecl:
    """A lightweight record of a class declaration.

    ``name`` is the lexically qualified class name (e.g. ``Outer.Inner``), so a
    type's members are gathered by lexical nesting — matching the Swift, Kotlin
    and TypeScript siblings (Go differs: it attaches methods by receiver).
    """

    kind: str
    name: str
    file: str
    start_line: int
    end_line: int


@dataclass
class FileReport:
    """The pure-syntactic result for a single source file."""

    path: str
    methods: List[MethodMetric] = field(default_factory=list)
    types: List[TypeDecl] = field(default_factory=list)


def method_id(file: str, qualified_name: str, start_line: int) -> str:
    """Stable cross-tool identifier: ``relative/path.py#Qualified.Name@line``."""
    return f"{file}#{qualified_name}@{start_line}"


def type_id(file: str, name: str, start_line: int) -> str:
    """Stable identifier for a type declaration."""
    return f"{file}#{name}@{start_line}"
