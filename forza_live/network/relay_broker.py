"""The main server's side of the volunteer relays (FH1 #516; the relays: relay_node.py).

Off unless the operator listed a relay (core/relays.py): then the server

- accepts the RELAY_LINKs of the listed relays (their HMAC with the relay's secret, a time within
  LINK_SKEW s and a nonce not seen before) and answers RELAY_LINKED; a relay not listed, or with a
  wrong HMAC, gets an ERROR (relay_node.ERROR_REFUSED) naming its nonce. A relay is up while its
  link was heard in the last LINK_TIMEOUT s and its address (the operator's, resolved) is known.
- tells games that can use relays (HELLO feature FEATURE_RELAYS) which relays to ping, and keeps
  the pings they report (RELAY_INFO, below).
- watches the server's own relay: two such games that still relay through this server after
  OFFER_AFTER s (longer than a game's hole punching, 6 s) could not connect directly. They are
  put in a relay session, on the up relay with the lowest worst ping of the two (a relay either
  could not reach is never picked; then the fewest sessions), or in the session one of them is in
  already, and each gets an offer: the relay's address, its own ticket for that session, and the
  players it reaches there. A game keeps relaying through this server until its relay answers
  its bind, and falls back to it when the offer is withdrawn (ip 0) or its ticket runs out.
  Tickets last TICKET_LIFE s and are renewed at half of that; a session whose relay goes down is
  dissolved and its players get a withdrawal.

- probes every listed relay (PROBE_EVERY s; PROBE_RETRY s while it fails or is not listed):
  its link must be up and it must echo a RELAY_PING of a fresh nonce sent to the address the
  players get (PROBE_TRIES tries, PROBE_TIMEOUT s), the time of which is its probe ping. A new
  relay is used and published in /relays.json once a probe passes; one failing DROP_FAILS probes
  in a row drops out of both (its sessions dissolved) and comes back after RETURN_PASSES passes.
  What was listed, and since when, is kept in DATA_DIR/relay_health.json across restarts.
- puts a relay on probation for PROBATION_TIME s after it is listed: it gets only a
  PROBATION_SHARE of the new pairs it would be picked for, at most PROBATION_SESSIONS sessions at
  once. Its link reports what it can take (its slots, the bandwidth its operator offers): a full
  relay gets no new pairs.

Games without FEATURE_RELAYS, and every pair while no relay is up, are relayed by this server
as before.

RELAY_INFO (kind 27), op byte first:
  0 server -> game, the relays to ping: version u16, count u8, then (index u8, ip u32, port u16);
  1 game -> server, the pings: version u16, count u8, then (index u8, milliseconds u16; 0xFFFF:
    no answer);
  2 server -> game, an offer: ip u32, port u16, ticket (u8 length + bytes), count u8, then the
    peer ids (u32) it reaches through that relay. ip 0 (no ticket, no peers): use this server.
"""

import asyncio
import ipaddress
import json
import logging
import os
import random
import socket
import struct
import time

from forza_live.core import durable, relays
from forza_live.network import relay_node

log = logging.getLogger("forza-live")

FEATURE_RELAYS = 0x40
OP_PROBE, OP_PINGS, OP_OFFER = 0, 1, 2
OFFER_AFTER = 8.0     # seconds a pair relays through this server before it gets a relay
PAIR_GAP = 5.0        # a pair not relayed for this long starts over
REOFFER = 10.0        # a pair still relayed here this long after its offer gets it again
TICKET_LIFE = 600.0
LINK_TIMEOUT = 30.0
LINK_SKEW = 120.0
RESOLVE_EVERY = 600.0
NO_ANSWER = 0xFFFF
UNKNOWN_PING = 5000   # a relay a game did not report: picked only before none
MAX_OFFER_PEERS = 100
PROBE_EVERY = 180.0   # seconds between the probes of a listed relay that passes them
PROBE_RETRY = 60.0    # while it fails them, or is not listed
PROBE_TRIES = 3       # pings of one probe (one a tick, every 5 s)
PROBE_TIMEOUT = 14.0  # without an echo: the probe failed
PROBE_GRACE = 30.0    # after a start, the links' time to come up before the first probes
DROP_FAILS = 3        # failed probes in a row: the relay drops out
RETURN_PASSES = 2     # passed probes in a row: a relay that dropped out is listed again
PROBATION_TIME = 3600.0
PROBATION_SHARE = 0.1
PROBATION_SESSIONS = 4
HEALTH_FILE = "relay_health.json"


