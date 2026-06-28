"""Progress reporting.

The sink for human-readable progress chatter from long-running operations
(directory walks, test runs, coverage parsing). It always writes to a side
channel (stderr in the CLI) so it can't pollute the main result stream —
``--json`` consumers piping into ``jq`` stay clean.
"""

from __future__ import annotations

from typing import IO, Optional

# Verbosity levels.
SILENT = 0  # swallow everything (default for library callers)
NORMAL = 1  # phase markers only
VERBOSE = 2  # additionally stream raw subprocess output


class ProgressReporter:
    def __init__(self, out: Optional[IO[str]], verbosity: int) -> None:
        self._out = out
        self._verbosity = verbosity

    @classmethod
    def silent(cls) -> "ProgressReporter":
        """Discard all progress. Use for library calls."""
        return cls(None, SILENT)

    def phase(self, message: str) -> None:
        """Emit a phase marker, prefixed with ``slopguard:`` so it's
        distinguishable from test-runner chatter when both share stderr."""
        if self._out is None or self._verbosity == SILENT:
            return
        self._out.write(f"slopguard: {message}\n")
        self._out.flush()

    def raw(self, chunk: str) -> None:
        """Pass subprocess output through verbatim. Only the verbose reporter
        writes; every other reporter discards."""
        if self._out is None or self._verbosity != VERBOSE:
            return
        self._out.write(chunk)
        self._out.flush()

    @property
    def is_verbose(self) -> bool:
        return self._verbosity == VERBOSE
