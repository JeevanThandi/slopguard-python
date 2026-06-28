"""Directory enumeration + the analyze-the-whole-tree entry point.

Globs use fnmatch-style semantics where ``*`` matches across path separators
(see :mod:`slopguard.glob`).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import List

from .errors import file_not_found, unreadable_file
from .fileanalyzer import analyze_file, is_python_source
from .glob import matches_any
from .models import FileReport

# Filtered out of every run unless ``--no-default-excludes`` is passed.
#   * Build / dependency / VCS / cache dirs — never product code.
#   * Test code — test files and test dirs. A test's CRAP isn't user-facing
#     risk; analyze it anyway with --no-default-excludes.
#   * Generated code — protobuf/grpc stubs and codegen output.
#   * Reference fixtures used to benchmark the analyzer itself.
DEFAULT_EXCLUDE_GLOBS: List[str] = [
    # Build / dependency / VCS / cache dirs
    "**/.git/**",
    "**/.venv/**",
    "**/venv/**",
    "**/env/**",
    "**/.tox/**",
    "**/.nox/**",
    "**/node_modules/**",
    "**/build/**",
    "**/dist/**",
    "**/.eggs/**",
    "**/*.egg-info/**",
    "**/__pycache__/**",
    "**/.mypy_cache/**",
    "**/.pytest_cache/**",
    "**/.ruff_cache/**",
    "**/site-packages/**",
    # Test code
    "**/test_*.py",
    "**/*_test.py",
    "**/tests/**",
    "**/test/**",
    "**/conftest.py",
    # Generated code
    "**/*_pb2.py",
    "**/*_pb2_grpc.py",
    "**/*.generated.py",
    # Reference fixtures (analyze on demand with an explicit --path).
    "**/sample-apps/**",
    "**/sampleapps/**",
]


@dataclass
class AnalysisOptions:
    """Controls how the directory walk enumerates files."""

    include_globs: List[str] = field(default_factory=list)
    exclude_globs: List[str] = field(default_factory=list)


def default_analysis_options() -> AnalysisOptions:
    """Options with the built-in excludes and no include filter."""
    return AnalysisOptions(exclude_globs=list(DEFAULT_EXCLUDE_GLOBS))


def analyze_tree(root: str, options: AnalysisOptions) -> List[FileReport]:
    """Analyze a directory tree (or a single file) rooted at ``root``.

    Returns one :class:`FileReport` per analyzed file, with ``path`` relative to
    ``root`` (forward-slash, no leading ``./``), sorted by path.
    """
    root_abs = os.path.abspath(root)
    if not os.path.exists(root_abs):
        raise file_not_found(root_abs)

    if os.path.isdir(root_abs):
        root_prefix = root_abs
        files = _enumerate(root_abs, options)
    else:
        root_prefix = os.path.dirname(root_abs)
        files = [root_abs]

    reports = [analyze_file(f, _relativize(f, root_prefix)) for f in files]
    reports.sort(key=lambda r: r.path)
    return reports


def _enumerate(root_path: str, options: AnalysisOptions) -> List[str]:
    results: List[str] = []

    def walk(directory: str) -> None:
        try:
            entries = sorted(os.scandir(directory), key=lambda e: e.name)
        except OSError as exc:
            raise unreadable_file(directory, exc)
        for entry in entries:
            name = entry.name
            if name.startswith("."):
                continue  # skip hidden files/dirs
            abs_path = os.path.join(directory, name)
            rel = _relativize(abs_path, root_path)
            if entry.is_dir(follow_symlinks=False):
                if not matches_any(options.exclude_globs, rel):
                    walk(abs_path)
                continue
            if not entry.is_file(follow_symlinks=False):
                continue  # don't follow symlinks
            if _should_analyze(rel, options):
                results.append(abs_path)

    walk(root_path)
    return results


def _should_analyze(rel: str, options: AnalysisOptions) -> bool:
    if not is_python_source(rel):
        return False
    if matches_any(options.exclude_globs, rel):
        return False
    if options.include_globs and not matches_any(options.include_globs, rel):
        return False
    return True


def _relativize(abs_path: str, root: str) -> str:
    """The forward-slash path of ``abs_path`` under ``root``."""
    if abs_path == root:
        return os.path.basename(abs_path)
    rel = os.path.relpath(abs_path, root)
    return rel.replace(os.sep, "/")
