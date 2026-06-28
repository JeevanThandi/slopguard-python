"""The CRAP (Change Risk Anti-Patterns) formula.

    wCRAP(m) = comp(m)^2 * (1 - cov(m)/100)^3 + comp(m)

Where:
  * ``comp`` is the complexity weighting fed in. Since schema 2, slopguard feeds
    ``weighted_complexity = sqrt(cyclomatic * cognitive)`` so the score reflects
    both raw branching (cyclomatic) and human-perceived difficulty (cognitive).
    The formula itself is metric-agnostic.
  * ``cov`` is the line coverage percentage in [0, 100].

Interpretation:
  * Fully covered code (cov = 100) collapses to ``comp`` — complexity alone.
  * Untested code (cov = 0) penalises quadratically: ``comp^2 + comp``.
  * The cubed coverage factor sharply rewards even partial test coverage.
"""

from __future__ import annotations

import math
from typing import List, NamedTuple

# Default threshold above which a method/type is considered "crappy", matching
# the original CRAP paper.
DEFAULT_CRAP_THRESHOLD = 30.0


def crap_score(complexity: float, coverage_percent: float) -> float:
    """CRAP score for a single unit of code.

    The complexity weighting is clamped to >= 0 and coverage to [0, 100]; the
    result is always >= 0.
    """
    comp = max(0.0, complexity)
    cov = max(0.0, min(100.0, coverage_percent))
    cov_factor = 1 - cov / 100
    return comp * comp * (cov_factor * cov_factor * cov_factor) + comp


class CrapAggregate(NamedTuple):
    """Three useful views over a collection of per-method CRAP scores."""

    sum: float
    max: float
    method_count: int


def aggregate_crap(scores: List[float]) -> CrapAggregate:
    """Reduce a list of method CRAP values into a :class:`CrapAggregate`."""
    total = 0.0
    worst = 0.0
    for s in scores:
        total += s
        if s > worst:
            worst = s
    return CrapAggregate(sum=total, max=worst, method_count=len(scores))


def weighted_complexity(cyclomatic: int, cognitive: int) -> float:
    """``sqrt(cyclomatic * cognitive)`` — the value fed into the CRAP formula
    since schema 2. Clamped so negative inputs never produce NaN."""
    cyc = max(0.0, float(cyclomatic))
    cog = max(0.0, float(cognitive))
    return math.sqrt(cyc * cog)
