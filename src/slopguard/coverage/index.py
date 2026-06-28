"""A fast per-line lookup over a parsed coverage.py report.

Built once after the test run and queried per method by the aggregator.
coverage.py reports file paths as the machine's view of the source tree; our
analyzer reports paths relative to the analysis root, which the aggregator
resolves to absolute before querying. We additionally keep a basename map to
fall back on when paths don't match exactly (CI checkouts vs. local clones,
symlinks) — picking the candidate sharing the longest path suffix.
"""

from __future__ import annotations

import os
from typing import Dict, List, Optional

from .report import FileCoverageData


class _IndexedFile:
    __slots__ = ("abs_path", "basename", "line_hits", "executable_lines", "covered_lines")

    def __init__(self, abs_path: str, line_hits: Dict[int, bool]) -> None:
        self.abs_path = abs_path
        self.basename = os.path.basename(abs_path)
        self.line_hits = line_hits
        self.executable_lines = len(line_hits)
        self.covered_lines = sum(1 for hit in line_hits.values() if hit)


class CoverageIndex:
    def __init__(self, coverage_map: Dict[str, FileCoverageData], project_root: str) -> None:
        self._by_abs: Dict[str, _IndexedFile] = {}
        self._by_basename: Dict[str, List[_IndexedFile]] = {}
        self.total_executable_lines = 0
        self.total_covered_lines = 0
        for name, data in coverage_map.items():
            line_hits: Dict[int, bool] = {}
            for ln in data.missing_lines:
                line_hits[ln] = line_hits.get(ln, False)
            for ln in data.executed_lines:
                line_hits[ln] = True
            abs_path = _resolve_disk_path(name, project_root)
            indexed = _IndexedFile(abs_path, line_hits)
            self.total_executable_lines += indexed.executable_lines
            self.total_covered_lines += indexed.covered_lines
            self._by_abs[abs_path] = indexed
            self._by_basename.setdefault(indexed.basename, []).append(indexed)

    def file_count(self) -> int:
        return len(self._by_abs)

    def method_coverage(self, absolute_path: str, line: int, end_line: int) -> Optional[float]:
        f = self._lookup(absolute_path)
        if f is None:
            return None
        executable = 0
        covered = 0
        for ln, hit in f.line_hits.items():
            if ln < line or ln > end_line:
                continue
            executable += 1
            if hit:
                covered += 1
        if executable == 0:
            return None
        return covered / executable * 100

    def file_coverage(self, absolute_path: str) -> Optional[float]:
        f = self._lookup(absolute_path)
        if f is None or f.executable_lines == 0:
            return None
        return f.covered_lines / f.executable_lines * 100

    def _lookup(self, absolute_path: str) -> Optional[_IndexedFile]:
        direct = self._by_abs.get(absolute_path)
        if direct is not None:
            return direct
        candidates = self._by_basename.get(os.path.basename(absolute_path), [])
        if len(candidates) == 1:
            return candidates[0]
        best: Optional[_IndexedFile] = None
        best_overlap = 0
        for c in candidates:
            overlap = _shared_suffix_len(absolute_path, c.abs_path)
            if overlap > best_overlap:
                best = c
                best_overlap = overlap
        return best


def _resolve_disk_path(name: str, project_root: str) -> str:
    if os.path.isabs(name):
        return os.path.normpath(name)
    return os.path.normpath(os.path.join(project_root, name))


def _shared_suffix_len(a: str, b: str) -> int:
    count = 0
    ai, bi = len(a) - 1, len(b) - 1
    while ai >= 0 and bi >= 0 and a[ai] == b[bi]:
        count += 1
        ai -= 1
        bi -= 1
    return count
