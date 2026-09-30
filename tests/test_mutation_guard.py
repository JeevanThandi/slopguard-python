"""The workspace guard: lock exclusivity, stale-lock recovery (both branches),
in-place writes with the mtime rule, and restore on every exit path."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest

from slopguard.errors import ERR_MUTATION_IN_PROGRESS, ERR_RESTORE_FAILED, SlopguardError
from slopguard.mutation.guard import (
    JOURNAL,
    LOCK,
    ORIGINAL,
    SourceChanged,
    WorkspaceGuard,
    guard_directory,
    process_is_alive,
)

SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
ORIGINAL_BYTES = b"def f(a, b):\n    return a < b\n"
MUTANT_BYTES = b"def f(a, b):\n    return a <= b\n"


def sha(data):
    return hashlib.sha256(data).hexdigest()


def dead_pid():
    """The pid of a process that has exited and been reaped."""
    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    proc.wait()
    return proc.pid


class GuardFixture(unittest.TestCase):
    def setUp(self):
        # A private temp root, so the suite leaves no guard directories behind.
        self.tmp_root = tempfile.mkdtemp(prefix="slop-guardroot-")
        self.project = tempfile.mkdtemp(prefix="slop-guardproj-")
        self.addCleanup(shutil.rmtree, self.tmp_root, True)
        self.addCleanup(shutil.rmtree, self.project, True)
        self.file = os.path.join(self.project, "calc.py")
        with open(self.file, "wb") as fh:
            fh.write(ORIGINAL_BYTES)
        # A fixed, old mtime (with sub-second precision) to check the rule.
        self.atime_ns = 1_600_000_000_250_000_000
        self.mtime_ns = 1_700_000_000_500_000_000
        os.utime(self.file, ns=(self.atime_ns, self.mtime_ns))
        self.directory = guard_directory(self.project, self.tmp_root)

    def acquire(self, **kwargs):
        return WorkspaceGuard.acquire(self.project, tmp_root=self.tmp_root, **kwargs)

    def read(self, path=None):
        with open(path or self.file, "rb") as fh:
            return fh.read()

    def plant(self, lock_pid=None, journal=None, backup=None):
        os.makedirs(self.directory, exist_ok=True)
        if lock_pid is not None:
            with open(os.path.join(self.directory, LOCK), "w") as fh:
                fh.write(str(lock_pid))
        if journal is not None:
            with open(os.path.join(self.directory, JOURNAL), "w") as fh:
                fh.write(journal if isinstance(journal, str) else json.dumps(journal))
        if backup is not None:
            with open(os.path.join(self.directory, ORIGINAL), "wb") as fh:
                fh.write(backup)

    def guard_files(self):
        return sorted(os.listdir(self.directory)) if os.path.isdir(self.directory) else []


class GuardDirectoryTests(GuardFixture):
    def test_location_under_the_temp_root(self):
        real = os.path.realpath(self.project)
        expected = hashlib.sha256(os.fsencode(real)).hexdigest()[:16]
        self.assertEqual(self.directory, os.path.join(self.tmp_root, "slopguard-mutate", expected))
        self.assertFalse(os.path.realpath(self.directory).startswith(os.path.realpath(self.project)))

    def test_symlinked_project_shares_the_directory(self):
        link_parent = tempfile.mkdtemp(prefix="slop-link-")
        self.addCleanup(shutil.rmtree, link_parent, True)
        link = os.path.join(link_parent, "proj")
        os.symlink(self.project, link)
        self.assertEqual(guard_directory(link, self.tmp_root), self.directory)

    def test_default_root_is_the_os_temp_dir(self):
        self.assertTrue(guard_directory(self.project).startswith(os.path.join(tempfile.gettempdir(), "slopguard-mutate")))


class LockTests(GuardFixture):
    def test_acquire_writes_our_pid_and_release_removes_it(self):
        guard = self.acquire()
        with open(os.path.join(self.directory, LOCK)) as fh:
            self.assertEqual(fh.read(), str(os.getpid()))
        self.assertEqual(guard.notes, [])
        guard.release()
        guard.release()  # idempotent
        self.assertFalse(os.path.exists(self.directory))  # the empty directory goes too
        self.assertTrue(os.path.isdir(os.path.dirname(self.directory)))

    def test_live_holder_blocks_a_second_run(self):
        holder = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        try:
            self.plant(lock_pid=holder.pid)
            with self.assertRaises(SlopguardError) as ctx:
                self.acquire()
            self.assertEqual(ctx.exception.code, ERR_MUTATION_IN_PROGRESS)
            self.assertEqual(
                ctx.exception.message,
                f"Another slopguard mutate run (pid {holder.pid}) is using {self.project}.",
            )
        finally:
            holder.kill()
            holder.wait()
        # Once the holder is gone the lock is stale and can be taken.
        self.acquire().release()

    def test_lock_is_exclusive_across_processes(self):
        # A real second process takes the guard through the library and holds it.
        ready = os.path.join(self.project, "ready")
        script = textwrap.dedent(
            f"""
            import os, sys, time
            sys.path.insert(0, {SRC!r})
            from slopguard.mutation.guard import WorkspaceGuard
            guard = WorkspaceGuard.acquire({self.project!r}, tmp_root={self.tmp_root!r})
            open({ready!r}, "w").close()
            sys.stdin.read()
            guard.release()
            """
        )
        child = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE)
        try:
            deadline = time.monotonic() + 60
            while not os.path.exists(ready):
                self.assertLess(time.monotonic(), deadline, "the child never took the lock")
                self.assertIsNone(child.poll(), "the child exited early")
                time.sleep(0.05)
            with self.assertRaises(SlopguardError) as ctx:
                self.acquire()
            self.assertEqual(ctx.exception.code, ERR_MUTATION_IN_PROGRESS)
            self.assertIn(f"(pid {child.pid})", ctx.exception.message)
        finally:
            child.communicate(b"", timeout=60)
        self.assertEqual(child.returncode, 0)
        self.acquire().release()

    def test_injected_liveness(self):
        self.plant(lock_pid=4242)
        with self.assertRaises(SlopguardError):
            self.acquire(is_alive=lambda pid: True)
        self.acquire(is_alive=lambda pid: False).release()

    def test_our_own_pid_counts_as_stale(self):
        self.plant(lock_pid=os.getpid())
        self.acquire().release()

    def test_unreadable_lock_contents_count_as_stale(self):
        self.plant(lock_pid="not-a-pid")
        self.acquire().release()


class RecoveryTests(GuardFixture):
    def journal(self, data):
        return {"file": self.file, "mutantSha256": sha(data)}

    def test_file_still_holding_the_mutant_is_restored(self):
        with open(self.file, "wb") as fh:
            fh.write(MUTANT_BYTES)
        self.plant(lock_pid=dead_pid(), journal=self.journal(MUTANT_BYTES), backup=ORIGINAL_BYTES)
        guard = self.acquire()
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        self.assertEqual(guard.notes, [f"Restored {self.file}, which an interrupted mutate run left mutated."])
        self.assertEqual(self.guard_files(), [LOCK])
        guard.release()

    def test_changed_file_is_left_alone_and_the_backup_kept(self):
        edited = b"def f(a, b):\n    return b > a  # edited since\n"
        with open(self.file, "wb") as fh:
            fh.write(edited)
        self.plant(lock_pid=dead_pid(), journal=self.journal(MUTANT_BYTES), backup=ORIGINAL_BYTES)
        guard = self.acquire()
        self.assertEqual(self.read(), edited)
        kept = [name for name in self.guard_files() if name.startswith("original-")]
        self.assertEqual(len(kept), 1)
        kept_path = os.path.join(self.directory, kept[0])
        self.assertEqual(self.read(kept_path), ORIGINAL_BYTES)
        self.assertEqual(
            guard.notes,
            [
                f"An interrupted mutate run left a backup of {self.file} at {kept_path}. "
                "The file has changed since, so it was not restored."
            ],
        )
        guard.release()
        self.assertEqual(self.guard_files(), kept)  # the backup outlives the run

    def test_file_already_back_to_the_original_needs_nothing(self):
        self.plant(lock_pid=dead_pid(), journal=self.journal(MUTANT_BYTES), backup=ORIGINAL_BYTES)
        guard = self.acquire()
        self.assertEqual(guard.notes, [])
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        guard.release()
        self.assertEqual(self.guard_files(), [])

    def test_missing_backup_with_the_mutant_in_place(self):
        with open(self.file, "wb") as fh:
            fh.write(MUTANT_BYTES)
        self.plant(lock_pid=dead_pid(), journal=self.journal(MUTANT_BYTES))
        guard = self.acquire()
        self.assertEqual(
            guard.notes,
            [
                f"An interrupted mutate run left {self.file} mutated and its backup is missing. "
                "Restore the file from version control."
            ],
        )
        guard.release()

    def test_missing_backup_without_the_mutant_needs_nothing(self):
        self.plant(lock_pid=dead_pid(), journal=self.journal(MUTANT_BYTES))
        self.assertEqual(self.acquire().notes, [])

    def test_torn_or_foreign_journal_is_ignored(self):
        for journal in ("{not json", json.dumps(["file"]), json.dumps({"file": 1, "mutantSha256": "x"})):
            self.plant(lock_pid=dead_pid(), journal=journal, backup=ORIGINAL_BYTES)
            guard = self.acquire()
            self.assertEqual(guard.notes, [])
            guard.release()
            self.assertEqual(self.guard_files(), [])

    def test_recovery_write_failure_is_restore_failed(self):
        with open(self.file, "wb") as fh:
            fh.write(MUTANT_BYTES)
        self.plant(lock_pid=dead_pid(), journal=self.journal(MUTANT_BYTES), backup=ORIGINAL_BYTES)
        os.chmod(self.file, stat.S_IRUSR)
        try:
            if os.access(self.file, os.W_OK):
                self.skipTest("running with privileges that ignore file modes")
            with self.assertRaises(SlopguardError) as ctx:
                self.acquire()
            self.assertEqual(ctx.exception.code, ERR_RESTORE_FAILED)
            self.assertIn(os.path.join(self.directory, ORIGINAL), ctx.exception.message)
        finally:
            os.chmod(self.file, stat.S_IRUSR | stat.S_IWUSR)


class WithMutantTests(GuardFixture):
    def test_mutant_in_place_then_original_back(self):
        guard = self.acquire()
        inode = os.stat(self.file).st_ino
        seen = {}

        def body():
            seen["bytes"] = self.read()
            seen["mtime"] = os.stat(self.file).st_mtime_ns
            seen["inode"] = os.stat(self.file).st_ino
            with open(os.path.join(self.directory, JOURNAL)) as fh:
                seen["journal"] = json.load(fh)
            seen["backup"] = self.read(os.path.join(self.directory, ORIGINAL))
            return "result"

        self.assertEqual(guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, body), "result")
        self.assertEqual(seen["bytes"], MUTANT_BYTES)
        self.assertEqual(seen["mtime"], self.mtime_ns + 1_000_000_000)
        self.assertEqual(seen["inode"], inode)
        self.assertEqual(seen["journal"], {"file": self.file, "mutantSha256": sha(MUTANT_BYTES)})
        self.assertEqual(seen["backup"], ORIGINAL_BYTES)

        after = os.stat(self.file)
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        self.assertEqual(after.st_mtime_ns, self.mtime_ns)
        self.assertEqual(after.st_atime_ns, self.atime_ns)
        self.assertEqual(after.st_ino, inode)
        self.assertNotIn(JOURNAL, self.guard_files())
        guard.release()
        self.assertEqual(self.guard_files(), [])

    def test_a_file_edited_before_its_first_mutant_is_left_alone(self):
        edited = ORIGINAL_BYTES + b"# edited\n"
        with open(self.file, "wb") as fh:
            fh.write(edited)
        guard = self.acquire()
        ran = []
        with self.assertRaises(SourceChanged) as ctx:
            guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, lambda: ran.append(True))
        self.assertEqual(ctx.exception.path, self.file)
        self.assertEqual(ran, [])
        self.assertEqual(self.read(), edited)
        self.assertEqual(self.guard_files(), [LOCK])  # no journal, no backup
        guard.release()

    def test_a_file_edited_between_two_mutants_is_left_alone(self):
        guard = self.acquire()
        guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, lambda: None)
        edited = ORIGINAL_BYTES + b"# edited\n"
        with open(self.file, "wb") as fh:
            fh.write(edited)
        with self.assertRaises(SourceChanged):
            guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, lambda: None)
        self.assertEqual(self.read(), edited)
        guard.release()
        self.assertEqual(self.read(), edited)  # the release does not put the old bytes back

    def test_a_deleted_file_counts_as_changed(self):
        guard = self.acquire()
        os.remove(self.file)
        with self.assertRaises(SourceChanged):
            guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, lambda: None)
        self.assertFalse(os.path.exists(self.file))
        guard.release()

    def test_restore_on_a_thrown_error(self):
        guard = self.acquire()

        def body():
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, body)
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        self.assertEqual(os.stat(self.file).st_mtime_ns, self.mtime_ns)
        self.assertIsNone(guard.restore())  # nothing left to restore
        guard.release()

    def test_restore_from_inside_the_body_is_idempotent(self):
        # What a signal handler does mid-run.
        guard = self.acquire()
        restored = []
        guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, lambda: restored.append(guard.restore()))
        self.assertEqual(restored, [self.file])
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        guard.release()

    def test_second_file_backs_up_its_own_original(self):
        other = os.path.join(self.project, "other.py")
        with open(other, "wb") as fh:
            fh.write(b"x = 1\n")
        guard = self.acquire()
        guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, lambda: None)
        backups = []
        guard.with_mutant(other, b"x = 1\n", b"x = 2\n", lambda: backups.append(self.read(os.path.join(self.directory, ORIGINAL))))
        self.assertEqual(backups, [b"x = 1\n"])
        self.assertEqual(self.read(other), b"x = 1\n")
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        guard.release()

    def test_release_restores_a_mutant_still_in_place(self):
        guard = self.acquire()

        def body():
            guard.release()  # e.g. the pipeline's finally, reached first

        guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, body)
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        self.assertEqual(self.guard_files(), [])

    def test_restore_failure_keeps_everything_for_recovery(self):
        guard = self.acquire()

        def body():
            os.chmod(self.file, stat.S_IRUSR)
            if os.access(self.file, os.W_OK):
                self.skipTest("running with privileges that ignore file modes")

        try:
            with self.assertRaises(SlopguardError) as ctx:
                guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, body)
            self.assertEqual(ctx.exception.code, ERR_RESTORE_FAILED)
            self.assertIn(self.file, ctx.exception.message)
            self.assertIn(os.path.join(self.directory, ORIGINAL), ctx.exception.message)
            with self.assertRaises(SlopguardError):
                guard.release()
            self.assertEqual(sorted(self.guard_files()), sorted([JOURNAL, LOCK, ORIGINAL]))
        finally:
            os.chmod(self.file, stat.S_IRUSR | stat.S_IWUSR)
        # The next run finds the stale lock and puts the original back.
        recovered = self.acquire()
        self.assertEqual(recovered.notes, [f"Restored {self.file}, which an interrupted mutate run left mutated."])
        self.assertEqual(self.read(), ORIGINAL_BYTES)
        recovered.release()

    def test_bytes_come_back_even_when_utime_fails(self):
        guard = self.acquire()
        import slopguard.mutation.guard as guard_module

        calls = []
        real_utime = os.utime

        def flaky_utime(path, ns=None):
            calls.append(ns)
            if len(calls) == 2:
                raise OSError("utime not supported")
            real_utime(path, ns=ns)

        guard_module.os.utime = flaky_utime
        try:
            guard.with_mutant(self.file, ORIGINAL_BYTES, MUTANT_BYTES, lambda: None)
        finally:
            guard_module.os.utime = real_utime
        self.assertEqual(self.read(), ORIGINAL_BYTES)  # the bytes still came back
        guard.release()


class ProcessIsAliveTests(unittest.TestCase):
    def test_probe(self):
        self.assertTrue(process_is_alive(os.getpid()))
        self.assertFalse(process_is_alive(0))
        self.assertFalse(process_is_alive(-5))
        self.assertFalse(process_is_alive(dead_pid()))
        self.assertFalse(process_is_alive(2**40))

    @unittest.skipUnless(os.name == "posix" and os.getuid() != 0, "needs a non-root POSIX user")
    def test_another_users_process_counts_as_alive(self):
        self.assertTrue(process_is_alive(1))  # init/launchd: EPERM, but it exists


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
