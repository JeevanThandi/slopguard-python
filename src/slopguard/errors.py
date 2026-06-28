"""Typed, machine-readable errors.

Every failure carries a stable ``code`` so the CLI's ``--json`` error path stays
consumable by agents without pattern-matching on free-text messages. The code
set mirrors the Go/TypeScript siblings (with Python-flavoured runner messages).
"""

from __future__ import annotations

from typing import Dict


# Stable error codes — shared classifiers across the slopguard ports.
ERR_FILE_NOT_FOUND = "file_not_found"
ERR_NOT_A_DIRECTORY = "not_a_directory"
ERR_UNREADABLE_FILE = "unreadable_file"
ERR_PARSE_FAILED = "parse_failed"
ERR_COVERAGE_DATA_MISSING = "coverage_data_missing"
ERR_PROJECT_ROOT_MISSING = "project_root_not_found"
ERR_RUNNER_NOT_DETECTED = "runner_not_detected"
ERR_RUNNER_UNAVAILABLE = "runner_unavailable"
ERR_TEST_RUN_FAILED = "test_run_failed"
ERR_COVERAGE_DECODE = "coverage_decode_failed"
ERR_INVALID_ARGUMENT = "invalid_argument"
ERR_UNSUPPORTED = "unsupported"
ERR_INTERNAL = "internal_error"


class SlopguardError(Exception):
    """The typed error raised throughout slopguard-python.

    ``code`` is stable across releases; ``message`` is human-facing.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message

    def envelope(self) -> Dict[str, str]:
        """The JSON-friendly shape: ``{"code": ..., "message": ...}``."""
        return {"code": self.code, "message": self.message}


def file_not_found(path: str) -> SlopguardError:
    return SlopguardError(ERR_FILE_NOT_FOUND, f"File not found: {path}")


def not_a_directory(path: str) -> SlopguardError:
    return SlopguardError(ERR_NOT_A_DIRECTORY, f"Not a directory: {path}")


def unreadable_file(path: str, underlying: object) -> SlopguardError:
    return SlopguardError(ERR_UNREADABLE_FILE, f"Could not read {path}: {underlying}")


def parse_failed(path: str, underlying: object) -> SlopguardError:
    return SlopguardError(ERR_PARSE_FAILED, f"Failed to parse {path}: {underlying}")


def project_root_not_found(searched_from: str) -> SlopguardError:
    return SlopguardError(
        ERR_PROJECT_ROOT_MISSING,
        f"No project root (pyproject.toml / setup.py / setup.cfg / .git) found at or "
        f"above {searched_from}. Pass --project-dir to point at the project root, or "
        f"--no-coverage to skip the test run.",
    )


def runner_not_detected(project_root: str) -> SlopguardError:
    return SlopguardError(
        ERR_RUNNER_NOT_DETECTED,
        f"Could not detect a test runner in {project_root}. Pass --runner pytest or "
        f"--runner unittest, point at the project with --project-dir, supply a prebuilt "
        f"coverage.py report with --coverage-file, or skip coverage with --no-coverage.",
    )


def runner_unavailable(message: str) -> SlopguardError:
    return SlopguardError(ERR_RUNNER_UNAVAILABLE, message)


def test_run_failed(exit_code: int, output: str) -> SlopguardError:
    return SlopguardError(
        ERR_TEST_RUN_FAILED,
        f"the test run failed before coverage was produced (exit {exit_code}): {output}",
    )


def coverage_decode_failed(underlying: object) -> SlopguardError:
    return SlopguardError(ERR_COVERAGE_DECODE, f"Failed to decode coverage data: {underlying}")


def invalid_argument(name: str, reason: str) -> SlopguardError:
    return SlopguardError(ERR_INVALID_ARGUMENT, f"Invalid argument '{name}': {reason}")


def unsupported(reason: str) -> SlopguardError:
    return SlopguardError(ERR_UNSUPPORTED, f"Unsupported: {reason}")


def envelope_for(err: BaseException) -> Dict[str, str]:
    """Convert any exception into a stable ``{"code", "message"}`` envelope.

    Non-``SlopguardError`` values are reported as ``internal_error``.
    """
    if isinstance(err, SlopguardError):
        return err.envelope()
    return {"code": ERR_INTERNAL, "message": str(err)}
