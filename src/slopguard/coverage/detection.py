"""Project-root discovery and test-runner detection.

The analog of slopguard-swift discovering an xcodebuild scheme and
slopguard-typescript detecting vitest/jest. slopguard-python auto-finds the
project's tests: it prefers ``pytest`` when the project shows pytest signals and
otherwise falls back to ``unittest`` (stdlib, always available), so a bare
``slopguard-python`` in a project root just works.
"""

from __future__ import annotations

import os
from typing import Tuple

# Test runners slopguard-python can drive itself.
RUNNER_PYTEST = "pytest"
RUNNER_UNITTEST = "unittest"
SUPPORTED_RUNNERS = (RUNNER_PYTEST, RUNNER_UNITTEST)

# Files that mark a Python project root, in priority order.
_ROOT_MARKERS = ("pyproject.toml", "setup.py", "setup.cfg", "tox.ini", ".git")


def discover_project_root(searching_from: str) -> Tuple[str, bool]:
    """Walk up from a source path to the nearest project root — the directory
    the test run should execute from. Returns ``(root, found)``."""
    path = os.path.abspath(searching_from)
    if not os.path.isdir(path):
        path = os.path.dirname(path)
    for _ in range(64):
        for marker in _ROOT_MARKERS:
            if os.path.exists(os.path.join(path, marker)):
                return path, True
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return "", False


def detect_runner(project_root: str) -> str:
    """Detect which test runner the project uses. Prefers ``pytest`` when its
    signals are present; otherwise ``unittest`` (the universal stdlib fallback).

    pytest signals: a ``pytest.ini`` / ``conftest.py``, a ``[tool:pytest]`` block
    in ``setup.cfg``, a ``[tool.pytest.ini_options]`` block in ``pyproject.toml``,
    or ``pytest`` named in the project's dependency/requirements files.
    """
    if _has_pytest_signals(project_root):
        return RUNNER_PYTEST
    return RUNNER_UNITTEST


def _has_pytest_signals(project_root: str) -> bool:
    if os.path.exists(os.path.join(project_root, "pytest.ini")):
        return True
    if os.path.exists(os.path.join(project_root, "conftest.py")):
        return True
    if _file_contains(os.path.join(project_root, "setup.cfg"), "[tool:pytest]"):
        return True
    if _file_contains(os.path.join(project_root, "pyproject.toml"), "[tool.pytest"):
        return True
    if _file_contains(os.path.join(project_root, "pyproject.toml"), "pytest"):
        return True
    for req in ("requirements.txt", "requirements-dev.txt", "dev-requirements.txt"):
        if _file_contains(os.path.join(project_root, req), "pytest"):
            return True
    return False


def _file_contains(path: str, needle: str) -> bool:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return needle in fh.read()
    except OSError:
        return False
    except UnicodeDecodeError:
        return False
