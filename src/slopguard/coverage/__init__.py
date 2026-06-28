"""Coverage subsystem: drive the project's own tests to produce a coverage.py
report, parse it, and join per-method line coverage onto the analysis.

Coverage is treated as an *artifact* slopguard generates — never a user input —
mirroring how slopguard-swift drives ``xcodebuild`` and slopguard-typescript
drives vitest/jest.
"""

from .pipeline import CoverageSource, MODE_AUTO, MODE_NONE, MODE_PREBUILT, run

__all__ = ["CoverageSource", "MODE_AUTO", "MODE_NONE", "MODE_PREBUILT", "run"]
