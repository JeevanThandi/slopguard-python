"""Version metadata for slopguard-python.

``TOOL_NAME`` and ``SCHEMA_VERSION`` are stable identifiers shared with the Go,
Kotlin, Swift, and TypeScript siblings — the JSON report schema is
byte-compatible across all of them. Edit ``VERSION`` for releases.
"""

# Released semantic version of slopguard-python.
VERSION = "0.1.0"

# Stable tool identifier emitted in reports (matches the sibling naming:
# slopguard-go, slopguard-kotlin, slopguard-swift).
TOOL_NAME = "slopguard-python"

# JSON report schema version, shared across every slopguard language port.
SCHEMA_VERSION = "2"
