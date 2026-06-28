"""Join per-file complexity output with optional coverage into the final report.

The report is a plain ``dict`` with camelCase keys — it *is* the schema-2 JSON
model, byte-compatible with the Go/Kotlin/Swift/TypeScript siblings. Unlike the
Go port (which attaches methods to types by receiver), Python — like Swift,
Kotlin and TypeScript — gathers a type's members by **lexical nesting**: a
method's owning class is its enclosing ``class`` declaration.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .crap import aggregate_crap, crap_score, weighted_complexity
from .coverage_provider import CoverageProvider
from .models import FileReport, method_id, type_id
from .version import SCHEMA_VERSION, TOOL_NAME, VERSION

# Attached to every report so downstream consumers know the crap-derived fields
# are driven by the weighted blend, not raw cyclomatic. Identical text to the
# siblings for cross-tool consistency.
_SCHEMA_TWO_NOTE = (
    "Score is wCRAP (weighted CRAP) since schema 2: complexity input is "
    "weightedComplexity = sqrt(cyclomatic × cognitive), not raw cyclomatic. "
    "Both raw metrics ship under `complexity` (cyclomatic, McCabe) and "
    "`cognitiveComplexity` (SonarSource 2023); the score itself is reported "
    "under the existing `crap` field for schema continuity. Recursion "
    "increment is deferred (known undercount vs Sonar parity)."
)


def aggregate(
    file_reports: List[FileReport],
    source_root: str,
    threshold: float,
    coverage: Optional[CoverageProvider] = None,
    coverage_data_path: Optional[str] = None,
    notes: Optional[List[str]] = None,
    generated_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Produce the final schema-2 report dict."""
    if not threshold:
        threshold = 30.0
    root_abs = os.path.abspath(source_root)
    coverage_available = coverage is not None

    methods: List[Dict[str, Any]] = []
    total_complexity = 0
    total_cognitive = 0
    total_weighted = 0.0
    total_covered = 0.0
    total_executable = 0.0

    for fr in file_reports:
        abs_file = _absolutize(root_abs, fr.path)
        for m in fr.methods:
            cov = _coverage_for(coverage, abs_file, m.start_line, m.end_line)
            crap = crap_score(m.weighted_complexity, cov)
            executable = float(max(0, m.end_line - m.start_line + 1))
            methods.append(
                {
                    "id": method_id(m.file, m.qualified_name, m.start_line),
                    "file": m.file,
                    "line": m.start_line,
                    "endLine": m.end_line,
                    "typeName": m.type_name,
                    "name": m.name,
                    "qualifiedName": m.qualified_name,
                    "kind": m.kind,
                    "complexity": m.complexity,
                    "cognitiveComplexity": m.cognitive_complexity,
                    "weightedComplexity": m.weighted_complexity,
                    "coverage": cov,
                    "crap": crap,
                    "isCrappy": crap > threshold,
                }
            )
            total_complexity += m.complexity
            total_cognitive += m.cognitive_complexity
            total_weighted += m.weighted_complexity
            total_covered += executable * cov / 100
            total_executable += executable

    types = _aggregate_types(file_reports, methods, threshold)

    methods.sort(key=lambda x: x["crap"], reverse=True)
    types.sort(key=lambda x: x["aggregatedCrap"], reverse=True)

    crappy_methods = sum(1 for m in methods if m["isCrappy"])
    crappy_types = sum(1 for t in types if t["isCrappy"])

    count = len(methods)
    avg_crap = 0.0
    max_crap = 0.0
    if count:
        avg_crap = sum(m["crap"] for m in methods) / count
        max_crap = methods[0]["crap"]

    weighted_cov: Optional[float] = None
    if coverage_available:
        weighted_cov = (total_covered / total_executable * 100) if total_executable > 0 else 0.0

    when = (generated_at or datetime.now(timezone.utc)).astimezone(timezone.utc)
    generated = when.strftime("%Y-%m-%dT%H:%M:%S.") + f"{when.microsecond // 1000:03d}Z"

    all_notes = [_SCHEMA_TWO_NOTE] + list(notes or [])

    return {
        "schemaVersion": SCHEMA_VERSION,
        "tool": TOOL_NAME,
        "toolVersion": VERSION,
        "generatedAt": generated,
        "sourceRoot": root_abs,
        "coverageDataPath": coverage_data_path,
        "threshold": threshold,
        "coverageAvailable": coverage_available,
        "notes": all_notes,
        "summary": {
            "fileCount": len(file_reports),
            "typeCount": len(types),
            "methodCount": count,
            "crappyMethodCount": crappy_methods,
            "crappyTypeCount": crappy_types,
            "averageCrap": avg_crap,
            "maxCrap": max_crap,
            "averageComplexity": _avg(total_complexity, count),
            "averageCognitiveComplexity": _avg(total_cognitive, count),
            "averageWeightedComplexity": _avg(total_weighted, count),
            "weightedCoverage": weighted_cov,
        },
        "methods": methods,
        "types": types,
    }


