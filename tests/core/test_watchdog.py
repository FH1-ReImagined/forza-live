"""The event loop's watchdog (core/watchdog.py, #74): what holds the loop over the limit is a line
in the log with what it was and how long, the worst ones also where it was, at most
LINES_PER_MINUTE lines a minute of each kind, and /status has the numbers. The loop is held with
time.sleep, never a busy loop, and every time is checked with a wide margin: a busy machine
makes the work slower, never faster."""

import asyncio
import json
import os
import re
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from unittest import mock

from forza_live import server as srv
from forza_live.core import watchdog
from forza_live.title_services import lsp
from tests.support import FakeTransport, ROOT

HELD = re.compile(r"held the event loop (\d+) ms")
LATE = re.compile(r"the event loop was (\d+) ms late")


def hold_the_loop(seconds):
    """What a store that writes its file on the loop does to the players (the stack test looks
    for this name)."""
    time.sleep(seconds)


def ms_of(line, pattern=HELD):
    found = pattern.search(line)
    return int(found.group(1)) if found else None


def presence():
    return srv.MAGIC + bytes([srv.PRESENCE]) + bytes(12)


class LoopTest(unittest.IsolatedAsyncioTestCase):
    """The server's watchdog (watchdog.WATCH) started on the test's loop, and a server with one
    player."""

    LIMIT_MS, STACK_MS = 50, 0

    async def asyncSetUp(self):
        self.watch = watchdog.WATCH
        self.addCleanup(self.watch.configure, 0)  # as every other test finds it: off
        self.watch.configure(self.LIMIT_MS, self.STACK_MS)
        self.watch.start()
        self.addCleanup(self.watch.stop)
        self.loop = asyncio.get_running_loop()
        self.server = srv.ForzaLive("test", 64)
        self.addCleanup(self.server.events.db.close)
        self.server.transport = FakeTransport()
        self.peer = srv.Peer(7, ("127.0.0.1", 20007), 7, 0x0009000000000007, "Racer", "#TU4",
                             (0, 0))
        self.server.peers[self.peer.id] = self.peer
        self.server.by_addr[self.peer.addr] = self.peer

    async def lines(self, logs, pattern, within=5.0):
        """The log's lines that match, once there is one (or after `within` seconds)."""
        deadline = time.monotonic() + within
        while True:
            found = [line for line in logs.output if re.search(pattern, line)]
            if found or time.monotonic() > deadline:
                return found
            await asyncio.sleep(0.02)

    async def datagram(self, data, addr=None):
        """A datagram as the transport hands it over: a callback of the loop, not inside a
        task."""
        done = self.loop.create_future()

        def deliver():
            self.server.datagram_received(data, addr or self.peer.addr)
            done.set_result(None)
        self.loop.call_soon(deliver)
        await done

    async def settle(self):
        """Long enough for the heartbeat to have woken a few times since."""
        await asyncio.sleep(0.15)


