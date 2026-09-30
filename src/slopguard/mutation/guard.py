"""Keep in-place mutation safe. ``mutate`` edits one source file at a time, and
the guard makes sure the original always comes back:

* Before a file's first mutant its bytes are backed up (in memory and in the
  guard directory); before each mutant a journal names the file and the
  mutant's sha256. After the test run the original bytes and timestamps are
  written back and the journal is deleted.
* A ``lock`` file (exclusive create, holding the pid) allows one run per
  project. A lock whose pid is dead belongs to an interrupted run: its journal
  is used to restore the file — but only when the file still holds exactly
  that mutant, so later edits are never overwritten.
* Each mutant is written with the original mtime + 1 s, so a ``__pycache__``
  entry of the original can never be loaded for it; the restore puts the
  original mtime back, so the user's ``.pyc`` files stay valid.
* Right before each mutant is written, the file must still hold the bytes the
  mutants were planned from. When someone edited it since, nothing is written
  (:class:`SourceChanged`), so the edit survives. :meth:`restore` does not
  compare: an edit saved while a mutant is in place is overwritten, as the
  shared contract specifies.

The guard directory is ``<temp dir>/slopguard-mutate/<16 hex chars of
sha256(real project root)>`` — never inside the project, and shared by every
slopguard port.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from typing import Callable, List, NamedTuple, Optional, TypeVar

from ..errors import mutation_in_progress, restore_failed

LOCK = "lock"
JOURNAL = "journal.json"
ORIGINAL = "original"

_ONE_SECOND_NS = 1_000_000_000

T = TypeVar("T")


class SourceChanged(Exception):
    """The file no longer holds the bytes its mutants were planned from:
    someone edited it during the run. Nothing was written."""

    def __init__(self, path: str) -> None:
        super().__init__(f"{path} changed while mutate was running")
        self.path = path


class _CurrentFile(NamedTuple):
    path: str
    original: bytes
    atime_ns: int
    mtime_ns: int


def guard_directory(project_root: str, tmp_root: Optional[str] = None) -> str:
    """``<tmp>/slopguard-mutate/<first 16 hex chars of sha256(real project path)>``."""
    real = os.path.realpath(os.path.abspath(project_root))
    digest = hashlib.sha256(os.fsencode(real)).hexdigest()[:16]
    return os.path.join(tmp_root or tempfile.gettempdir(), "slopguard-mutate", digest)


def process_is_alive(pid: int) -> bool:
    """Whether a process with ``pid`` exists. ``os.kill(pid, 0)`` probes it on
    POSIX; on Windows the same call would terminate the process, so it is never
    made there."""
    if pid <= 0:
        return False
    if os.name == "nt":  # pragma: no cover — exercised on Windows only
        return _windows_process_is_alive(pid)
    try:
        os.kill(pid, 0)
    except PermissionError:
        return True  # the process exists but belongs to another user
    except (OSError, OverflowError):
        return False
    return True


class WorkspaceGuard:
    """One project's lock plus the backup and journal of the file being
    mutated. Get one with :meth:`acquire`; always :meth:`release` it."""

    def __init__(self, directory: str, notes: List[str]) -> None:
        self.directory = directory
        # Plain-sentence notes from recovering an interrupted run.
        self.notes = notes
        self._current: Optional[_CurrentFile] = None
        self._mutated = False
        self._released = False

    @classmethod
    def acquire(
        cls,
        project_root: str,
        tmp_root: Optional[str] = None,
        pid: Optional[int] = None,
        is_alive: Optional[Callable[[int], bool]] = None,
    ) -> "WorkspaceGuard":
        """Take the project's lock, recovering an interrupted run's leftovers
        first. Raises ``mutation_in_progress`` while another live run holds
        it."""
        directory = guard_directory(project_root, tmp_root)
        pid = os.getpid() if pid is None else pid
        is_alive = is_alive or process_is_alive
        os.makedirs(directory, exist_ok=True)
        lock = os.path.join(directory, LOCK)
        if _try_lock(lock, pid):
            return cls(directory, [])

        holder = _read_pid(lock)
        if holder is not None and holder != pid and is_alive(holder):
            raise mutation_in_progress(holder, project_root)
        notes = _recover_interrupted_run(directory)
        _remove(lock)
        if _try_lock(lock, pid):
            return cls(directory, notes)
        # Only when another run takes the lock between the cleanup and the retry.
        raise mutation_in_progress(_read_pid(lock) or 0, project_root)  # pragma: no cover

    def with_mutant(self, path: str, planned: bytes, mutated: bytes, body: Callable[[], T]) -> T:
        """Write ``mutated`` into ``path``, run ``body``, and restore the
        original — whatever ``body`` does. The file is backed up before its
        first mutant. Raises :class:`SourceChanged`, writing nothing, when the
        file no longer holds ``planned`` (the bytes the mutants came from)."""
        if self._current is None or self._current.path != path:
            self._begin(path, planned)
        elif _read_or_none(path) != planned:
            raise SourceChanged(path)
        current = self._current
        _write_json_atomic(
            os.path.join(self.directory, JOURNAL),
            {"file": path, "mutantSha256": hashlib.sha256(mutated).hexdigest()},
        )
        self._mutated = True
        try:
            _write_in_place(path, mutated)
            os.utime(path, ns=(current.atime_ns, current.mtime_ns + _ONE_SECOND_NS))
            return body()
        finally:
            self.restore()

    def restore(self) -> Optional[str]:
        """Put the original back if a mutant is in place. Safe to call from a
        signal handler. Returns the restored path, or ``None``."""
        current = self._current
        if current is None or not self._mutated:
            return None
        try:
            _write_in_place(current.path, current.original)
        except OSError as exc:
            raise restore_failed(current.path, os.path.join(self.directory, ORIGINAL), exc)
        try:
            os.utime(current.path, ns=(current.atime_ns, current.mtime_ns))
        except OSError:
            pass  # the bytes are back; a newer mtime only makes Python recompile
        self._mutated = False
        _remove(os.path.join(self.directory, JOURNAL))
        return current.path

    def release(self) -> None:
        """Restore anything mutated, then delete the lock, journal and backup,
        and the guard directory itself when nothing else is left in it (a
        kept recovery backup stays). Idempotent."""
        if self._released:
            return
        self.restore()
        self._released = True
        for name in (JOURNAL, JOURNAL + ".tmp", ORIGINAL, LOCK):
            _remove(os.path.join(self.directory, name))
        try:
            os.rmdir(self.directory)
        except OSError:
            pass  # not empty: a backup from a recovery is kept for the user

    def _begin(self, path: str, planned: bytes) -> None:
        self.restore()
        try:
            stat = os.stat(path)  # before the read, which may bump the atime
        except OSError:
            raise SourceChanged(path)
        if _read_or_none(path) != planned:
            raise SourceChanged(path)
        _write_in_place(os.path.join(self.directory, ORIGINAL), planned)
        self._current = _CurrentFile(path, planned, stat.st_atime_ns, stat.st_mtime_ns)


def _try_lock(lock: str, pid: int) -> bool:
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="ascii") as fh:
        fh.write(str(pid))
    return True


def _read_pid(lock: str) -> Optional[int]:
    data = _read_or_none(lock)
    text = data.decode("ascii", errors="replace").strip() if data is not None else ""
    return int(text) if re.fullmatch(r"[0-9]+", text) else None


def _recover_interrupted_run(directory: str) -> List[str]:
    """Undo what an interrupted run left behind. Returns notes for the report."""
    journal_path = os.path.join(directory, JOURNAL)
    backup_path = os.path.join(directory, ORIGINAL)
    journal = _read_journal(journal_path)
    notes = [] if journal is None else _recover_file(journal["file"], journal["mutantSha256"], backup_path, directory)
    _remove(journal_path)
    _remove(backup_path)
    return notes


def _recover_file(path: str, mutant_sha: str, backup_path: str, directory: str) -> List[str]:
    current = _read_or_none(path)
    backup = _read_or_none(backup_path)
    holds_mutant = current is not None and hashlib.sha256(current).hexdigest() == mutant_sha
    if backup is None:
        if not holds_mutant:
            return []
        return [
            f"An interrupted mutate run left {path} mutated and its backup is missing. "
            f"Restore the file from version control."
        ]
    if holds_mutant:
        try:
            _write_in_place(path, backup)
        except OSError as exc:
            raise restore_failed(path, backup_path, exc)
        return [f"Restored {path}, which an interrupted mutate run left mutated."]
    if current == backup:
        return []
    kept = os.path.join(directory, f"original-{int(time.time() * 1000)}")
    os.replace(backup_path, kept)
    return [
        f"An interrupted mutate run left a backup of {path} at {kept}. "
        f"The file has changed since, so it was not restored."
    ]


def _read_journal(journal_path: str) -> Optional[dict]:
    data = _read_or_none(journal_path)
    if data is None:
        return None
    try:
        parsed = json.loads(data.decode("utf-8"))
    except ValueError:
        return None  # a torn journal carries no usable information
    if (
        isinstance(parsed, dict)
        and isinstance(parsed.get("file"), str)
        and isinstance(parsed.get("mutantSha256"), str)
    ):
        return parsed
    return None


def _write_json_atomic(target: str, value: dict) -> None:
    """Write via a temp file and rename, so a crash never leaves a torn journal."""
    temp = target + ".tmp"
    with open(temp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(value, separators=(",", ":"), ensure_ascii=False))
    os.replace(temp, target)


def _write_in_place(path: str, data: bytes) -> None:
    """Truncate and write, so the inode, mode and hard links survive."""
    with open(path, "wb") as fh:
        fh.write(data)


def _read_or_none(path: str) -> Optional[bytes]:
    try:
        with open(path, "rb") as fh:
            return fh.read()
    except OSError:
        return None


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def _windows_process_is_alive(pid: int) -> bool:  # pragma: no cover — Windows only
    import ctypes

    kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
    process_query_limited_information = 0x1000
    still_active = 259
    handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
    if not handle:
        return kernel32.GetLastError() == 5  # access denied: it exists
    try:
        code = ctypes.c_ulong()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True
        return code.value == still_active
    finally:
        kernel32.CloseHandle(handle)
