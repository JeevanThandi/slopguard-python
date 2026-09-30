"""Run the project's test runner for ``mutate``: the plain baseline and one run
per mutant.

Mutant runs use the runner ``analyze`` drives, without coverage (a project
config that enables pytest-cov could fail a threshold and fake a kill),
stopping at the first failure:

* pytest:   ``python -m pytest -x -q -p no:cacheprovider`` (+ ``--no-cov`` when
  pytest-cov is importable in the target interpreter)
* unittest: ``python -m unittest discover -f``

Each run starts a new session, so its process group holds the runner and
every process it starts. A timeout (or an interrupt) kills the whole group: an
infinite-loop mutant must not leave a spinning test process behind.
"""

from __future__ import annotations

import os
import signal
import subprocess
import threading
import time
import warnings
from collections import deque
from typing import IO, Deque, Dict, List, Mapping, Optional

from ..coverage.detection import RUNNER_PYTEST
from ..errors import runner_unavailable
from ..progress import ProgressReporter
from .models import STATUS_KILLED, STATUS_SURVIVED, STATUS_TIMEOUT

# Added to three times the baseline run time to form the default timeout.
TIMEOUT_GRACE_SECONDS = 10

_TAIL_LIMIT = 8 * 1024
_POSIX = os.name == "posix"

# How long to wait for the output reader once the runner has exited.
_READER_GRACE_SECONDS = 2.0

# The signals ``mutate`` handles. The output reader thread blocks them, so
# they always reach the main thread — where Python runs signal handlers — and
# wake it from waiting on the test run.
_INTERRUPTS = tuple(
    s for s in (getattr(signal, name, None) for name in ("SIGINT", "SIGTERM", "SIGHUP")) if s is not None
)

_FIND_PYTEST_COV = (
    "import importlib.util, sys; "
    "sys.exit(0 if importlib.util.find_spec('pytest_cov') else 1)"
)


class CommandOutcome:
    """How one test-runner invocation ended."""

    __slots__ = ("exit_code", "timed_out", "seconds", "output_tail")

    def __init__(self, exit_code: int, timed_out: bool, seconds: float, output_tail: str) -> None:
        self.exit_code = exit_code  # negative when a signal ended the process
        self.timed_out = timed_out
        self.seconds = seconds
        self.output_tail = output_tail  # bounded tail of stdout + stderr


