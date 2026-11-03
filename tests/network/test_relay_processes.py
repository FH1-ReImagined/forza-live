"""A real main server and a real --relay-only relay on this machine (FH1 #516): the relay links,
two games that keep relaying through the main server are handed the relay, bind there and reach
each other through it; the relay keeps forwarding while the main server restarts, and links to
the restarted server again."""

import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

from forza_live.core import relays
from forza_live.network import relay_broker as rb
from forza_live.network import relay_node as rn
from tests.support import ROOT

MAGIC = b"RLV1"
HELLO, WELCOME, RELAY, FORWARD = 1, 2, 5, 6
PYTHON = getattr(sys, "_base_executable", sys.executable)


def free_port(kind=socket.SOCK_DGRAM):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Game:
    """A game's UDP socket, as far as the relays go."""

    def __init__(self, n):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.05)
        self.n, self.xuid = n, 0x0009000000000000 + n
        self.id = None
        self.offer = None  # (relay address, ticket, peer ids)
        self.probes = []
        self.got = []  # (from, sender id, payload) of FORWARDs
        self.bound = False
        self.challenges = 0

    def hello(self, main):
        name = b"Driver%d" % self.n
        body = struct.pack(">HQQIH", 1, 1000 + self.n, self.xuid, 0, 0) + b"\x04#TU4" + \
            bytes([len(name)]) + name + bytes([rb.FEATURE_RELAYS | 0x01])
        self.sock.sendto(MAGIC + bytes([HELLO]) + body, main)

    def pump(self, seconds=0.1):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (socket.timeout, ConnectionResetError):
                continue
            kind, body = data[4], data[5:]
            if kind == WELCOME:
                self.id = struct.unpack_from(">I", body)[0]
            elif kind == FORWARD:
                self.got.append((addr, struct.unpack_from(">I", body)[0], body[20:]))
            elif kind == rn.RELAY_BOUND:
                self.bound = True
            elif kind == rn.RELAY_CHALLENGE and self.offer:  # prove the address: bind again
                self.challenges += 1
                self.sock.sendto(MAGIC + bytes([rn.RELAY_BIND]) + self.offer[1] + body, addr)
            elif kind == rn.RELAY_INFO and body[0] == rb.OP_PROBE:
                self.probes.append(body)
            elif kind == rn.RELAY_INFO and body[0] == rb.OP_OFFER:
                ip, port, n = struct.unpack_from(">IHB", body, 1)
                ticket, count = body[8:8 + n], body[8 + n]
                ids = list(struct.unpack_from(">%dI" % count, body, 9 + n))
                self.offer = ((socket.inet_ntoa(struct.pack(">I", ip)), port), ticket, ids)

    def relay(self, to, dst_id, payload):
        self.sock.sendto(MAGIC + bytes([RELAY]) + struct.pack(">IHH", dst_id, 1000, 1000) + payload, to)

    def close(self):
        self.sock.close()


class RelayProcesses(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.data = os.path.join(self.temp.name, "main")
        self.main_port, self.http = free_port(), free_port(socket.SOCK_STREAM)
        self.relay_port = free_port()
        secret_file = relays.add_relay(self.data, "local-1", "Local relay",
                                       "127.0.0.1:%d" % self.relay_port, "Here")
        self.procs = []
        self.start_main()
        self.relay = self.spawn(["--relay-only", "--host", "127.0.0.1", "--port",
                                 str(self.relay_port), "--relay-of", "127.0.0.1:%d" % self.main_port,
                                 "--relay-id", "local-1", "--relay-secret-file", secret_file,
                                 "--relay-allow-private"])
        self.games = [Game(1), Game(2)]
        for g in self.games:
            self.addCleanup(g.close)

    def spawn(self, args):
        proc = subprocess.Popen([PYTHON, os.path.join(ROOT, "forza_live_server.py")] + args,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.procs.append(proc)
        self.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc):
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()

    def start_main(self):
        self.main = self.spawn(["--host", "127.0.0.1", "--port", str(self.main_port), "--http-port",
                                str(self.http), "--data-dir", self.data, "--relay-after", "1",
                                "--relay-probation", "0"])

    def status(self):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/status" % self.http, timeout=2) as r:
                return json.loads(r.read())
        except (urllib.error.URLError, OSError, ValueError):
            return None

    def wait(self, what, seconds, step=None):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if step:
                step()
            for g in self.games:
                g.pump(0.05)
            if what():
                return True
        return False

    def relay_up(self):
        s = self.status()
        return bool(s and s.get("relays") and s["relays"][0]["up"])

    def test_two_games_through_a_local_relay(self):
        main = ("127.0.0.1", self.main_port)
        relay = ("127.0.0.1", self.relay_port)
        a, b = self.games
        self.assertTrue(self.wait(self.relay_up, 30), "the relay did not link")
        self.assertTrue(self.wait(lambda: a.id and b.id and a.probes and b.probes, 10,
                                  step=lambda: [g.hello(main) for g in self.games]))
        # the two games cannot punch: they relay through the main server until it hands them the
        # relay (--relay-after 1)
        self.assertTrue(self.wait(lambda: a.offer and b.offer, 10,
                                  step=lambda: (a.relay(main, b.id, b"via main"),
                                                b.relay(main, a.id, b"via main"))))
        self.assertTrue(any(src == main for src, _, _ in b.got))
        self.assertEqual(a.offer[0], relay)
        self.assertEqual((a.offer[2], b.offer[2]), ([b.id], [a.id]))
        for g in self.games:
            g.sock.sendto(MAGIC + bytes([rn.RELAY_BIND]) + g.offer[1], relay)
        self.assertTrue(self.wait(lambda: a.bound and b.bound, 5))
        self.assertEqual((a.challenges, b.challenges), (1, 1))  # each proved its address
        self.assertEqual(self.status()["relays"][0]["listed"], True)  # its probe passed
        a.got.clear()
        b.got.clear()
        self.assertTrue(self.wait(lambda: any(src == relay for src, _, _ in b.got) and
                                  any(src == relay for src, _, _ in a.got), 5,
                                  step=lambda: (a.relay(relay, b.id, b"via relay"),
                                                b.relay(relay, a.id, b"via relay"))))
        self.assertIn((relay, a.id, b"via relay"), b.got)
        self.assertIn((relay, b.id, b"via relay"), a.got)
        # the main server restarts: the relay keeps forwarding, and links to it again
        self.stop(self.main)
        b.got.clear()
        self.assertTrue(self.wait(lambda: (relay, a.id, b"still") in b.got, 5,
                                  step=lambda: a.relay(relay, b.id, b"still")))
        self.start_main()
        self.assertTrue(self.wait(self.relay_up, 30), "the relay did not link again")
        self.assertIsNone(self.relay.poll())


if __name__ == "__main__":
    unittest.main()
