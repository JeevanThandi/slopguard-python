"""Data models for ``mutate``. Pure data plus the scoring maths — no I/O."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

# Mutant statuses, shared with every slopguard port.
#   killed        — the tests failed with the mutant in place (good).
#   survived      — the tests still passed: no test checks this behaviour.
#   timeout       — the test run exceeded the timeout; counted as killed.
#   no_coverage   — no test executes the mutated line, so it was not run.
#   compile_error — the mutant does not compile; excluded from the score.
#   ignored       — switched off by a ``slopguard-ignore-mutant`` marker.
#   pending       — listed by ``--dry-run`` (or when nothing runs), never run.
STATUS_KILLED = "killed"
STATUS_SURVIVED = "survived"
STATUS_TIMEOUT = "timeout"
STATUS_NO_COVERAGE = "no_coverage"
STATUS_COMPILE_ERROR = "compile_error"
STATUS_IGNORED = "ignored"
STATUS_PENDING = "pending"

# Version of the mutation report's JSON shape (independent of the CRAP
# report's schema version; ``reportType`` tells the two apart).
MUTATION_SCHEMA_VERSION = "1"


@dataclass(frozen=True)
class MutantSite:
    """One planned source change, before any test run.

    ``start``/``end`` index the decoded source text (code points); ``line`` and
    ``column`` are 1-based, with ``column`` counted in Unicode code points.
    """

    file: str  # path relative to the source root, forward slashes
    line: int
    column: int
    operator: str
    original: str  # the exact text the mutant replaces
    replacement: str  # the exact text written in its place
    start: int
    end: int

    @property
    def id(self) -> str:
        return mutant_id(self.file, self.line, self.column, self.operator)


@dataclass(frozen=True)
class PlannedMutant:
    """A mutant plus everything known about it before any test run."""

    site: MutantSite
    method: Optional[str]  # innermost enclosing method, None for top-level code
    ignored: bool  # switched off by a slopguard-ignore-mutant marker


def mutant_id(file: str, line: int, column: int, operator: str) -> str:
    """Stable cross-port identifier: ``<file>:<line>:<column>:<operator>``."""
    return f"{file}:{line}:{column}:{operator}"


def site_sort_key(site: MutantSite) -> Tuple[str, int, int, str]:
    """Report order: file, then line, column and operator id. Python compares
    strings by code point, which is the byte-wise order of their UTF-8 form."""
    return (site.file, site.line, site.column, site.operator)


def mutation_score(killed: int, timed_out: int, survived: int, no_coverage: int) -> Optional[float]:
    """``(killed + timedOut) / (killed + timedOut + survived + noCoverage) × 100``,
    unrounded, or ``None`` when no mutant counts towards the score."""
    detected = killed + timed_out
    scored = detected + survived + no_coverage
    if scored == 0:
        return None
    return detected / scored * 100


def summarize(mutants: Sequence[Dict[str, Any]], file_count: int) -> Dict[str, Any]:
    """The ``summary`` block of the report. The counts always sum to
    ``mutantCount``."""

    def count(status: str) -> int:
        return sum(1 for m in mutants if m["status"] == status)

    killed = count(STATUS_KILLED)
    timed_out = count(STATUS_TIMEOUT)
    survived = count(STATUS_SURVIVED)
    no_coverage = count(STATUS_NO_COVERAGE)
    return {
        "fileCount": file_count,
        "mutantCount": len(mutants),
        "killed": killed,
        "survived": survived,
        "timedOut": timed_out,
        "noCoverage": no_coverage,
        "compileErrors": count(STATUS_COMPILE_ERROR),
        "ignored": count(STATUS_IGNORED),
        "pending": count(STATUS_PENDING),
        "mutationScore": mutation_score(killed, timed_out, survived, no_coverage),
    }


def mutant_entries(planned: Sequence[PlannedMutant], statuses: Sequence[str]) -> List[Dict[str, Any]]:
    """The report's ``mutants`` list (camelCase keys, one entry per mutant)."""
    return [
        {
            "id": p.site.id,
            "file": p.site.file,
            "line": p.site.line,
            "column": p.site.column,
            "operator": p.site.operator,
            "original": p.site.original,
            "replacement": p.site.replacement,
            "method": p.method,
            "status": status,
        }
        for p, status in zip(planned, statuses)
    ]