class EntryPointsTest(LoopTest):
    async def test_a_datagram_that_blocks_is_a_line_with_its_type_player_and_time(self):
        with mock.patch.object(self.server, "on_presence", lambda peer, body: hold_the_loop(0.1)):
            with self.assertLogs("forza-live", "WARNING") as logs:
                await self.datagram(presence())
                await self.settle()
        held = [line for line in logs.output if "held the event loop" in line]
        self.assertEqual(len(held), 1, logs.output)
        self.assertIn("loop: datagram PRESENCE from #7 'Racer' held the event loop", held[0])
        self.assertGreaterEqual(ms_of(held[0]), 100)
        self.assertLess(ms_of(held[0]), 10000)
        self.assertIn("(limit 50 ms)", held[0])
        self.assertNotIn("127.0.0.1", "\n".join(logs.output))  # never the player's address
        self.assertNotIn("20007", "\n".join(logs.output))
        # the heartbeat was late too, but for a unit that has its line: no second line
        self.assertEqual([line for line in logs.output if "late" in line], [])

    async def test_a_fast_datagram_is_no_line(self):
        with self.assertNoLogs("forza-live", "WARNING"):
            for _ in range(3):
                await self.datagram(presence())
            await self.settle()
        self.assertEqual(self.watch.status()["event_loop"]["over_limit_last_hour"], 0)

    async def test_an_unregistered_sender_is_named_by_nobody(self):
        with mock.patch.object(self.server, "on_hello", lambda body, addr: hold_the_loop(0.1)):
            with self.assertLogs("forza-live", "WARNING") as logs:
                await self.datagram(srv.MAGIC + bytes([srv.HELLO]) + bytes(30),
                                    ("198.51.100.4", 4000))
        self.assertRegex(logs.output[0], r"loop: datagram HELLO held the event loop \d+ ms")
        self.assertNotIn("198.51.100.4", "\n".join(logs.output))

    async def http(self, request):
        """One request to http_handler on a port of the test's: the answer."""
        srv.http_conns.clear()
        srv.http_open.clear()
        listener = await asyncio.start_server(lambda r, w: srv.http_handler(self.server, r, w),
                                              "127.0.0.1", 0)
        try:
            port = listener.sockets[0].getsockname()[1]
            reader, writer = await asyncio.open_connection("127.0.0.1", port)
            writer.write(request)
            await writer.drain()
            answer = await asyncio.wait_for(reader.read(), 10)
            writer.close()
            return answer
        finally:
            listener.close()
            await listener.wait_closed()

    async def test_a_title_service_call_that_blocks_is_its_service_and_method(self):
        def slow(server, method, path, headers, body, peer):
            hold_the_loop(0.1)
            return 200, "bin/xtsw", b""
        request = (b"POST /Services.Horizon/o.xtsw HTTP/1.1\r\nHost: x\r\n"
                   b"X-ClassName: Forza.WebServices.Scoreboard, Forza\r\nX-Method: SubmitScore\r\n"
                   b"X-UserId: 0009000000000007\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
        with mock.patch.object(lsp, "handle", slow):
            with self.assertLogs("forza-live", "WARNING") as logs:
                answer = await self.http(request)
                await self.settle()
        self.assertTrue(answer.startswith(b"HTTP/1.1 200"))
        held = [line for line in logs.output if "held the event loop" in line]
        self.assertEqual(len(held), 1, logs.output)
        self.assertIn("loop: title Scoreboard.SubmitScore held the event loop", held[0])
        self.assertGreaterEqual(ms_of(held[0]), 100)
        self.assertNotIn("127.0.0.1", held[0])
        self.assertNotIn("0009000000000007", held[0])  # the title lines name no player
        self.assertEqual([line for line in logs.output if "late" in line], [])

    async def test_a_web_route_and_an_operator_route_are_their_routes(self):
        with mock.patch.object(self.server, "status",
                               lambda: (hold_the_loop(0.1), {"server": "forza-live"})[1]):
            with self.assertLogs("forza-live", "WARNING") as logs:
                await self.http(b"GET /status HTTP/1.1\r\nConnection: close\r\n\r\n")
        self.assertIn("loop: http GET /status held the event loop", logs.output[0])
        self.addCleanup(setattr, lsp, "ADMIN_TOKEN", lsp.ADMIN_TOKEN)
        lsp.ADMIN_TOKEN = ""  # this machine's requests are the operator's
        with mock.patch.object(self.server.chat, "moderation",
                               lambda: (hold_the_loop(0.1), {})[1]):
            with self.assertLogs("forza-live", "WARNING") as logs:
                await self.http(b"GET /admin/chat HTTP/1.1\r\nConnection: close\r\n\r\n")
        self.assertIn("loop: operator GET /admin/chat held the event loop", logs.output[0])

    async def test_a_task_and_the_main_loops_tick_are_their_names(self):
        async def keep_busy():
            hold_the_loop(0.1)
            await asyncio.sleep(0)
        with self.assertLogs("forza-live", "WARNING") as logs:
            await asyncio.create_task(keep_busy())
            self.watch.call("expire", hold_the_loop, 0.1)
        self.assertRegex(logs.output[0], r"loop: task \S*keep_busy held the event loop \d+ ms")
        self.assertIn("loop: task expire held the event loop", logs.output[1])

    def test_routes_name_no_ids_codes_or_queries(self):
        for path, route in (("/", "/"), ("/status", "/status"), ("/operator/", "/operator"),
                            ("/community/img/ABCDEFGHJK23.jpg", "/community/img/*"),
                            ("/owner/photos/ABCDEFGHJK23/delete", "/owner/photos/*"),
                            ("/admin/scores/rank?token=s3cret&xuid=1", "/admin/scores/rank"),
                            ("/storage/0009000000000007/ghost", "/storage/*"),
                            ("/a/b/c/d", "/a/b/c/*"), ("/%2e%2e/x", "/*")):
            self.assertEqual(watchdog.route_of(path), route, path)
        self.assertEqual(watchdog.describe_request("GET", "/gift?to=Racer&token=x", {}),
                         ("operator", "GET /gift"))
        self.assertEqual(watchdog.describe_request("BREW", "/leaderboards/7", {}),
                         ("http", "? /leaderboards/*"))
        self.assertEqual(watchdog.describe_request(
            "POST", "/Services.Horizon/o.xtsw",
            {"x-classname": "Xls.WebServices.Presence, Xls", "x-method": "Get\r\nX"}),
            ("title", "Xls.Presence.GetX"))


class RateLimitTest(LoopTest):
    async def test_at_most_n_lines_a_minute_of_each_kind_and_a_summary(self):
        now = [5000.0]
        self.watch.wall = lambda: now[0]
        self.addCleanup(setattr, self.watch, "wall", time.monotonic)
        n = watchdog.LINES_PER_MINUTE
        with self.assertLogs("forza-live", "WARNING") as logs:
            for i in range(n + 7):
                self.watch.report("datagram", "PRESENCE", None, watchdog.clock(), 0.06 + i / 1000)
            self.watch.report("title", "Hopper.GetGameOptions", None, watchdog.clock(), 0.07)
        self.assertEqual(sum("loop: datagram" in line for line in logs.output), n)
        self.assertEqual(sum("loop: title" in line for line in logs.output), 1)  # its own count
        now[0] += 61
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.watch.lines.flush()
        self.assertEqual(len(logs.output), 1)
        self.assertIn("loop: 7 more datagram line(s) were left out in that minute (the worst "
                      "held the loop 76 ms)", logs.output[0])
        with self.assertLogs("forza-live", "WARNING") as logs:  # a new minute
            self.watch.report("datagram", "PRESENCE", None, watchdog.clock(), 0.06)
        self.assertEqual(len(logs.output), 1)
        status = self.watch.status()["event_loop"]
        self.assertEqual(status["over_limit_last_hour"], n + 9)  # every one is counted
        self.assertEqual(status["worst_last_hour"]["ms"], 76)


class HeartbeatTest(LoopTest):
    async def test_a_stall_no_entry_point_times_is_noticed_with_how_late(self):
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.loop.call_soon(hold_the_loop, 0.15)  # a plain callback: nothing times it
            late = await self.lines(logs, "late")
        self.assertEqual(len(late), 1, logs.output)
        self.assertIn("loop: the event loop was", late[0])
        self.assertGreaterEqual(ms_of(late[0], LATE), 100)  # 150, less the heartbeat's period
        self.assertLess(ms_of(late[0], LATE), 10000)
        worst = self.watch.status()["event_loop"]["worst_last_hour"]
        self.assertEqual((worst["kind"], worst["what"]), ("stall", "untimed"))


class StackTest(LoopTest):
    STACK_MS = 100

    async def test_the_stack_names_the_blocking_function_once_per_stall(self):
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.loop.call_soon(hold_the_loop, 0.5)
            stack = await self.lines(logs, "so far")
            late = await self.lines(logs, "late")
        self.assertEqual(len(stack), 1, logs.output)  # once, though the thread looked often
        self.assertRegex(stack[0], r"loop: stall 1: the event loop has been held \d+ ms so far, "
                                   r"at hold_the_loop \(test_watchdog\.py:\d+\)")
        self.assertNotIn("base_events", stack[0])  # asyncio's own frames are left out
        self.assertIn("(stall 1)", late[0])  # the stall's line points to it
        with self.assertLogs("forza-live", "WARNING") as logs:  # a second stall: a second stack
            with mock.patch.object(self.server, "on_presence",
                                   lambda peer, body: hold_the_loop(0.4)):
                await self.datagram(presence())
            await self.lines(logs, "stall 2", within=1)
        self.assertTrue(any("loop: stall 2:" in line and "hold_the_loop" in line
                            for line in logs.output), logs.output)
        self.assertTrue(any("loop: datagram PRESENCE" in line and "(stall 2)" in line
                            for line in logs.output), logs.output)


class OffTest(LoopTest):
    LIMIT_MS = 0

    async def test_zero_turns_everything_off(self):
        self.assertIsNone(self.loop.get_task_factory())
        self.assertIsNone(self.watch.heart)
        self.assertIsNone(self.watch.thread)
        self.assertEqual(watchdog.LIMIT, float("inf"))
        with self.assertNoLogs("forza-live", "WARNING"):
            with mock.patch.object(self.server, "on_presence",
                                   lambda peer, body: hold_the_loop(0.1)):
                await self.datagram(presence())
            self.loop.call_soon(hold_the_loop, 0.1)
            await self.settle()
            self.watch.call("expire", hold_the_loop, 0.1)
        self.assertEqual(self.server.status()["event_loop"], {"limit_ms": 0})


class StopTest(LoopTest):
    async def test_stop_puts_the_loop_back(self):
        self.assertEqual(self.loop.get_task_factory(), self.watch.task_factory)
        self.watch.stop()
        self.assertIsNone(self.loop.get_task_factory())
        self.assertIsNone(self.watch.thread)
        with self.assertNoLogs("forza-live", "WARNING"):  # the stop's saves are not lines
            self.watch.call("save_at_stop", hold_the_loop, 0.1)


class StatusTest(LoopTest):
    async def test_status_has_the_last_hours_count_and_worst(self):
        now = [7200.0]
        self.watch.wall = lambda: now[0]
        self.addCleanup(setattr, self.watch, "wall", time.monotonic)
        self.assertEqual(self.server.status()["event_loop"],
                         {"limit_ms": 50, "over_limit_last_hour": 0, "over_limit_since_start": 0})
        with mock.patch.object(self.server, "on_presence", lambda peer, body: hold_the_loop(0.1)):
            with self.assertLogs("forza-live", "WARNING"):
                await self.datagram(presence())
        now[0] += 30
        status = json.loads(json.dumps(self.server.status()))["event_loop"]
        self.assertEqual(status["over_limit_last_hour"], 1)
        self.assertEqual(status["over_limit_since_start"], 1)
        worst = status["worst_last_hour"]
        self.assertEqual((worst["kind"], worst["what"], worst["seconds_ago"]),
                         ("datagram", "PRESENCE", 30))
        self.assertGreaterEqual(worst["ms"], 100)
        self.assertNotIn("Racer", json.dumps(status))  # who it was is the log's
        now[0] += 3600
        status = self.server.status()["event_loop"]
        self.assertEqual(status["over_limit_last_hour"], 0)
        self.assertEqual(status["over_limit_since_start"], 1)
        self.assertNotIn("worst_last_hour", status)


def free_port(kind):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FlagTest(unittest.TestCase):
    """The server's command line: the watchdog is on by default, --stall-limit 0 turns it off,
    and /status says which."""

    def run_server(self, *flags):
        data = tempfile.TemporaryDirectory()
        self.addCleanup(data.cleanup)
        http = free_port(socket.SOCK_STREAM)
        proc = subprocess.Popen(
            [getattr(sys, "_base_executable", sys.executable),
             os.path.join(ROOT, "forza_live_server.py"),
             "--host", "127.0.0.1", "--port", str(free_port(socket.SOCK_DGRAM)),
             "--http-port", str(http), "--data-dir", data.name, *flags],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        def end():
            proc.terminate()
            proc.wait()
        self.addCleanup(end)
        return proc, "http://127.0.0.1:%d/status" % http

    @staticmethod
    def status(proc, url):
        deadline = time.monotonic() + 30
        while True:
            try:
                return json.loads(urllib.request.urlopen(url, timeout=2).read())
            except (urllib.error.URLError, OSError):
                if proc.poll() is not None or time.monotonic() > deadline:
                    raise RuntimeError("the server did not start")
                time.sleep(0.05)

    def test_on_by_default_and_off_at_zero(self):
        default = self.run_server()
        off = self.run_server("--stall-limit", "0")
        tuned = self.run_server("--stall-limit", "80", "--stall-stack", "0")
        on = self.status(*default)["event_loop"]
        self.assertEqual(on["limit_ms"], 50)
        self.assertIsInstance(on["over_limit_last_hour"], int)  # 0 unless the machine is busy
        self.assertIsInstance(on["over_limit_since_start"], int)
        self.assertEqual(self.status(*off)["event_loop"], {"limit_ms": 0})
        self.assertEqual(self.status(*tuned)["event_loop"]["limit_ms"], 80)


if __name__ == "__main__":
    unittest.main()
