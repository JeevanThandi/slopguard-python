"""The mutant command runner: real subprocesses for exit codes, timeouts that
kill the whole process group, output tails and streaming; plus the command,
environment, compile check and classification helpers.

Timing margins are generous: other builds may share the CPU.
"""

import io
import os
import shutil
import signal
import sys
import tempfile
import threading
import time
import unittest

from slopguard.errors import ERR_RUNNER_UNAVAILABLE, SlopguardError
from slopguard.mutation.guard import process_is_alive
from slopguard.mutation.runner import (
    CommandOutcome,
    CommandRunner,
    _drain,
    _OutputTail,
    classify,
    compiles,
    has_pytest_cov,
    mutant_command,
    mutant_env,
)
from slopguard.progress import NORMAL, VERBOSE, ProgressReporter

PY = sys.executable
POSIX = os.name == "posix"


_TEMP_DIRS = []


def temp_dir(prefix="slop-"):
    """A temp dir removed when this module's tests finish."""
    path = tempfile.mkdtemp(prefix=prefix)
    _TEMP_DIRS.append(path)
    return path


def tearDownModule():
    for path in _TEMP_DIRS:
        shutil.rmtree(path, ignore_errors=True)


def run(code, timeout=None, progress=None, runner=None):
    runner = runner or CommandRunner()
    return runner.run(
        [PY, "-c", code], temp_dir("slop-run-"), dict(os.environ), timeout, progress or ProgressReporter.silent()
    )


def sleeper(pid_file):
    """Code for a process that records its pid, then sleeps."""
    return f"import os, time; open({pid_file!r}, 'w').write(str(os.getpid())); time.sleep(120)"


def wait_until_dead(pid, seconds=30):
    deadline = time.monotonic() + seconds
    while process_is_alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def read_pid(path, seconds=30):
    deadline = time.monotonic() + seconds
    while True:
        try:
            with open(path) as fh:
                text = fh.read().strip()
            if text:
                return int(text)
        except OSError:
            pass
        if time.monotonic() > deadline:
            raise AssertionError(f"no pid written to {path}")
        time.sleep(0.05)


class CommandRunnerTests(unittest.TestCase):
    def test_exit_zero_and_output(self):
        outcome = run("print('hello from the child')")
        self.assertEqual(outcome.exit_code, 0)
        self.assertFalse(outcome.timed_out)
        self.assertIn("hello from the child", outcome.output_tail)
        self.assertGreaterEqual(outcome.seconds, 0)
        self.assertEqual(classify(outcome), "survived")

    def test_nonzero_exit_is_killed(self):
        outcome = run("import sys; print('failing'); sys.exit(3)")
        self.assertEqual(outcome.exit_code, 3)
        self.assertEqual(classify(outcome), "killed")

    def test_stderr_is_captured_too(self):
        outcome = run("import sys; sys.stderr.write('to stderr\\n')")
        self.assertIn("to stderr", outcome.output_tail)

    def test_undecodable_output_does_not_break_the_reader(self):
        outcome = run("import sys; sys.stdout.buffer.write(b'bad \\xff byte\\n')")
        self.assertIn("bad", outcome.output_tail)

    def test_launch_failure_is_runner_unavailable(self):
        with self.assertRaises(SlopguardError) as ctx:
            CommandRunner().run(["/no/such/python-xyz"], temp_dir(), dict(os.environ), None, ProgressReporter.silent())
        self.assertEqual(ctx.exception.code, ERR_RUNNER_UNAVAILABLE)

    def test_timeout_kills_a_long_running_process(self):
        started = time.monotonic()
        outcome = run("import time; print('started', flush=True); time.sleep(120)", timeout=1.0)
        elapsed = time.monotonic() - started
        self.assertTrue(outcome.timed_out)
        self.assertEqual(classify(outcome), "timeout")
        self.assertLess(elapsed, 60)
        self.assertIn("started", outcome.output_tail)

    @unittest.skipUnless(POSIX, "process groups are POSIX")
    def test_timeout_kills_the_whole_process_group(self):
        pid_file = os.path.join(temp_dir("slop-grand-"), "grandchild.pid")
        code = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {sleeper(pid_file)!r}])\n"
            "time.sleep(120)\n"
        )
        runner = CommandRunner()
        outcome = run(code, timeout=3.0, runner=runner)
        self.assertTrue(outcome.timed_out)
        grandchild = read_pid(pid_file)
        self.assertTrue(wait_until_dead(grandchild), f"grandchild {grandchild} outlived the timeout")

    @unittest.skipUnless(POSIX, "process groups are POSIX")
    def test_a_background_process_holding_the_pipe_is_killed(self):
        pid_file = os.path.join(temp_dir("slop-bg-"), "bg.pid")
        code = (
            "import subprocess, sys\n"
            f"subprocess.Popen([sys.executable, '-c', {sleeper(pid_file)!r}])\n"
            "print('leader done', flush=True)\n"
        )
        started = time.monotonic()
        outcome = run(code)
        self.assertEqual(outcome.exit_code, 0)
        self.assertLess(time.monotonic() - started, 60)
        self.assertTrue(wait_until_dead(read_pid(pid_file)))

    def test_verbose_streams_output(self):
        buf = io.StringIO()
        run("print('streamed line')", progress=ProgressReporter(buf, VERBOSE))
        self.assertIn("streamed line", buf.getvalue())

    def test_normal_progress_does_not_stream(self):
        buf = io.StringIO()
        run("print('quiet line')", progress=ProgressReporter(buf, NORMAL))
        self.assertEqual(buf.getvalue(), "")

    def test_output_tail_is_bounded(self):
        outcome = run("for i in range(5000): print('x' * 80, i)")
        self.assertLessEqual(len(outcome.output_tail), 8 * 1024 + 200)
        self.assertIn("4999", outcome.output_tail)

    def test_kill_active_without_a_command_is_a_no_op(self):
        CommandRunner().kill_active()

    @unittest.skipUnless(POSIX, "process groups are POSIX")
    def test_kill_active_on_a_vanished_group_is_quiet(self):
        import subprocess

        proc = subprocess.Popen([PY, "-c", "pass"], start_new_session=True)
        proc.wait()
        runner = CommandRunner()
        runner._active = proc  # what a late signal handler would see
        runner.kill_active()

    @unittest.skipUnless(POSIX and hasattr(signal, "pthread_sigmask"), "POSIX signals")
    def test_a_signal_during_a_run_kills_the_process_group(self):
        """The mutate signal handler raises in the main thread; the runner
        must kill the running group before the exception moves on."""
        pid_file = os.path.join(temp_dir("slop-sig-"), "grandchild.pid")
        code = (
            "import subprocess, sys, time\n"
            f"subprocess.Popen([sys.executable, '-c', {sleeper(pid_file)!r}])\n"
            "time.sleep(120)\n"
        )

        class Stop(BaseException):
            pass

        def handler(signum, frame):
            raise Stop()

        def fire():
            # Block it here so the signal can only land in the main thread.
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
            read_pid(pid_file)
            os.kill(os.getpid(), signal.SIGTERM)

        runner = CommandRunner()
        previous = signal.signal(signal.SIGTERM, handler)
        timer = threading.Thread(target=fire, daemon=True)
        try:
            timer.start()
            started = time.monotonic()
            with self.assertRaises(Stop):
                run(code, runner=runner)  # no timeout: only the signal ends it
        finally:
            signal.signal(signal.SIGTERM, previous)
            timer.join(60)
        self.assertLess(time.monotonic() - started, 90)
        self.assertIsNone(runner._active)
        self.assertTrue(wait_until_dead(read_pid(pid_file)))


