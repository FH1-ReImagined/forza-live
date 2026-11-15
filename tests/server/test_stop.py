"""A stop signal is the event loop's (#78): SIGTERM (`docker stop`) and SIGINT (Ctrl+C) only note
themselves (server.StopRequest), and main() ends from its own code with every save of the stop.
They used to raise KeyboardInterrupt wherever the main thread was, and Python only prints an
exception raised in a weakref callback, a __del__ or a garbage collector callback: there the
stop was lost and the server ran on until the container runtime killed it.

The unit tests raise the signals in this process (signal.raise_signal), the others stop real
servers on this machine. tools/stop_loop.py stops a busy server thousands of times."""

import asyncio
import gc
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import weakref
from unittest import mock

from forza_live import server as srv
from forza_live.core import relays
from forza_live.social import notifications as notices
from tests.support import ROOT

PYTHON = getattr(sys, "_base_executable", sys.executable)
POSIX_SIGNALS = unittest.skipIf(sys.platform == "win32",
                                "a stop signal to another process is POSIX's (Windows: "
                                "TerminateProcess, or Ctrl+C in the server's own console)")
PLAYER = 0x0009000000000001


class Dropped:
    """An object whose weakref callback runs some code: where a signal used to be lost."""


def in_a_weakref_callback(code):
    """code() run as Python runs a weakref callback (WeakSet._remove when a task is collected):
    what it raises is printed and dropped there, never seen by the code around it."""
    obj = Dropped()
    ref = weakref.ref(obj, lambda ref: code())
    with mock.patch("sys.unraisablehook"):  # "Exception ignored in ..." without the noise
        del obj
        gc.collect()
    return ref


def put_back(test):
    """The stop signals' handlers as this process has them, after the test."""
    for number in srv.STOP_SIGNALS:
        test.addCleanup(signal.signal, number, signal.getsignal(number))