def _aggregate_types(
    file_reports: List[FileReport],
    methods: List[Dict[str, Any]],
    threshold: float,
) -> List[Dict[str, Any]]:
    """One entry per class, gathering the methods lexically nested in it
    (matched by ``(file, qualified class name)``)."""
    by_owner: Dict[str, List[Dict[str, Any]]] = {}
    for m in methods:
        if m["typeName"] is None:
            continue
        key = _owner_key(m["file"], m["typeName"])
        by_owner.setdefault(key, []).append(m)

    types: List[Dict[str, Any]] = []
    for fr in file_reports:
        for decl in fr.types:
            owned = by_owner.get(_owner_key(decl.file, decl.name), [])
            types.append(_make_type_crap(decl, owned, threshold))
    return types


def _make_type_crap(decl, owned: List[Dict[str, Any]], threshold: float) -> Dict[str, Any]:
    total_complexity = 0
    max_complexity = 0
    total_cognitive = 0
    max_cognitive = 0
    scores: List[float] = []
    for m in owned:
        total_complexity += m["complexity"]
        total_cognitive += m["cognitiveComplexity"]
        max_complexity = max(max_complexity, m["complexity"])
        max_cognitive = max(max_cognitive, m["cognitiveComplexity"])
        scores.append(m["crap"])
    agg = aggregate_crap(scores)
    weighted_cov = _weighted_coverage(owned)
    weighted_total = weighted_complexity(total_complexity, total_cognitive)
    aggregated = crap_score(weighted_total, weighted_cov)
    return {
        "id": type_id(decl.file, decl.name, decl.start_line),
        "file": decl.file,
        "line": decl.start_line,
        "kind": decl.kind,
        "name": decl.name,
        "methodCount": len(owned),
        "totalComplexity": total_complexity,
        "maxComplexity": max_complexity,
        "totalCognitiveComplexity": total_cognitive,
        "maxCognitiveComplexity": max_cognitive,
        "weightedTotalComplexity": weighted_total,
        "weightedCoverage": weighted_cov,
        "sumCrap": agg.sum,
        "maxCrap": agg.max,
        "aggregatedCrap": aggregated,
        "isCrappy": aggregated > threshold or agg.max > threshold,
    }


def _owner_key(file: str, qualified_type: str) -> str:
    return file + "\x00" + qualified_type


def _coverage_for(
    provider: Optional[CoverageProvider], abs_file: str, line: int, end_line: int
) -> float:
    if provider is None:
        return 0.0
    pct = provider.method_coverage(abs_file, line, end_line)
    if pct is not None:
        return pct
    pct = provider.file_coverage(abs_file)
    if pct is not None:
        return pct
    return 0.0


def _weighted_coverage(methods: List[Dict[str, Any]]) -> float:
    total_lines = 0.0
    weighted = 0.0
    for m in methods:
        lines = float(max(1, m["endLine"] - m["line"] + 1))
        total_lines += lines
        weighted += m["coverage"] * lines
    if total_lines == 0:
        return 0.0
    return weighted / total_lines


def _absolutize(root_abs: str, rel: str) -> str:
    if os.path.isabs(rel):
        return rel
    return os.path.join(root_abs, rel)


def _avg(total: float, count: int) -> float:
    if count == 0:
        return 0.0
    return total / count