class DrainTests(unittest.TestCase):
    def test_a_stream_closed_under_the_reader_is_quiet(self):
        class Closed:
            def __iter__(self):
                raise ValueError("I/O operation on closed file")

        tail = _OutputTail()
        _drain(Closed(), tail, ProgressReporter.silent())
        self.assertEqual(tail.text(), "")


class CommandTests(unittest.TestCase):
    def test_pytest_command(self):
        self.assertEqual(mutant_command("pytest", "py", False), ["py", "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider"])
        self.assertEqual(mutant_command("pytest", "py", True)[-1], "--no-cov")

    def test_unittest_command(self):
        self.assertEqual(mutant_command("unittest", "py", True), ["py", "-m", "unittest", "discover", "-f"])

    def test_pytest_cov_probe(self):
        cwd = temp_dir("slop-covprobe-")
        with open(os.path.join(cwd, "pytest_cov.py"), "w") as fh:
            fh.write("# a stand-in module\n")
        self.assertTrue(has_pytest_cov(PY, cwd))  # importable from the project dir
        self.assertFalse(has_pytest_cov("/no/such/python-xyz", cwd))

    def test_pytest_cov_probe_reports_a_missing_module(self):
        fake_python = os.path.join(temp_dir("slop-fakepy-"), "python")
        with open(fake_python, "w") as fh:
            fh.write("#!/bin/sh\nexit 1\n")
        os.chmod(fake_python, 0o755)
        if os.name == "posix":
            self.assertFalse(has_pytest_cov(fake_python, temp_dir()))

    def test_environment(self):
        env = mutant_env({"PATH": "/bin", "CI": "0"})
        self.assertEqual(env["PATH"], "/bin")
        self.assertEqual(env["CI"], "1")
        self.assertEqual(env["PYTHONDONTWRITEBYTECODE"], "1")
        self.assertEqual(env["NO_COLOR"], "1")
        self.assertEqual(env["PY_COLORS"], "0")
        self.assertIn("PYTHONDONTWRITEBYTECODE", mutant_env())


class CompileAndClassifyTests(unittest.TestCase):
    def test_compiles(self):
        self.assertTrue(compiles("x = 1\n", "a.py"))
        self.assertFalse(compiles("x = (\n", "a.py"))
        self.assertFalse(compiles("x = 1\x00\n", "a.py"))

    def test_compile_warnings_stay_quiet(self):
        import warnings

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            self.assertTrue(compiles("x = 1\nif x is 1:\n    pass\n", "a.py"))
        self.assertEqual(caught, [])

    def test_classify(self):
        self.assertEqual(classify(CommandOutcome(0, False, 1.0, "")), "survived")
        self.assertEqual(classify(CommandOutcome(1, False, 1.0, "")), "killed")
        self.assertEqual(classify(CommandOutcome(-9, True, 1.0, "")), "timeout")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