class StopRequestTest(unittest.IsolatedAsyncioTestCase):
    """The request itself, on the test's loop, with the signals raised in this process."""

    async def asyncSetUp(self):
        put_back(self)
        self.loop = asyncio.get_running_loop()
        self.addCleanup(self.take_off)

    def serving(self, stop):
        stop.serving = True  # as main() has it once the start is done
        return stop

    def take_off(self):
        for number in srv.STOP_SIGNALS:
            try:
                self.loop.remove_signal_handler(number)
            except (NotImplementedError, RuntimeError):
                pass

    async def test_the_old_way_loses_a_signal_in_a_weakref_callback(self):
        """Why (#78): with Python's handler the KeyboardInterrupt is raised inside the callback,
        and dropped there."""
        signal.signal(signal.SIGINT, signal.default_int_handler)
        reached = []

        def callback():
            signal.raise_signal(signal.SIGINT)
            reached.append("after")  # not reached: the KeyboardInterrupt ended the callback
        try:
            in_a_weakref_callback(callback)
        except KeyboardInterrupt:
            self.fail("the KeyboardInterrupt reached the caller")
        self.assertEqual(reached, [])  # and nobody knows there was a stop

    async def test_a_signal_is_noted_and_wakes_the_wait(self):
        stop = self.serving(srv.StopRequest().install(self.loop))
        self.assertFalse(await stop.wait(0.01))
        signal.raise_signal(signal.SIGTERM)  # nothing raised here
        self.assertTrue(await stop.wait(5))
        self.assertTrue(await stop.wait(0))  # and from then on, at once
        self.assertEqual(stop.came, [signal.SIGTERM])
        self.assertEqual(stop.said(), "stop: SIGTERM: the server stops")

    async def test_a_signal_in_a_weakref_callback_is_not_lost(self):
        stop = srv.StopRequest().install(self.loop)
        reached = []

        def callback():
            signal.raise_signal(signal.SIGTERM)
            reached.append("after")
        in_a_weakref_callback(callback)
        self.assertEqual(reached, ["after"])  # the callback ran on: nothing was raised in it
        self.assertTrue(await stop.wait(5))
        self.assertEqual(stop.came, [signal.SIGTERM])

    async def test_where_the_loop_has_no_signal_handlers_one_that_only_schedules(self):
        """Windows' event loops have no add_signal_handler: a signal.signal handler that only
        hands the note to the loop, which is what runs on Windows (this test runs it there, and
        everywhere else with the loop's own refused)."""
        refused = mock.patch.object(self.loop, "add_signal_handler",
                                    side_effect=NotImplementedError)
        with refused:
            stop = srv.StopRequest().install(self.loop)
        self.assertEqual(stop.taken, [])
        self.assertEqual(signal.getsignal(signal.SIGINT), stop.schedule)
        reached = []

        def callback():
            signal.raise_signal(signal.SIGINT)
            reached.append("after")
        in_a_weakref_callback(callback)
        self.assertEqual(reached, ["after"])
        self.assertEqual(stop.came, [])  # the loop notes it, not the handler
        self.assertTrue(await stop.wait(5))
        self.assertEqual(stop.came, [signal.SIGINT])

    async def test_more_signals_change_nothing(self):
        stop = self.serving(srv.StopRequest().install(self.loop))
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGTERM):
            signal.raise_signal(number)
        self.assertTrue(await stop.wait(5))
        await asyncio.sleep(0.05)  # the loop has read them all
        self.assertEqual(stop.came, [signal.SIGTERM, signal.SIGINT, signal.SIGTERM])
        self.assertEqual(stop.said(), "stop: SIGTERM: the server stops")

    async def test_a_signal_during_the_start_waits_for_it(self):
        stop = srv.StopRequest().install(self.loop)
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.05)  # the start's next await: noted, nothing else
        self.assertTrue(stop.stopping.is_set())
        stop.serving = True  # main(): the start is done
        self.assertTrue(await stop.wait(1))
        self.assertEqual(stop.said(), "stop: SIGTERM during the start: the server stops now "
                                      "that it is done")

    async def test_the_relay_is_cancelled_at_its_await(self):
        stop = srv.StopRequest().install(self.loop)
        steps = []

        async def relay():
            try:
                while True:
                    steps.append("tick")
                    await asyncio.sleep(0.01)
            finally:
                steps.append("closed")
        self.loop.call_later(0.05, signal.raise_signal, signal.SIGTERM)
        self.assertIsNone(await asyncio.wait_for(stop.until_stopped(relay()), 5))
        self.assertEqual(steps[-1], "closed")

        async def refused():  # a relay that ends by itself (its command line) ends main() so
            raise ValueError("--relay-only needs ...")
        with self.assertRaises(ValueError):
            await srv.StopRequest().until_stopped(refused())

    async def test_after_the_stop_a_signal_is_ignored(self):
        """To the end of the process, with SIG_IGN: the interpreter's end puts the default
        action back for Python's own handlers, and a signal then ended the process with its
        status instead of 0 (a third signal 20 ms into a stop of the busy server, 179 times in
        700)."""
        stop = srv.StopRequest().install(self.loop)
        stop.end()
        for number in srv.STOP_SIGNALS:
            self.assertEqual(signal.getsignal(number), signal.SIG_IGN)
        signal.raise_signal(signal.SIGINT)  # nothing happens
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.05)
        self.assertEqual(stop.came, [])
        stop.end()  # (run() again, after main())
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)


class EndTest(unittest.TestCase):
    def setUp(self):
        put_back(self)

    def test_the_loops_close_no_longer_touches_the_signals(self):
        """end() takes the signals off the loop while it is open: its close then leaves SIG_IGN
        in place, and no thread still running can be ended by a SIGTERM with Python's default
        action (the main thread's mask covered only the main thread)."""
        loop = asyncio.new_event_loop()
        try:
            stop = srv.StopRequest().install(loop)
            stop.end()
            self.assertEqual(stop.taken, [])
        finally:
            loop.close()
        for number in srv.STOP_SIGNALS:
            self.assertEqual(signal.getsignal(number), signal.SIG_IGN)

    def test_a_sigint_in_the_moment_the_loop_lets_go(self):
        """The loop's remove_signal_handler gives SIGINT Python's default handler back before
        end() sets SIG_IGN: a SIGINT then (a flood, taken by another thread while the main one
        blocks it) raised KeyboardInterrupt out of end(), and the rest of main()'s stop after
        the saves was skipped."""
        loop = asyncio.new_event_loop()
        try:
            stop = srv.StopRequest().install(loop)
            if signal.SIGINT not in stop.taken:
                self.skipTest("the loop has no signal handlers here")
            real, raised = loop.remove_signal_handler, []

            def remove(number):
                found = real(number)
                if number == signal.SIGINT and not raised:
                    self.assertIs(signal.getsignal(number), signal.default_int_handler)
                    raised.append(number)
                    signal.raise_signal(signal.SIGINT)  # (no mask: pthread_sigmask is mocked)
                return found
            with mock.patch.object(loop, "remove_signal_handler", remove), \
                    mock.patch.object(signal, "pthread_sigmask", lambda how, mask: set()):
                try:
                    stop.end()
                except KeyboardInterrupt:
                    self.fail("end() raised the SIGINT's KeyboardInterrupt")
        finally:
            loop.close()
        for number in srv.STOP_SIGNALS:
            self.assertEqual(signal.getsignal(number), signal.SIG_IGN)


