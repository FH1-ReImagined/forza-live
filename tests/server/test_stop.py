"""A stop signal is the event loop's (#78): SIGTERM (`docker stop`) and SIGINT (Ctrl+C) only note
themselves (server.StopRequest), and main() ends from its own code with every save of the stop.
They used to raise KeyboardInterrupt wherever the main thread was, and Python only prints an
exception raised in a weakref callback, a __del__ or a garbage collector callback: there the
stop was lost and the server ran on until the container runtime killed it.

The unit tests raise the signals in this process (signal.raise_signal), the others stop real
servers on this machine. tools/stop_loop.py stops a busy server thousands of times."""

import argparse
import asyncio
import contextvars
import gc
import json
import os
import re
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
from tests.support import ROOT, hello

PYTHON = getattr(sys, "_base_executable", sys.executable)
POSIX_SIGNALS = unittest.skipIf(sys.platform == "win32",
                                "a stop signal to another process is POSIX's (Windows: "
                                "TerminateProcess, or Ctrl+C in the server's own console)")
PLAYER = 0x0009000000000001
SET_IN_MAIN = contextvars.ContextVar("set_in_main", default=None)


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

    def serving(self, stop):
        stop.serving = True  # as main() has it once the start is done
        return stop

    def installed(self):
        stop = srv.StopRequest().install(self.loop)
        self.addCleanup(stop.close)
        return stop

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
        stop = self.serving(self.installed())
        self.assertFalse(await stop.wait(0.01))
        signal.raise_signal(signal.SIGTERM)  # nothing raised here
        self.assertTrue(await stop.wait(5))
        self.assertTrue(await stop.wait(0))  # and from then on, at once
        self.assertEqual(stop.came, [signal.SIGTERM])
        self.assertEqual(stop.said(), "stop: SIGTERM: the server stops")

    async def test_a_signal_in_a_weakref_callback_is_not_lost(self):
        stop = self.installed()
        reached = []

        def callback():
            signal.raise_signal(signal.SIGTERM)
            reached.append("after")
        in_a_weakref_callback(callback)
        self.assertEqual(reached, ["after"])  # the callback ran on: nothing was raised in it
        self.assertTrue(await stop.wait(5))
        self.assertEqual(stop.came, [signal.SIGTERM])

    async def test_the_handler_only_schedules_the_note(self):
        """A signal.signal handler that only hands the note to the loop, on every system (the
        loop's add_signal_handler is not used: Windows' loops have none, and elsewhere the loop
        puts Python's default handlers back as it lets go of them)."""
        stop = self.installed()
        for number in srv.STOP_SIGNALS:
            self.assertEqual(signal.getsignal(number), stop.schedule)
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
        stop = self.serving(self.installed())
        for number in (signal.SIGTERM, signal.SIGINT, signal.SIGTERM):
            signal.raise_signal(number)
        self.assertTrue(await stop.wait(5))
        await asyncio.sleep(0.05)  # the loop has read them all
        self.assertEqual(stop.came, [signal.SIGTERM, signal.SIGINT, signal.SIGTERM])
        self.assertEqual(stop.said(), "stop: SIGTERM: the server stops")

    async def test_a_signal_during_the_start_waits_for_it(self):
        stop = self.installed()
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.05)  # the start's next await: noted, nothing else
        self.assertTrue(stop.stopping.is_set())
        stop.serving = True  # main(): the start is done
        self.assertTrue(await stop.wait(1))
        self.assertEqual(stop.said(), "stop: SIGTERM during the start: the server stops now "
                                      "that it is done")

    async def test_the_relay_is_cancelled_at_its_await(self):
        stop = self.installed()
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
        stop = self.installed()
        stop.end()  # main(), after the saves
        signal.raise_signal(signal.SIGINT)  # nothing happens
        signal.raise_signal(signal.SIGTERM)
        await asyncio.sleep(0.05)
        self.assertEqual(stop.came, [])
        self.assertFalse(stop.stopping.is_set())
        stop.end()  # (run() again, after main())
        stop.quiet()  # run(), last
        stop.close()
        for number in srv.STOP_SIGNALS:
            self.assertEqual(signal.getsignal(number), signal.SIG_IGN)
        signal.raise_signal(signal.SIGINT)
        signal.raise_signal(signal.SIGTERM)
        self.assertEqual(stop.came, [])


