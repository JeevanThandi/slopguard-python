"""The coverage lookup contract the aggregator queries per method.

Kept separate from the coverage subsystem so :mod:`slopguard.aggregator` stays
pure (no subprocesses, no I/O) and usable in no-coverage modes.
"""

from __future__ import annotations

from typing import Optional

try:  # Protocol is 3.8+, but guard for safety.
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore


class CoverageProvider(Protocol):
    def method_coverage(self, absolute_path: str, line: int, end_line: int) -> Optional[float]:
        """Line coverage in [0, 100] for ``[line, end_line]``, or ``None`` if the
        file is unknown or no executable line falls in the span."""
        ...

    def file_coverage(self, absolute_path: str) -> Optional[float]:
        """Whole-file line coverage in [0, 100], or ``None`` if unknown."""
        ...