class Link:
    __slots__ = ("addr", "seen", "nonces", "slots", "sessions", "bytes", "max_slots", "bandwidth")

    def __init__(self):
        self.addr, self.seen, self.nonces = None, None, []
        self.slots = self.sessions = self.bytes = self.max_slots = self.bandwidth = 0


class Health:
    """What the probes found of one relay."""
    __slots__ = ("listed", "dropped", "since", "fails", "passes", "next_probe", "nonce", "started",
                 "sent", "tries", "probe_ms", "probed", "sessions_relayed")

    def __init__(self, next_probe, listed=False, dropped=False, since=None):
        self.listed, self.dropped = listed, dropped  # dropped: listed once, then dropped out
        self.since = since  # unix time it was listed (its probation runs from there)
        self.fails = self.passes = 0
        self.next_probe = next_probe
        self.nonce, self.started, self.sent, self.tries = None, 0.0, 0.0, 0
        self.probe_ms = None
        self.probed = False  # since this server started
        self.sessions_relayed = 0


class RelaySession:
    __slots__ = ("id", "relay", "members", "issued")

    def __init__(self, sid, relay):
        self.id, self.relay = sid, relay
        self.members = set()
        self.issued = {}  # peer id -> when its ticket was issued


def literal_ipv4(host):
    try:
        return ipaddress.IPv4Address(host).exploded
    except ValueError:
        return None


