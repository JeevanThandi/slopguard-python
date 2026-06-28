"""Parse the ``coverage json`` output of coverage.py.

coverage.py's JSON report is the universal Python interchange format — anything
that can run under ``coverage`` (pytest, unittest, nose) produces it — so a
report your CI already generated is supported via ``--coverage-file``. It is to
slopguard-python what an ``.xcresult`` is to slopguard-swift: the single
canonical artifact coverage is read from.
"""

from __future__ import annotations

import json
from typing import Dict, List

from ..errors import coverage_decode_failed


class FileCoverageData:
    """Per-file line data from a coverage.py report."""

    __slots__ = ("executed_lines", "missing_lines")

    def __init__(self, executed_lines: List[int], missing_lines: List[int]) -> None:
        self.executed_lines = executed_lines
        self.missing_lines = missing_lines


def parse_coverage_json(text: str) -> Dict[str, FileCoverageData]:
    """Parse a coverage.py JSON report into ``{path: FileCoverageData}``.

    Paths are taken verbatim from the report (coverage.py emits absolute paths by
    default, relative ones under ``[run] relative_files = true``); the index
    resolves them to disk.
    """
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise coverage_decode_failed(exc)
    if not isinstance(raw, dict):
        raise coverage_decode_failed("top-level coverage JSON is not an object")
    files = raw.get("files")
    result: Dict[str, FileCoverageData] = {}
    if not isinstance(files, dict):
        return result
    for path, data in files.items():
        if not isinstance(data, dict):
            continue
        executed = _int_list(data.get("executed_lines"))
        missing = _int_list(data.get("missing_lines"))
        result[path] = FileCoverageData(executed, missing)
    return result


def _int_list(value: object) -> List[int]:
    if not isinstance(value, list):
        return []
    return [int(v) for v in value if isinstance(v, (int, float))]