class StopAcceptingTest(unittest.IsolatedAsyncioTestCase):
    async def test_no_new_connection_is_served_and_the_open_ones_go_on(self):
        served = []

        async def handler(reader, writer):
            served.append(await reader.readline())
            writer.write(b"ok\n")
            await writer.drain()
            writer.close()
        listening = await asyncio.start_server(handler, "127.0.0.1", 0)
        self.addCleanup(listening.close)
        port = listening.sockets[0].getsockname()[1]
        reader, writer = await asyncio.open_connection("127.0.0.1", port)
        self.addCleanup(writer.close)
        await asyncio.sleep(0.05)  # accepted
        srv.stop_accepting(listening)
        writer.write(b"open before\n")
        self.assertEqual(await asyncio.wait_for(reader.readline(), 5), b"ok\n")
        try:
            late = socket.create_connection(("127.0.0.1", port), timeout=2)  # the backlog
        except OSError:  # Windows: Server.close(), the port refuses it
            pass
        else:
            self.addCleanup(late.close)
            late.sendall(b"opened after\n")
        await asyncio.sleep(0.2)
        self.assertEqual(served, [b"open before\n"])


class HeldTest(unittest.TestCase):
    """During the stop's saves the signals are held: noted, and the log says so."""

    def setUp(self):
        put_back(self)

    def test_held_signals_are_told_once_and_never_raised(self):
        signal.signal(signal.SIGTERM, signal.default_int_handler)  # the worst handler outside
        held = srv.hold_stop_signals()
        signal.raise_signal(signal.SIGTERM)
        in_a_weakref_callback(lambda: signal.raise_signal(signal.SIGTERM))
        with self.assertLogs("forza-live", "INFO") as logs:
            srv.tell_held(held, srv.SMALL_STORES)
        signal.raise_signal(signal.SIGINT)
        with self.assertLogs("forza-live", "INFO") as more:
            srv.release_stop_signals(held, srv.JOURNALED_STORES)
        self.assertEqual(logs.output, [
            "INFO:forza-live:stop: SIGTERM again while the registrations, co-op ledger and "
            "friends were written: ignored until they were"])
        self.assertEqual(more.output, [
            "INFO:forza-live:stop: SIGINT again while notifications.json and scoreboards.json "
            "were written: ignored until they were"])
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.default_int_handler)

    @unittest.skipUnless(hasattr(signal, "pthread_sigmask"), "POSIX")
    def test_two_signals_handled_in_one_round(self):
        """SIGINT and SIGTERM waiting together: Python runs their handlers in one round, and the
        first one's SIG_IGN must not be the second one's (it printed "Signal 15 ignored due to
        race condition", with a traceback, in 5 of 79 busy three-signal stops)."""
        held = srv.hold_stop_signals()
        self.addCleanup(srv.release_stop_signals, held)
        printed = []
        with mock.patch("sys.unraisablehook", printed.append):
            signal.pthread_sigmask(signal.SIG_BLOCK, srv.STOP_SIGNALS)
            try:
                signal.raise_signal(signal.SIGINT)
                signal.raise_signal(signal.SIGTERM)
            finally:
                signal.pthread_sigmask(signal.SIG_UNBLOCK, srv.STOP_SIGNALS)  # both, at once
        self.assertEqual(printed, [])
        self.assertEqual(held[0], [signal.SIGINT, signal.SIGTERM])

    def test_a_flood_costs_the_saves_nothing(self):
        """Once a held signal is noted it is SIG_IGN until the log has told of it: a flood of
        them is not a handler's call each, nor a byte each in the loop's wakeup pipe (which
        filled, and printed a warning for every signal after)."""
        signal.signal(signal.SIGTERM, signal.default_int_handler)
        held = srv.hold_stop_signals()
        noted = signal.getsignal(signal.SIGTERM)
        signal.raise_signal(signal.SIGINT)
        self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
        for _ in range(2000):
            signal.raise_signal(signal.SIGINT)  # ignored by the system itself
            signal.raise_signal(signal.SIGTERM)  # noted once, then ignored too
        self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
        self.assertEqual(held[0], [signal.SIGINT, signal.SIGTERM])
        with self.assertLogs("forza-live", "INFO") as logs:
            srv.tell_held(held, srv.SMALL_STORES)
        self.assertEqual(len(logs.output), 1)
        for number in srv.STOP_SIGNALS:  # the next part notes them again
            self.assertIs(signal.getsignal(number), noted)
        for _ in range(2000):
            signal.raise_signal(signal.SIGTERM)
        with self.assertLogs("forza-live", "INFO") as more:
            srv.release_stop_signals(held, srv.JOURNALED_STORES)
        self.assertEqual(more.output, [
            "INFO:forza-live:stop: SIGTERM again while notifications.json and scoreboards.json "
            "were written: ignored until they were"])
        self.assertIs(signal.getsignal(signal.SIGTERM), signal.default_int_handler)