class EndTest(unittest.TestCase):
    def setUp(self):
        put_back(self)

    def test_python_s_default_action_is_never_put_back(self):
        """From install() to quiet() the handler is ours, then SIG_IGN: never SIG_DFL, nor
        Python's default_int_handler, not even for a moment (the loop's close put them back
        when the loop had the signals, and a SIGTERM then, taken by a thread still running,
        ended a stop of a flood of signals with -15 after its saves)."""
        seen = []
        real = signal.signal

        def setting(number, handler):
            seen.append(handler)
            return real(number, handler)
        loop = asyncio.new_event_loop()
        with mock.patch.object(signal, "signal", setting):
            try:
                stop = srv.StopRequest().install(loop)
                self.addCleanup(stop.close)
                held = srv.hold_stop_signals()
                srv.release_stop_signals(held)
                stop.end()
            finally:
                loop.close()
            stop.quiet()
        self.assertTrue(seen)
        self.assertNotIn(signal.SIG_DFL, seen)
        self.assertNotIn(signal.default_int_handler, seen)
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
        them is not a handler's call each (a flood of thousands a second starved the boards'
        write past the stop's 20 s)."""
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


class BreakInTest(unittest.TestCase):
    """The last resort (StopRequest.watch): a loop held past the stop's limit, so that it never
    takes the signal's note, is broken into, and the stop runs; run() as the server has it,
    with a main() of the test's."""

    def setUp(self):
        put_back(self)

    def run_held(self, hold, at_stop=None, limit=0.3):
        steps = []

        async def stopping():
            steps.append("stop")
            self.stop_saw = SET_IN_MAIN.get()
            if at_stop is not None:
                at_stop()
            steps.append("saved")

        async def main(stop):
            self.stop = stop
            SET_IN_MAIN.set("main")  # (the context main() runs in: the stop's too)
            stop.limit = limit
            stop.at_stop = stopping
            stop.serving = True

            async def holder():
                await asyncio.sleep(0.05)
                signal.raise_signal(signal.SIGTERM)  # noted: the loop is held before it takes it
                hold()
                steps.append("free")
            asyncio.get_running_loop().create_task(holder())
            try:
                while not await stop.wait(1):
                    pass
            finally:
                await stop.stop_once()
        with mock.patch.object(srv, "main", main):
            began = time.monotonic()
            srv.run()
        return steps, time.monotonic() - began

    @staticmethod
    def spin(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            pass

    def test_a_loop_free_before_the_limit_takes_the_signal_itself(self):
        with self.assertNoLogs("forza-live", "WARNING"):
            steps, took = self.run_held(lambda: self.spin(0.1))
        self.assertEqual(steps, ["free", "stop", "saved"])

    def test_a_loop_held_past_the_limit_is_broken_into(self):
        with self.assertLogs("forza-live", "WARNING") as logs:
            steps, took = self.run_held(lambda: self.spin(30))
        self.assertEqual(steps, ["stop", "saved"])  # the hold never ended by itself
        self.assertLess(took, 5)
        self.assertRegex(logs.output[0], r"stop: SIGTERM came 0\.3 s ago and the event loop has "
                                         r"not taken it: .* the stop breaks into it")

    def test_the_stop_after_a_break_in_runs_in_main_s_context(self):
        """The stop that run() runs after a break-in (finish) sees the context main() runs
        in, as main()'s own stop would."""
        with self.assertLogs("forza-live", "WARNING"):
            steps, took = self.run_held(lambda: self.spin(30))
        self.assertEqual(steps, ["stop", "saved"])
        self.assertEqual(self.stop_saw, "main")

    def test_the_stop_s_own_tasks_are_let_go_once_done(self):
        with self.assertLogs("forza-live", "WARNING"):
            steps, took = self.run_held(lambda: self.spin(30))
        self.assertEqual(steps, ["stop", "saved"])
        self.assertEqual(self.stop.own_tasks, set())

    def test_a_nan_limit_set_by_code_is_never_and_the_thread_ends(self):
        """A limit of nan (code, not the command line: it refuses nan) is no limit: the
        watching thread ends, rather than spin (Event.wait(nan) returns at once)."""
        stop = srv.StopRequest()
        stop.limit, stop.serving = float("nan"), True
        waits = []
        real = stop.begun.wait
        stop.begun.wait = lambda seconds=None: waits.append(seconds) or real(seconds)
        read, write = os.pipe()
        self.addCleanup(os.close, read)
        self.addCleanup(os.close, write)
        os.write(write, b"!")  # the first stop signal
        thread = threading.Thread(target=stop.watch, args=(read,), daemon=True)
        thread.start()
        thread.join(1)
        alive = thread.is_alive()
        stop.closing = True
        stop.begun.set()
        thread.join(5)
        self.assertFalse(alive, "%d waits in 1 s" % len(waits))
        self.assertLess(len(waits), 5)

    def test_a_try_where_python_only_prints_it_is_followed_by_another(self):
        """The loop held inside weakref callbacks first: a BreakIn raised there ends the
        callback and is dropped ("Exception ignored"); the next try, 0.1 s later, is not."""
        printed = []

        def hold():
            end = time.monotonic() + 1.0
            while time.monotonic() < end:
                obj = Dropped()
                ref = weakref.ref(obj, lambda ref: self.spin(0.25))
                del obj  # the callback runs here
            self.spin(30)
        with mock.patch("sys.unraisablehook", printed.append), \
                self.assertLogs("forza-live", "WARNING"):
            steps, took = self.run_held(hold)
        self.assertEqual(steps, ["stop", "saved"])
        self.assertTrue(any(isinstance(p.exc_value, srv.BreakIn) for p in printed), printed)
        self.assertLess(took, 5)

    def test_never_raised_in_the_stop_s_own_code(self):
        """A try of the break-in that lands in main()'s own frame (its finally, before the stop
        has begun) or once the stop has begun is nothing: there it would skip the stop."""
        stop = srv.StopRequest()
        stop.forcing = True
        coro = srv.main(stop)
        self.addCleanup(coro.close)
        stop.poking = True
        self.assertIsNone(stop.schedule(signal.SIGTERM, coro.cr_frame))
        self.assertFalse(stop.poking)
        stop.poking = True
        with self.assertRaises(srv.BreakIn):
            stop.schedule(signal.SIGTERM, sys._getframe())
        stop.poking, stop.began = True, True
        self.assertIsNone(stop.schedule(signal.SIGTERM, sys._getframe()))
        self.assertEqual(stop.came, [])  # none of them was a signal that came

    def test_a_limit_that_is_not_seconds_is_refused(self):
        """--stop-limit inf crashed the watching thread (OverflowError in its wait) once a
        signal came: inf, nan, a negative number or past STOP_LIMIT_MAX are refused at the
        command line."""
        for text in ("inf", "-inf", "nan", "-1", "1e300", "3601", "five"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=text):
                srv.stop_limit(text)
        self.assertEqual(srv.stop_limit("0"), 0)
        self.assertEqual(srv.stop_limit("2.5"), 2.5)
        refused = subprocess.run([PYTHON, os.path.join(ROOT, "forza_live_server.py"),
                                  "--stop-limit", "inf"], cwd=ROOT, capture_output=True,
                                 text=True, stdin=subprocess.DEVNULL, timeout=60)
        self.assertEqual(refused.returncode, 2)
        self.assertIn("argument --stop-limit: 'inf': seconds from 0 (never) to 3600",
                      refused.stderr)

    def test_minus_zero_is_refused(self):
        """-0 is a negative number as it is written, as -1 is: refused, not taken as 0."""
        for text in ("-0", "-0.0", "-nan"):
            with self.assertRaises(argparse.ArgumentTypeError, msg=text):
                srv.stop_limit(text)
        self.assertEqual(srv.stop_limit("+0"), 0)

    def test_a_negative_limit_as_its_own_word_gets_the_same_message(self):
        """--stop-limit -inf, two words: argparse took -inf for an option ("expected one
        argument"); the value is refused as --stop-limit=-inf is."""
        for value in ("-inf", "-1e5"):
            refused = subprocess.run([PYTHON, os.path.join(ROOT, "forza_live_server.py"),
                                      "--stop-limit", value], cwd=ROOT, capture_output=True,
                                     text=True, stdin=subprocess.DEVNULL, timeout=60)
            self.assertEqual(refused.returncode, 2, refused.stderr)
            self.assertIn("argument --stop-limit: %r: seconds from 0 (never) to 3600" % value,
                          refused.stderr)

    def test_glued_only_takes_a_number_after_stop_limit(self):
        self.assertEqual(srv.glued(["--stop-limit", "-inf", "-v"]), ["--stop-limit=-inf", "-v"])
        self.assertEqual(srv.glued(["--stop-limit", "-v"]), ["--stop-limit", "-v"])
        self.assertEqual(srv.glued(["--stop-limit", "2", "--port", "-1"]),
                         ["--stop-limit", "2", "--port", "-1"])
        self.assertEqual(srv.glued(["--stop-limit"]), ["--stop-limit"])

    def test_never_inside_the_stop(self):
        """Once the stop has begun, it is never broken into, however long it holds the loop."""
        with self.assertLogs("forza-live", "WARNING"):
            steps, took = self.run_held(lambda: self.spin(30), at_stop=lambda: self.spin(1.0))
        self.assertEqual(steps, ["stop", "saved"])
        self.assertGreater(took, 1.0)


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
# A server whose loop is held, from the moment a file appears, by a pure-Python loop, a sleep or
# a C call that signals do not end (a pthread mutex another thread holds): KIND SECONDS FILE.
# edge, chain: held until just before the stop's limit (5 ms, 20 ms), then by a callback of
# 0.1 s, 0.3 s: the signal's note has run and main()'s wakeup waits behind that callback, which
# the break-in lands in. task: the same as chain, but what holds the loop 0.3 s is a connection's
# task (asyncio.start_server's, as http_handler is), woken as the hold ends
HELD = """
import asyncio, ctypes, logging, os, socket, sys, threading, time
sys.path.insert(0, os.getcwd())
from forza_live import server as srv
log = logging.getLogger("forza-live")
KIND, SECONDS, TRIGGER = sys.argv[1], float(sys.argv[2]), sys.argv[3]
def c_hold(seconds):
    libc = ctypes.CDLL(None)
    mutex = ctypes.create_string_buffer(64)
    held = threading.Event()
    def owner():
        libc.pthread_mutex_lock(mutex)
        held.set()
        time.sleep(seconds)
        libc.pthread_mutex_unlock(mutex)
    threading.Thread(target=owner, daemon=True).start()
    held.wait()
    libc.pthread_mutex_lock(mutex)  # (the GIL let go of: the other threads run)
    libc.pthread_mutex_unlock(mutex)
def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass
def hold(stop):
    log.info("test: the loop is held")
    if KIND in ("edge", "chain", "task"):  # free just before the limit, then held again
        while stop.first_at is None:
            pass
        before, again = (0.005, 0.1) if KIND == "edge" else (0.02, 0.3)
        spin(stop.first_at + stop.limit - before - time.monotonic())
        if KIND == "task":
            go.set()  # (the connection's task: its wakeup queued before main()'s)
        else:
            asyncio.get_running_loop().call_soon(spin, again)
    elif KIND == "python":
        end = time.monotonic() + SECONDS
        while time.monotonic() < end:
            pass
    elif KIND == "sleep":
        time.sleep(SECONDS)
    else:
        c_hold(SECONDS)
    log.info("test: the loop is free")
go = asyncio.Event()
async def connection(reader, writer):
    await go.wait()
    spin(0.3)
    writer.close()
async def trigger(stop):
    if KIND == "task":
        listening = await asyncio.start_server(connection, "127.0.0.1", 0)
        client = socket.create_connection(listening.sockets[0].getsockname()[:2])
    while not os.path.exists(TRIGGER):
        await asyncio.sleep(0.01)
    hold(stop)
real_main = srv.main
async def main(stop):
    asyncio.get_running_loop().create_task(trigger(stop))
    await real_main(stop)
srv.main = main
sys.argv = ["forza_live_server.py"] + sys.argv[4:]
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

    def held(self, kind, seconds, *flags):
        """A server whose loop is held (HELD), and a SIGTERM 0.5 s into the hold."""
        trigger = os.path.join(self.temp.name, "hold")
        proc, log, http = self.start(*flags, head=[PYTHON, "-c", HELD, kind, str(seconds),
                                                   trigger])
        self.ready(proc, http)
        open(trigger, "w").close()
        deadline = time.monotonic() + 30
        while "test: the loop is held" not in (log.seek(0) or log.read()):
            self.assertLess(time.monotonic(), deadline, "not held")
            time.sleep(0.01)
        time.sleep(0.5)
        proc.send_signal(signal.SIGTERM)
        return proc, log

    def test_a_loop_held_3_s_stops_once_it_is_free(self):
        """Under the stop's limit (5 s): the loop takes the signal itself, once it is free."""
        proc, log = self.held("python", 3)
        status, lines = self.ended(proc, log)
        self.assert_stopped_cleanly(status, lines)
        said = [line.split(" ", 2)[2] for line in lines]
        self.assertLess(said.index("test: the loop is free"),
                        said.index("stop: SIGTERM: the server stops"), said)
        self.assertFalse(any("breaks into" in line for line in said), said)

    def test_a_loop_held_past_the_limit_is_broken_into_and_saves(self):
        """A loop held by Python code past --stop-limit: broken into, the stop and every save,
        exit status 0, long before the container runtime's kill."""
        began = time.monotonic()
        proc, log = self.held("python", 60, "--stop-limit", "1")
        said = self.assert_stopped_cleanly(*self.ended(proc, log))
        self.assertLess(time.monotonic() - began, 15)
        log.seek(0)
        lines = [line.split(" ", 2)[2] for line in log.read().splitlines()]
        self.assertNotIn("test: the loop is free", lines)  # it was broken into
        told = [i for i, line in enumerate(lines) if line.startswith(
            "stop: SIGTERM came 1 s ago and the event loop has not taken it")]
        self.assertEqual(len(told), 1, lines)
        self.assertLess(told[0], lines.index("stop: SIGTERM: the server stops"))
        self.assertEqual(said[0], "stop: SIGTERM: the server stops")

    def assert_broken_into_at_the_limit(self, kind):
        proc, log = self.held(kind, 0, "--stop-limit", "1")
        status, lines = self.ended(proc, log)
        self.assert_stopped_cleanly(status, lines)
        said = [line.split(" ", 2)[2] for line in lines]
        self.assertTrue(any(line.endswith("the stop breaks into it") for line in said), said)

    def test_a_loop_free_at_the_limit_then_held_by_a_callback(self):
        """The loop frees itself as the break-in comes, and the break-in lands in the next
        callback: main()'s stop runs first (its wakeup was queued before run()'s own task for
        the stop), and that task is part of the stop, not cancelled with the others (it was:
        a CancelledError out of run(), exit status 1, after every save)."""
        self.assert_broken_into_at_the_limit("edge")

    def test_a_loop_free_before_the_limit_then_held_again(self):
        self.assert_broken_into_at_the_limit("chain")

    def test_a_connection_s_task_broken_into(self):
        """The break-in lands in a connection's task (http_handler's, from asyncio's streams):
        the task the BreakIn ended is no error to log (asyncio logged it, "Unhandled exception
        in client_connected_cb" and a traceback, after the saves)."""
        self.assert_broken_into_at_the_limit("task")

    def test_a_loop_held_in_a_sleep_is_broken_into(self):
        proc, log = self.held("sleep", 60, "--stop-limit", "1")
        self.assert_stopped_cleanly(*self.ended(proc, log))

    @unittest.skipUnless(sys.platform.startswith("linux"), "a pthread mutex through ctypes")
    def test_a_call_that_cannot_be_broken_into_ends_before_the_kill(self):
        """A C call that signals do not end: the stop waits for it, and is broken into as soon
        as it returns, with every save."""
        proc, log = self.held("c", 4, "--stop-limit", "1")
        status, lines = self.ended(proc, log)
        self.assert_stopped_cleanly(status, lines)
        said = [line.split(" ", 2)[2] for line in lines]
        self.assertTrue(any(line.endswith("the stop breaks into it") for line in said), said)
        self.assertNotIn("test: the loop is free", said)  # broken into as the call returned

    @unittest.skipUnless(sys.platform.startswith("linux"), "a pthread mutex through ctypes")
    def test_a_call_that_cannot_be_broken_into_past_the_kill(self):
        """Not ended by the container runtime's kill (here at 8 s): nothing can save there. The
        log says what the stop waited for; exit status -9, no save made."""
        proc, log = self.held("c", 60, "--stop-limit", "1")
        try:
            proc.wait(8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        self.assertEqual(proc.returncode, -signal.SIGKILL)
        log.seek(0)
        text = log.read()
        self.assertIn("the stop breaks into it", text)
        self.assertIn("stop: the event loop is still held 5 s later: inside a call that cannot "
                      "be broken into; the stop waits for it", text)
        self.assertNotIn("stop: SIGTERM: the server stops", text)
        self.assertFalse(os.path.exists(os.path.join(self.data, "notifications.json")))

    def test_nothing_new_is_taken_after_the_saves(self):
        """During the wait for a Discord status update after the saves, no datagram is answered
        (no player joins) and a connection opened before the stop gets no answer: nothing
        changes a store after its save."""
        proc, log, http = self.start(head=[PYTHON, "-c", SLOW_DISCORD])
        udp = int(proc.args[proc.args.index("--port") + 1])
        self.ready(proc, http)
        before = socket.create_connection(("127.0.0.1", http), timeout=5)
        self.addCleanup(before.close)
        request = b"GET /status HTTP/1.1\r\nHost: x\r\nConnection: keep-alive\r\n\r\n"
        before.sendall(request)
        self.assertTrue(before.recv(65536).startswith(b"HTTP/1.1 200"))
        deadline = time.monotonic() + 30
        while "test: the status update starts" not in (log.seek(0) or log.read()):
            self.assertLess(time.monotonic(), deadline, "no status update")
            time.sleep(0.02)
        proc.send_signal(signal.SIGTERM)
        while not re.search(r"scoreboards: .*at the stop", log.seek(0) or log.read()):
            self.assertLess(time.monotonic(), deadline, "no saves")
            time.sleep(0.01)
        game = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(game.close)
        game.settimeout(0.5)
        game.sendto(hello(1, PLAYER, "Late"), ("127.0.0.1", udp))
        try:
            answer = game.recv(2048)
        except OSError:  # nothing, or the port's refusal
            answer = b""
        self.assertEqual(answer, b"")
        try:
            before.sendall(request)
            late = before.recv(65536)
        except OSError:
            late = b""
        self.assertEqual(late, b"")
        status, lines = self.ended(proc, log)
        text = "\n".join(lines)
        self.assertEqual(status, 0, text)
        self.assertNotIn("Traceback", text)
        self.assertNotIn("join", text)
        self.assertIn("test: the status update ends", text)  # the wait was under way meanwhile
        said = [line.split(" ", 2)[2] for line in lines]
        self.assertGreater(said.index("test: the status update ends"),
                           [i for i, line in enumerate(said) if line.startswith("scoreboards: ")][0])

    def test_a_step_before_the_saves_that_fails_does_not_skip_them(self):
        proc, log, http = self.start(head=[PYTHON, "-c", FAILED_STEP])
        self.ready(proc, http)
        proc.send_signal(signal.SIGTERM)
        status, lines = self.ended(proc, log)
        text = "\n".join(lines)
        self.assertEqual(status, 0, text)
        self.assertIn("stop: a step before the saves failed; the saves are made all the same",
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