class RelayBroker:
    def __init__(self, server, rate=lambda: 256 * 1024, clock=time.monotonic, wall=time.time):
        self.server = server
        self.rate = rate
        self.clock, self.wall = clock, wall
        self.data_dir = None
        self.stamp = None
        self.entries = {}   # id -> the operator's entry
        self.secrets = {}   # id -> secret
        self.links = {}     # id -> Link
        self.resolved = {}  # id -> ((ip, port) or None, when)
        self.sessions = {}  # session id -> RelaySession
        self.of_peer = {}   # peer id -> session id
        self.pairs = {}     # (peer id, peer id) -> [first, last, next offer]
        self.pings = {}     # peer id -> {relay id: ms}
        self.probed = {}    # peer id -> version of the probe list it got
        self.version = 1
        self.probe_list = []  # relay ids, in the order of the probe list
        self.offer_after = OFFER_AFTER
        self.probation_time = PROBATION_TIME  # --relay-probation
        self.health = {}    # id -> Health
        self.saved_health = {}  # id -> what relay_health.json says (configure)
        self.random = random.random
        self.counters = {"offers": 0, "withdrawals": 0, "links_refused": 0}

    # --- the operator's list ---

    def configure(self, data_dir):
        self.data_dir = data_dir
        try:
            saved = durable.load(os.path.join(data_dir, HEALTH_FILE)).get("relays", {})
            self.saved_health = saved if isinstance(saved, dict) else {}
        except (OSError, ValueError, AttributeError):
            self.saved_health = {}
        self.reload()

    def reload(self):
        if self.data_dir is None:
            return
        current = relays.stamp(self.data_dir)
        if current == self.stamp:
            return
        self.stamp = current
        try:
            entries, problems = relays.load_entries(self.data_dir)
        except FileNotFoundError:
            entries, problems = [], []
        except (OSError, ValueError) as e:
            log.error("relays: %s: %s (the relays are unchanged)", relays.LIST_FILE, e)
            return
        for problem in problems:
            log.warning("relays: %s: %s (left out)", relays.LIST_FILE, problem)
        secrets = relays.load_secrets(entries, self.data_dir)
        changed = {rid for rid in set(self.secrets) | set(secrets)
                   if self.secrets.get(rid) != secrets.get(rid)}
        known = set(self.secrets)
        self.entries = {e["id"]: e for e in entries if e["id"] in secrets}
        self.secrets = secrets
        for rid in changed:  # revoked, or a new secret: its link must prove it again
            self.links.pop(rid, None)
            self.resolved.pop(rid, None)
            if rid in known:
                self.health.pop(rid, None)  # and the probes start over
        for rid in [r for r in self.health if r not in self.entries]:
            del self.health[rid]
        for rid in self.entries:
            if rid not in self.health:
                saved = self.saved_health.pop(rid, None)
                saved = saved if isinstance(saved, dict) else {}
                since = saved.get("since")
                self.health[rid] = Health(
                    self.clock() + PROBE_GRACE, listed=saved.get("listed") is True,
                    dropped=saved.get("dropped") is True,
                    since=since if isinstance(since, (int, float)) else None)
        self.publish_listed()
        if self.entries:
            log.info("relays: %d listed (%s)", len(self.entries), ", ".join(sorted(self.entries)))
        self.update_up()

    @property
    def enabled(self):
        return bool(self.secrets)

    # --- links ---

    def on_link(self, body, addr):
        try:
            got = relay_node.parse_link(body)
        except (ValueError, UnicodeDecodeError, struct.error):
            return
        rid, wall, nonce, signed, tag = got.relay_id, got.wall, got.nonce, got.signed, got.mac
        secret = self.secrets.get(rid)
        if secret is None or relays.mac(secret, relays.LINK_TAG, signed) != tag:
            self.counters["links_refused"] += 1
            self.refuse(addr, nonce, "not listed" if secret is None else "wrong secret")
            return
        if abs(self.wall() - wall) > LINK_SKEW:
            log.warning("relays: %s's clock is %d s off; its link is ignored", rid, wall - self.wall())
            return
        link = self.links.get(rid)
        if link is None:
            link = self.links[rid] = Link()
        if nonce in link.nonces:
            return  # a replay
        link.nonces = (link.nonces + [nonce])[-64:]
        was_up = self.up(rid)
        link.addr, link.seen = addr, self.clock()
        link.slots, link.sessions, link.bytes = got.slots, got.sessions, got.bytes
        link.max_slots, link.bandwidth = got.max_slots, got.bandwidth
        self.server.send(addr, relay_node.RELAY_LINKED, relay_node.linked_body(secret, nonce, self.wall()))
        self.resolve(rid)
        if not was_up and self.up(rid):
            log.info("relays: %s is linked", rid)
            health = self.health.get(rid)
            if (health is not None and health.nonce is None and
                    (not health.probed or (not health.listed and self.clock() >= health.next_probe))):
                self.start_probe(rid)  # a relay just linked: probed at once
            self.update_up()

    def refuse(self, addr, nonce, why):
        if self.server.take_error(addr):
            text = ("relay refused: " + why).encode()
            self.server.send(addr, relay_node.ERROR,
                             struct.pack(">BH", relay_node.ERROR_REFUSED, len(text)) + text + nonce)

    def resolve(self, rid):
        """Finds the address of the operator's entry (the one players are sent to): at once for
        an IPv4 address, else in a thread, again every RESOLVE_EVERY s."""
        found, when = self.resolved.get(rid, (None, None))
        now = self.clock()
        if when is not None and now - when < RESOLVE_EVERY:
            return
        host, port = relay_node.split_host_port(self.entries[rid]["address"])
        ip = literal_ipv4(host)
        if ip is not None:
            self.resolved[rid] = ((ip, port), now)
            return
        self.resolved[rid] = (found, now)  # one lookup at a time

        def lookup():
            try:
                return socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)[0][4][:2]
            except OSError as e:
                log.warning("relays: cannot resolve %s for %s: %s", host, rid, e)
                return None

        def done(future):
            result = future.result()
            if result is not None and rid in self.entries:
                was_up = self.up(rid)
                self.resolved[rid] = (result, self.clock())
                if not was_up and self.up(rid):
                    log.info("relays: %s is linked", rid)
                    self.update_up()

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            result = lookup()
            if result is not None:
                self.resolved[rid] = (result, now)
            return
        loop.run_in_executor(None, lookup).add_done_callback(done)

    def address(self, rid):
        return self.resolved.get(rid, (None, None))[0]

    def up(self, rid):
        """Whether the relay's link is up (and its address known)."""
        link = self.links.get(rid)
        return (rid in self.secrets and link is not None and link.seen is not None and
                self.clock() - link.seen <= LINK_TIMEOUT and self.address(rid) is not None)

    def listed(self, rid):
        health = self.health.get(rid)
        return health is not None and health.listed

    def usable(self, rid):
        """Linked, and listed by the probes: the relays players are sent to."""
        return self.up(rid) and self.listed(rid)

    def up_relays(self):
        return sorted(rid for rid in self.entries if self.usable(rid))

    # --- probes ---

    def start_probe(self, rid):
        health = self.health[rid]
        if not self.up(rid):
            self.probe_result(rid, False, "its link is down")
            return
        health.nonce, health.started, health.tries = os.urandom(16), self.clock(), 0
        self.send_probe(rid)

    def send_probe(self, rid):
        health = self.health[rid]
        health.tries += 1
        health.sent = self.clock()
        self.server.send(self.address(rid), relay_node.RELAY_PING, health.nonce)

    def on_pong(self, body, addr):
        """A RELAY_PONG: the echo of a probe, from the address the players are sent to."""
        for rid, health in self.health.items():
            if health.nonce is not None and body == health.nonce and addr == self.address(rid):
                health.probe_ms = int((self.clock() - health.sent) * 1000)
                self.probe_result(rid, True)
                return

    def probe_result(self, rid, ok, why=""):
        health = self.health[rid]
        health.nonce, health.probed = None, True
        now = self.clock()
        changed = False
        if ok:
            health.fails, health.passes = 0, health.passes + 1
            if not health.listed and health.passes >= (RETURN_PASSES if health.dropped else 1):
                health.listed, health.dropped, health.since = True, False, self.wall()
                log.info("relays: %s passes its probes (%d ms): listed, on probation for %d "
                         "min", rid, health.probe_ms, self.probation_time // 60)
                changed = True
        else:
            health.passes, health.fails = 0, health.fails + 1
            log.warning("relays: %s failed a probe (%s), %d in a row", rid, why, health.fails)
            if health.listed and health.fails >= DROP_FAILS:
                health.listed, health.dropped, health.since = False, True, None
                log.warning("relays: %s failed %d probes in a row: it drops out of the list "
                            "until it passes %d", rid, health.fails, RETURN_PASSES)
                changed = True
        health.next_probe = now + (PROBE_EVERY if health.listed and not health.fails
                                   else PROBE_RETRY)
        if changed:
            self.save_health()
            self.publish_listed()
            self.update_up()

    def probe_tick(self):
        now = self.clock()
        for rid in sorted(self.health):
            health = self.health[rid]
            if health.nonce is not None:
                if now - health.started >= PROBE_TIMEOUT:
                    self.probe_result(rid, False, "no echo from %s" % self.entries[rid]["address"])
                elif health.tries < PROBE_TRIES:
                    self.send_probe(rid)
            elif now >= health.next_probe:
                self.start_probe(rid)

    def on_probation(self, rid):
        health = self.health.get(rid)
        return self.probation_time > 0 and (health is None or health.since is None or
                self.wall() - health.since < self.probation_time)

    def capacity(self, rid):
        """The players the relay takes, roughly: its slots, and the bandwidth its operator
        offers at the rate cap a player has."""
        link = self.links.get(rid)
        if link is None:
            return 0
        slots = link.max_slots or relay_node.MAX_SLOTS
        if link.bandwidth:
            slots = min(slots, link.bandwidth * 1024 // max(1, self.rate()))
        return slots

    def players_on(self, rid):
        return sum(len(s.members) for s in self.sessions.values() if s.relay == rid)

    def publish_listed(self):
        relays.set_listed(sorted(rid for rid in self.entries if self.listed(rid))
                          if self.data_dir is not None else None)

    def save_health(self):
        if self.data_dir is None:
            return
        data = {"relays": {rid: {"listed": h.listed, "dropped": h.dropped, "since": h.since}
                           for rid, h in sorted(self.health.items())}}
        path = os.path.join(self.data_dir, HEALTH_FILE)
        try:
            with open(path + ".tmp", "w") as f:
                json.dump(data, f, indent=1)
            durable.replace(path + ".tmp", path)
        except OSError as e:
            log.warning("relays: cannot save %s: %s", HEALTH_FILE, e)

    def update_up(self):
        """A new probe list when the up relays changed; sessions on a relay that went down are
        dissolved."""
        ups = self.up_relays()
        if ups != self.probe_list:
            self.probe_list = ups
            self.version = self.version % 0xFFFF + 1
            for peer in self.server.peers.values():
                self.probe(peer)
        for session in [s for s in self.sessions.values() if s.relay not in ups]:
            log.info("relays: %s is down; its session %016X is dissolved", session.relay, session.id)
            self.dissolve(session)

    # --- games ---

    @staticmethod
    def can_use(peer):
        return bool(peer.features & FEATURE_RELAYS)

    def probe(self, peer):
        if not self.can_use(peer) or self.probed.get(peer.id) == self.version:
            return
        if not self.probe_list and peer.id not in self.probed:
            self.probed[peer.id] = self.version  # nothing to ping, and it never had a list
            return
        self.probed[peer.id] = self.version
        body = struct.pack(">BHB", OP_PROBE, self.version, len(self.probe_list))
        for index, rid in enumerate(self.probe_list):
            ip, port = self.address(rid)
            body += struct.pack(">BIH", index, int(ipaddress.IPv4Address(ip)), port)
        self.server.send(peer.addr, relay_node.RELAY_INFO, body)

    def hello(self, peer):
        """After each WELCOME: the probe list once per version."""
        if self.enabled:
            self.probe(peer)

    def on_info(self, peer, body):
        if body[0] != OP_PINGS or len(body) < 4:
            return
        version, count = struct.unpack_from(">HB", body, 1)
        if version != self.version:
            return
        pings = {}
        for i in range(min(count, len(self.probe_list))):
            index, ms = struct.unpack_from(">BH", body, 4 + 3 * i)
            if index < len(self.probe_list):
                pings[self.probe_list[index]] = ms
        self.pings[peer.id] = pings

    def ping(self, peer, rid):
        return self.pings.get(peer.id, {}).get(rid)

    def choose(self, a, b):
        """The up relay with the lowest worst ping of the pair (one either could not reach is
        never picked), then the fewest sessions, that has room for two more players; a relay on
        probation is taken for only PROBATION_SHARE of the pairs, and PROBATION_SESSIONS
        sessions. None when there is none."""
        candidates = []
        for rid in self.up_relays():
            pa, pb = self.ping(a, rid), self.ping(b, rid)
            if NO_ANSWER in (pa, pb):
                continue
            worst = max(UNKNOWN_PING if pa is None else pa, UNKNOWN_PING if pb is None else pb)
            load = sum(1 for s in self.sessions.values() if s.relay == rid)
            candidates.append((worst, load, rid))
        for _, load, rid in sorted(candidates):
            if self.players_on(rid) + 2 > self.capacity(rid):
                continue
            if self.on_probation(rid) and (load >= PROBATION_SESSIONS or
                                           self.random() >= PROBATION_SHARE):
                continue
            return rid
        return None

    def note_relayed(self, src, dst):
        """The server relayed a datagram from src to dst (server.on_relay)."""
        if not self.enabled or not self.can_use(src) or not self.can_use(dst):
            return
        key = (min(src.id, dst.id), max(src.id, dst.id))
        now = self.clock()
        pair = self.pairs.get(key)
        if pair is None or now - pair[1] > PAIR_GAP:
            self.pairs[key] = [now, now, now + self.offer_after]
            return
        pair[1] = now
        if now >= pair[2]:
            pair[2] = now + REOFFER
            self.assign(src, dst)

    def session_of(self, peer):
        sid = self.of_peer.get(peer.id)
        return self.sessions.get(sid) if sid is not None else None

    def new_session_id(self, a, b):
        """The game session the two share (their presence's XSESSION id), else a random id."""
        shared = a.session[:8]
        if shared == b.session[:8] and any(shared):
            sid = int.from_bytes(shared, "big")
            if sid not in self.sessions:
                return sid
        while True:
            sid = random.getrandbits(64)
            if sid and sid not in self.sessions:
                return sid

    def assign(self, a, b):
        sa, sb = self.session_of(a), self.session_of(b)
        if sa is not None and sa is sb:
            target = sa  # offered already: its games may have missed the offer
        else:
            target = max((s for s in (sa, sb) if s is not None), key=lambda s: len(s.members),
                         default=None)
            if target is not None and not self.up(target.relay):
                target = None
            if target is None:
                rid = self.choose(a, b)
                if rid is None:
                    return
                target = RelaySession(self.new_session_id(a, b), rid)
                self.sessions[target.id] = target
                self.health[rid].sessions_relayed += 1
                log.info("relays: #%d %r and #%d %r relayed through %s (session %016X)", a.id,
                         a.name, b.id, b.name, rid, target.id)
            for peer in (a, b):
                old = self.session_of(peer)
                if old is not target:
                    if old is not None:
                        self.leave(peer.id, old)
                    target.members.add(peer.id)
                    self.of_peer[peer.id] = target.id
        for pid in sorted(target.members):
            peer = self.server.peers.get(pid)
            if peer is not None:
                self.offer(peer, target)

    def offer(self, peer, session):
        secret = self.secrets[session.relay]
        ip, port = self.address(session.relay)
        ticket = relays.make_ticket(secret, session.relay, session.id, peer.xuid, peer.id,
                                    self.wall() + TICKET_LIFE, self.rate())
        others = sorted(session.members - {peer.id})[:MAX_OFFER_PEERS]
        body = (struct.pack(">BIHB", OP_OFFER, int(ipaddress.IPv4Address(ip)), port, len(ticket)) +
                ticket + struct.pack(">B", len(others)) +
                b"".join(struct.pack(">I", pid) for pid in others))
        session.issued[peer.id] = self.clock()
        self.counters["offers"] += 1
        self.server.send(peer.addr, relay_node.RELAY_INFO, body)

    def withdraw(self, peer):
        self.counters["withdrawals"] += 1
        self.server.send(peer.addr, relay_node.RELAY_INFO, struct.pack(">BIHBB", OP_OFFER, 0, 0, 0, 0))

    def leave(self, pid, session, tell=True):
        """pid leaves the session; the others get their new offer (or a withdrawal when one is
        left)."""
        session.members.discard(pid)
        session.issued.pop(pid, None)
        if self.of_peer.get(pid) == session.id:
            del self.of_peer[pid]
        if len(session.members) <= 1:
            self.dissolve(session)
        elif tell:
            for other in sorted(session.members):
                peer = self.server.peers.get(other)
                if peer is not None:
                    self.offer(peer, session)

    def dissolve(self, session):
        self.sessions.pop(session.id, None)
        for pid in sorted(session.members):
            if self.of_peer.get(pid) == session.id:
                del self.of_peer[pid]
            peer = self.server.peers.get(pid)
            if peer is not None:
                self.withdraw(peer)
        session.members.clear()

    def drop(self, peer):
        """server.drop: the player left."""
        self.pings.pop(peer.id, None)
        self.probed.pop(peer.id, None)
        for key in [k for k in self.pairs if peer.id in k]:
            del self.pairs[key]
        session = self.session_of(peer)
        if session is not None:
            self.leave(peer.id, session)

    def tick(self):
        """Every few seconds (server.expire): the list again, the relays' state, tickets renewed
        and pairs forgotten."""
        self.reload()
        if not self.enabled and not self.sessions:
            return
        self.probe_tick()
        self.update_up()
        now = self.clock()
        for session in list(self.sessions.values()):
            for pid in [p for p in session.members if p not in self.server.peers]:
                self.leave(pid, session, tell=False)
            if session.id not in self.sessions:
                continue
            for pid in sorted(session.members):
                if now - session.issued.get(pid, now) >= TICKET_LIFE / 2:
                    self.offer(self.server.peers[pid], session)
        for key in [k for k, p in self.pairs.items() if now - p[1] > 60]:
            del self.pairs[key]

    def status(self):
        players = {}
        for session in self.sessions.values():
            players[session.relay] = players.get(session.relay, 0) + len(session.members)
        out = []
        for rid, e in sorted(self.entries.items()):
            health = self.health.get(rid) or Health(0.0)
            out.append({
                "id": rid, "name": e["name"], "region": e["region"], "up": self.usable(rid),
                "linked": self.up(rid), "listed": health.listed,
                "probation": health.listed and self.on_probation(rid),
                "probe_ms": health.probe_ms, "probe_fails": health.fails,
                "sessions": sum(1 for s in self.sessions.values() if s.relay == rid),
                "sessions_relayed": health.sessions_relayed,
                "players": players.get(rid, 0), "capacity": self.capacity(rid),
                "slots": getattr(self.links.get(rid), "slots", 0),
                "relayed_bytes": getattr(self.links.get(rid), "bytes", 0)})
        return {"relays": out, "relay_offers": self.counters["offers"]}