def mutant_command(runner: str, python: str, pytest_cov: bool) -> List[str]:
    """The argv of one mutant run (and of the plain baseline)."""
    if runner == RUNNER_PYTEST:
        argv = [python, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"]
        if pytest_cov:
            argv.append("--no-cov")
        return argv
    return [python, "-m", "unittest", "discover", "-f"]


def has_pytest_cov(python: str, cwd: str) -> bool:
    """Whether ``pytest_cov`` is importable by ``python`` in ``cwd`` (so
    ``--no-cov`` is a known flag)."""
    try:
        proc = subprocess.run(
            [python, "-c", _FIND_PYTEST_COV],
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        return False
    return proc.returncode == 0


def mutant_env(base: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    """The environment of every mutant run: non-interactive, no colour, and no
    bytecode writes (a mutant's ``.pyc`` must never land in ``__pycache__``)."""
    env = dict(os.environ if base is None else base)
    env.update({"CI": "1", "NO_COLOR": "1", "PY_COLORS": "0", "PYTHONDONTWRITEBYTECODE": "1"})
    return env


def compiles(text: str, filename: str) -> bool:
    """Whether mutated source compiles. A mutant that does not is classified
    ``compile_error`` without a test run."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # SyntaxWarnings of the original code
        try:
            compile(text, filename, "exec", dont_inherit=True)
        except (SyntaxError, ValueError):
            return False
    return True


def classify(outcome: CommandOutcome) -> str:
    """``timeout`` when the run was killed for time, ``survived`` when every
    test passed, ``killed`` otherwise."""
    if outcome.timed_out:
        return STATUS_TIMEOUT
    if outcome.exit_code == 0:
        return STATUS_SURVIVED
    return STATUS_KILLED


class CommandRunner:
    """Runs test commands in their own process group; ``kill_active`` reaches
    every process of the running command (it is safe to call from a signal
    handler)."""

    def __init__(self) -> None:
        self._active: Optional[subprocess.Popen] = None

    def run(
        self,
        argv: List[str],
        cwd: str,
        env: Mapping[str, str],
        timeout: Optional[float],
        progress: ProgressReporter,
    ) -> CommandOutcome:
        """Run to completion, or until ``timeout`` seconds pass (``None`` = no
        limit). Output is drained into a bounded tail and streamed through
        under ``--verbose``."""
        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                argv,
                cwd=cwd,
                env=dict(env),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                encoding="utf-8",
                errors="replace",
                start_new_session=_POSIX,
            )
        except OSError as exc:
            raise runner_unavailable(f"could not launch {' '.join(argv)}: {exc}")
        self._active = proc
        tail = _OutputTail()
        timed_out = False
        try:
            reader = _start_reader(proc.stdout, tail, progress)
            try:
                code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                timed_out = True
                self.kill_active()
                code = proc.wait()
            self._finish_reader(reader, proc)
        except BaseException:
            # Interrupted (a signal handler raised): leave nothing running.
            self.kill_active()
            _reap(proc)
            raise
        finally:
            self._active = None
        return CommandOutcome(code, timed_out, time.monotonic() - started, tail.text())

    def kill_active(self) -> None:
        """Kill the running command and its process group, if any."""
        proc = self._active
        if proc is None:
            return
        try:
            if _POSIX:
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover — Windows has no process groups to signal
                proc.kill()
        except OSError:
            pass  # already gone

    def _finish_reader(self, reader: threading.Thread, proc: subprocess.Popen) -> None:
        """Wait for the output to drain. A process the tests left running in the
        background can hold the pipe open; kill the rest of the group then."""
        reader.join(_READER_GRACE_SECONDS)
        if reader.is_alive():
            self.kill_active()
            reader.join(_READER_GRACE_SECONDS)
        if not reader.is_alive() and proc.stdout is not None:
            proc.stdout.close()


class _OutputTail:
    """The last ``limit`` characters of a stream, kept in whole lines."""

    def __init__(self, limit: int = _TAIL_LIMIT) -> None:
        self._limit = limit
        self._chunks: Deque[str] = deque()
        self._size = 0
        self._lock = threading.Lock()

    def push(self, chunk: str) -> None:
        with self._lock:
            self._chunks.append(chunk)
            self._size += len(chunk)
            while len(self._chunks) > 1 and self._size > self._limit:
                self._size -= len(self._chunks.popleft())

    def text(self) -> str:
        with self._lock:
            return "".join(self._chunks)


def _start_reader(stream: IO[str], tail: _OutputTail, progress: ProgressReporter) -> threading.Thread:
    """Start the output reader with the interrupt signals blocked (a new
    thread inherits the mask); the main thread's mask is restored at once."""
    reader = threading.Thread(target=_drain, args=(stream, tail, progress), daemon=True)
    if not hasattr(signal, "pthread_sigmask"):  # pragma: no cover — Windows
        reader.start()
        return reader
    previous = signal.pthread_sigmask(signal.SIG_BLOCK, _INTERRUPTS)
    try:
        reader.start()
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous)
    return reader


def _drain(stream: IO[str], tail: _OutputTail, progress: ProgressReporter) -> None:
    try:
        for line in stream:
            progress.raw(line)
            tail.push(line)
    except (OSError, ValueError):
        pass  # the stream was closed under us


def _reap(proc: subprocess.Popen) -> None:
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:  # pragma: no cover — SIGKILL is not ignorable
        pass