def free_port(kind=socket.SOCK_DGRAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# A server whose start is stopped: a SIGTERM to itself while its boards are read, or while the
# server's own code is still being imported (before run() has a handler).
DURING_LOAD = """
import os, signal, sys, time
sys.path.insert(0, os.getcwd())
from forza_live.competition import scores
real = scores.Store.load
def load(self):
    if not self.loaded:
        os.kill(os.getpid(), signal.SIGTERM)  # docker stop, while the boards are read
        time.sleep(0.2)
    return real(self)
scores.Store.load = load
from forza_live.server import run
sys.argv = ["forza_live_server.py"] + sys.argv[1:]
run()
"""
DURING_IMPORT = """
import importlib.abc, os, runpy, signal, sys
class Stop(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "forza_live.server":
            os.kill(os.getpid(), signal.SIGTERM)  # docker stop, while the code is read
        return None
sys.meta_path.insert(0, Stop())
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name="__main__")
"""
# A server with a Discord status whose update takes 3 s (a slow network: it can take tens of
# seconds), its ticks 100 times faster (the update every 0.3 s instead of 30 s), and a line
# when its HTTP port takes no new connection.
SLOW_DISCORD = """
import logging, os, sys, time
sys.path.insert(0, os.getcwd())
from forza_live import server as srv
from forza_live.social import discord_status
log = logging.getLogger("forza-live")
def update(status, data_dir, now=None):
    log.info("test: the status update starts")
    time.sleep(3)
    log.info("test: the status update ends")
discord_status.enabled = lambda: True
discord_status.update = update
discord_status.offline = lambda data_dir: log.info("test: the status says offline")
real_wait = srv.StopRequest.wait
async def wait(self, seconds):
    return await real_wait(self, seconds / 100)
srv.StopRequest.wait = wait
real_stop_accepting = srv.stop_accepting
def stop_accepting(listening):
    real_stop_accepting(listening)
    log.info("test: no new connection")
srv.stop_accepting = stop_accepting
sys.argv = ["forza_live_server.py"] + sys.argv[1:]
srv.run()
"""
# A server whose HTTP port cannot be taken off the loop at the stop (a closed socket's fd -1).
FAILED_STEP = """
import os, sys
sys.path.insert(0, os.getcwd())
from forza_live import server as srv
def stop_accepting(listening):
    raise ValueError("Invalid file descriptor: -1")
srv.stop_accepting = stop_accepting
sys.argv = ["forza_live_server.py"] + sys.argv[1:]
srv.run()
"""


@POSIX_SIGNALS
class RealStopTest(unittest.TestCase):
    """Real servers on this machine, stopped as a container runtime and a terminal stop them."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = os.path.join(self.temp.name, "data")
        old = notices.DATA_DIR
        self.addCleanup(notices.configure, old)
        notices.configure(self.data)  # a notification only in the journal: the stop writes
        notices.STORE.load()          # notifications.json whole, and says so
        notices.send(PLAYER, notices.NEWS, {"Message": "before the stop"})
        notices.configure(old)

    def start(self, *flags, head=None, data=True):
        http = free_port(socket.SOCK_STREAM)
        args = ["--host", "127.0.0.1", "--port", str(free_port())]
        if data:
            args += ["--http-port", str(http), "--data-dir", self.data]
        log = open(os.path.join(self.temp.name, "server.log"), "w+")
        self.addCleanup(log.close)
        proc = subprocess.Popen((head or [PYTHON, os.path.join(ROOT, "forza_live_server.py")])
                                + args + list(flags), cwd=ROOT, stdin=subprocess.DEVNULL,
                                stdout=log, stderr=subprocess.STDOUT)
        self.addCleanup(lambda: proc.poll() is None and (proc.kill(), proc.wait()))
        return proc, log, http

    def ready(self, proc, http):
        deadline = time.monotonic() + 30
        while True:
            try:
                return json.loads(urllib.request.urlopen(
                    "http://127.0.0.1:%d/status" % http, timeout=2).read())
            except (urllib.error.URLError, OSError, ValueError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the server did not start")
                time.sleep(0.05)

    def ended(self, proc, log):
        """The exit status and the log, once the process has ended (20 s: the public server's
        time to stop)."""
        try:
            proc.wait(20)
        except subprocess.TimeoutExpired:
            self.fail("the stop was lost: the server still runs")
        log.seek(0)
        return proc.returncode, log.read().splitlines()

    def assert_stopped_cleanly(self, status, lines, first="stop: SIGTERM: the server stops"):
        text = "\n".join(lines)
        self.assertEqual(status, 0, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("Exception ignored", text)
        self.assertNotIn("Task was destroyed", text)
        said = [line.split(" ", 2)[2] for line in lines]
        self.assertIn(first, said)
        stop = said.index(first)
        # the last lines: the notifications', then the boards' (then, when signals came while
        # they were written, the line that says so)
        if " again while notifications.json and scoreboards.json were written" in said[-1]:
            said = said[:-1]
        self.assertRegex(said[-2], r"^notifications: notifications\.json written whole at the "
                                   r"stop in [\d.]+ s: it has every change: 1 notification ")
        self.assertRegex(said[-1], r"^scoreboards: .*at the stop")
        self.assertLess(stop, len(said) - 2)
        with open(os.path.join(self.data, "notifications.json")) as f:
            self.assertIn("before the stop", f.read())
        self.assertTrue(os.path.exists(os.path.join(self.data, srv.STATE_FILE)))
        return said[stop:]

    def busy(self, http, stop):
        """Connections opened and dropped: tasks die, their weak references are cleaned up."""
        while not stop.is_set():
            try:
                c = socket.create_connection(("127.0.0.1", http), timeout=2)
                c.sendall(b"GET /sta")
                c.close()
            except OSError:
                time.sleep(0.01)

    def test_sigterm_while_busy(self):
        proc, log, http = self.start()
        self.ready(proc, http)
        stop = threading.Event()
        loader = threading.Thread(target=self.busy, args=(http, stop))
        loader.start()
        self.addCleanup(loader.join)
        self.addCleanup(stop.set)
        time.sleep(0.3)
        proc.send_signal(signal.SIGTERM)
        status, lines = self.ended(proc, log)
        stop.set()
        self.assertEqual(self.assert_stopped_cleanly(status, lines)[1:], [
            line.split(" ", 2)[2] for line in lines[-2:]])  # nothing between but the saves

    def test_ctrl_c(self):
        proc, log, http = self.start()
        self.ready(proc, http)
        proc.send_signal(signal.SIGINT)
        self.assert_stopped_cleanly(*self.ended(proc, log), first="stop: SIGINT: the server stops")

    def test_three_signals_change_nothing(self):
        """docker stop, then again and again while it stops: the stop and its saves as for one,
        and a line in the log for the signals it held."""
        proc, log, http = self.start()
        self.ready(proc, http)
        for number in (signal.SIGTERM, signal.SIGTERM, signal.SIGINT):
            proc.send_signal(number)
            time.sleep(0.01)  # the last one as the process ends, often (its status stays 0)
        said = self.assert_stopped_cleanly(*self.ended(proc, log))
        self.assertTrue(all(line.startswith(("stop: ", "notifications: ", "scoreboards: "))
                            for line in said), said)

    def test_a_signal_while_the_stores_are_read(self):
        """The boards are read whole, the server starts and then stops, with every save."""
        proc, log, _ = self.start(head=[PYTHON, "-c", DURING_LOAD])
        self.assert_stopped_cleanly(*self.ended(proc, log), first="stop: SIGTERM during the "
                                    "start: the server stops now that it is done")

    def test_a_signal_while_the_code_is_read(self):
        proc, log, _ = self.start(head=[PYTHON, "-c", DURING_IMPORT,
                                        os.path.join(ROOT, "forza_live_server.py")])
        self.assert_stopped_cleanly(*self.ended(proc, log), first="stop: SIGTERM during the "
                                    "start: the server stops now that it is done")

    def test_the_saves_do_not_wait_for_the_discord_status(self):
        """A stop while the tick waits for the Discord status update: the saves at once, then
        the update's end, then the offline status (the last edit). They used to come once the
        update was done, and an update past the stop's 20 s cost them all."""
        proc, log, http = self.start(head=[PYTHON, "-c", SLOW_DISCORD])
        deadline = time.monotonic() + 30
        while "test: the status update starts" not in (log.seek(0) or log.read()):
            self.assertLess(time.monotonic(), deadline, "no status update")
            self.assertIsNone(proc.poll())
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        status, lines = self.ended(proc, log)
        text = "\n".join(lines)
        self.assertEqual(status, 0, text)
        self.assertNotIn("Traceback", text)
        said = [line.split(" ", 2)[2] for line in lines]
        order = [said.index(line) for line in (
            "stop: SIGTERM: the server stops", "test: no new connection",
            "test: the status update ends", "test: the status says offline")]
        self.assertEqual(order, sorted(order), text)
        saves = [i for i, line in enumerate(said) if line.startswith(
            ("notifications: notifications.json written whole at the stop", "scoreboards: "))
            and "at the stop" in line]
        self.assertEqual(len(saves), 2, text)
        self.assertTrue(order[1] < saves[0] < saves[1] < order[2], text)
        self.assertEqual(said.index("test: the status says offline"), len(said) - 1, text)

    def test_a_step_before_the_saves_that_fails_does_not_skip_them(self):
        proc, log, http = self.start(head=[PYTHON, "-c", FAILED_STEP])
        self.ready(proc, http)
        proc.send_signal(signal.SIGTERM)
        status, lines = self.ended(proc, log)
        text = "\n".join(lines)
        self.assertEqual(status, 0, text)
        self.assertIn("stop: the steps before the saves failed; the saves are made all the same",
                      text)
        self.assertIn("ValueError: Invalid file descriptor: -1", text)
        self.assertRegex(lines[-2], r"notifications: notifications\.json written whole at the "
                                    r"stop")
        self.assertRegex(lines[-1], r"scoreboards: .*at the stop")

    def test_a_relay(self):
        relay_port = free_port()
        secret = relays.add_relay(os.path.join(self.temp.name, "main"), "local-1", "Local",
                                  "127.0.0.1:%d" % relay_port, "Here")
        proc, log, _ = self.start("--relay-only", "--relay-of", "127.0.0.1:%d" % free_port(),
                                  "--relay-id", "local-1", "--relay-secret-file", secret,
                                  data=False)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            log.seek(0)
            if "relay local-1 on UDP" in log.read():
                break
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        status, lines = self.ended(proc, log)
        self.assertEqual(status, 0, lines)
        self.assertTrue(lines[-1].endswith("stop: SIGTERM: the server stops"), lines)
        self.assertNotIn("Traceback", "\n".join(lines))

    def test_a_start_refused_still_exits_1(self):
        os.mkdir(os.path.join(self.data, "scoreboards.json"))  # cannot be read
        proc, log, _ = self.start()
        status, lines = self.ended(proc, log)
        self.assertEqual(status, 1)
        self.assertRegex(lines[-1], r"scoreboards\.json cannot be read .* not started")


if __name__ == "__main__":
    unittest.main()
