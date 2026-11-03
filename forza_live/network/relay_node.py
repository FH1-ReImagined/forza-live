"""A volunteer relay (FH1 #516): forza_live_server.py --relay-only.

A relay forwards game traffic between players who cannot reach each other directly, closer to
them than the main server. It holds no database and no admin token, and serves nothing but UDP:

- It links out to the main server (--relay-of host:port) with its id (--relay-id) and the
  secret the main server's operator made for it (--relay-secret-file; core/relays.py): every
  LINK_INTERVAL s a RELAY_LINK with the time, a fresh nonce and its counters, and an HMAC with
  the secret. The main server answers RELAY_LINKED (the nonce, its HMAC), or, for a relay it no
  longer lists, an ERROR (code ERROR_REFUSED) that names the nonce: the relay then drops every
  slot and takes no ticket until a link is accepted again. A main server that does not answer for
  LINK_TIMEOUT s is retried; slots keep working meanwhile, until their tickets expire.
- A player binds a slot with a ticket the main server signed for this relay (RELAY_BIND; the
  relay answers RELAY_BOUND with the session, its peer id and the seconds the ticket has left).
  The slot is the player's address within the ticket's session.
- A slot's RELAY datagrams (the main server's own RELAY body: destination peer id, ports,
  payload) go, as FORWARD, only to another slot of the same session, within the ticket's rate
  (bytes a second, bursts of 4 s), and never to a private, loopback, link-local, multicast or
  reserved address (as coturn's CVE-2020-26262: a relay is no way into its own network). An
  address of that kind cannot bind either, unless --relay-allow-private (a test on one LAN).
- A slot not used (bound or sent from) for SLOT_IDLE s, or whose ticket expired, is dropped.
- RELAY_PING is answered with RELAY_PONG and the same bytes (at most 32), so games can measure
  their ping to the relay.
"""

import asyncio
import ipaddress
import logging
import os
import socket
import struct
import time

from forza_live.core import relays

log = logging.getLogger("forza-live")

MAGIC = b"RLV1"
ERROR, RELAY, FORWARD = 3, 5, 6  # as in server.py
# The relays' messages (the main server knows them too, network/relay_broker.py):
RELAY_LINK = 25    # relay -> main server: the link (id, time, nonce, counters, HMAC)
RELAY_LINKED = 26  # main server -> relay: accepted (nonce, time, HMAC)
RELAY_INFO = 27    # main server <-> game: the relays to ping, the pings, offers (relay_broker.py)
RELAY_BIND = 28    # game -> relay: a ticket
RELAY_BOUND = 29   # relay -> game: session u64, peer id u32, seconds left u32
RELAY_PING = 30    # game -> relay: up to 32 bytes
RELAY_PONG = 31    # relay -> game: the same bytes
ERROR_NOT_BOUND = 1  # a RELAY from an address with no slot: bind again
ERROR_REFUSED = 6    # main server -> relay: this relay is not (any more) listed
ERROR_TICKET = 7     # relay -> game: the ticket was refused (the message says why)

LINK_VERSION = 1
LINK_INTERVAL = 10.0  # seconds between RELAY_LINKs while linked
LINK_RETRY = 5.0      # while not linked
LINK_TIMEOUT = 30.0   # without a RELAY_LINKED: the link is down
SLOT_IDLE = 60.0
MAX_SLOTS = 4096
MAX_PAYLOAD = 1500
MAX_PING = 32
PACKETS_PER_IP = (2500, 5000)
BINDS_PER_IP = (5, 20)
PINGS_PER_IP = (5, 20)
ERRORS_PER_IP = (2, 10)

# Never relayed to (nor bound from): what a relay on the internet has no business sending to.
REFUSED_NETS = [ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16",
    "172.16.0.0/12", "192.0.0.0/24", "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4",
    "240.0.0.0/4", "::/128", "::1/128", "fc00::/7", "fe80::/10", "ff00::/8", "::ffff:0:0/96")]


def refused_address(ip):
    """Whether a relay must not send to (or bind) `ip`: private, loopback, link-local, CGNAT,
    multicast, reserved or unspecified (and anything that is not an IP address)."""
    try:
        addr = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return True
    return any(addr in net for net in REFUSED_NETS if net.version == addr.version)


def take(buckets, key, limit, now, cost=1.0):
    rate, burst = limit
    tokens, last = buckets.get(key, (burst, now))
    tokens = min(burst, tokens + (now - last) * rate)
    ok = tokens >= cost
    buckets[key] = (tokens - cost if ok else tokens, now)
    return ok


def link_body(relay_id, secret, wall, nonce, slots, sessions, nbytes):
    rid = relay_id.encode()
    body = (struct.pack(">BB", LINK_VERSION, len(rid)) + rid +
            struct.pack(">Q16sIIQ", int(wall), nonce, slots, sessions, nbytes))
    return body + relays.mac(secret, relays.LINK_TAG, body)


def parse_link(body):
    """(relay id, wall, nonce, slots, sessions, bytes, signed part, mac) of a RELAY_LINK, or
    ValueError."""
    if len(body) < 2 or body[0] != LINK_VERSION:
        raise ValueError("link version")
    n = body[1]
    end = 2 + n + struct.calcsize(">Q16sIIQ")
    if len(body) != end + relays.MAC_SIZE:
        raise ValueError("link size")
    relay_id = body[2:2 + n].decode("ascii")
    wall, nonce, slots, sessions, nbytes = struct.unpack_from(">Q16sIIQ", body, 2 + n)
    return relay_id, wall, nonce, slots, sessions, nbytes, body[:end], body[end:]


def linked_body(secret, nonce, wall):
    body = struct.pack(">16sQ", nonce, int(wall))
    return body + relays.mac(secret, relays.LINKED_TAG, body)


class Slot:
    __slots__ = ("session", "peer_id", "xuid", "addr", "expiry", "rate", "budget", "budget_time",
                 "used", "bytes")

    def __init__(self, ticket, addr, now):
        self.session, self.peer_id, self.xuid = ticket.session, ticket.peer_id, ticket.xuid
        self.addr, self.expiry, self.rate = addr, ticket.expiry, ticket.rate
        self.budget, self.budget_time = 4.0 * ticket.rate, now
        self.used = now
        self.bytes = 0


class RelayNode(asyncio.DatagramProtocol):
    def __init__(self, relay_id, secret, main_addr, allow_private=False, clock=time.monotonic,
                 wall=time.time, max_slots=MAX_SLOTS):
        self.relay_id, self.secret = relay_id, secret
        self.main_addr = main_addr  # (ip, port), resolved
        self.allow_private = allow_private
        self.clock, self.wall = clock, wall
        self.max_slots = max_slots
        self.transport = None
        self.slots = {}    # (session, peer id) -> Slot
        self.by_addr = {}  # (ip, port) -> Slot
        self.buckets, self.bind_buckets, self.ping_buckets, self.error_buckets = {}, {}, {}, {}
        self.nonces = []   # of the last RELAY_LINKs sent (answers may arrive late)
        self.linked = False
        self.revoked = False
        self.last_linked = None
        self.next_link = 0.0
        self.counters = {"forwarded_packets": 0, "forwarded_bytes": 0, "refused_session": 0,
                         "refused_private": 0, "refused_tickets": 0, "over_rate": 0,
                         "expired_slots": 0}

    # --- the socket ---

    def connection_made(self, transport):
        self.transport = transport

    def send(self, addr, kind, body=b""):
        self.transport.sendto(MAGIC + bytes([kind]) + body, addr)

    def error(self, addr, code, message, now):
        if take(self.error_buckets, addr[0], ERRORS_PER_IP, now):
            text = message.encode()
            self.send(addr, ERROR, struct.pack(">BH", code, len(text)) + text)

    def refused(self, ip):
        return not self.allow_private and refused_address(ip)

    def datagram_received(self, data, addr):
        if len(data) < 5 or data[:4] != MAGIC or len(data) > 2048:
            return
        now = self.clock()
        kind, body = data[4], data[5:]
        try:
            if addr == self.main_addr:
                self.on_main(kind, body)
                return
            if not take(self.buckets, addr[0], PACKETS_PER_IP, now):
                self.counters["over_rate"] += 1
                return
            if kind == RELAY:
                self.on_relay(addr, body, now)
            elif kind == RELAY_BIND:
                self.on_bind(addr, body, now)
            elif kind == RELAY_PING and len(body) <= MAX_PING:
                if take(self.ping_buckets, addr[0], PINGS_PER_IP, now):
                    self.send(addr, RELAY_PONG, body)
        except Exception as e:  # a malformed datagram must never end the relay
            log.debug("bad packet from %s: %r", addr, e)

    # --- the link to the main server ---

    def send_link(self):
        nonce = os.urandom(16)
        self.nonces = (self.nonces + [nonce])[-4:]
        sessions = len({s.session for s in self.slots.values()})
        self.send(self.main_addr, RELAY_LINK,
                  link_body(self.relay_id, self.secret, self.wall(), nonce, len(self.slots),
                            sessions, self.counters["forwarded_bytes"]))

    def on_main(self, kind, body):
        if kind == RELAY_LINKED and len(body) == 24 + relays.MAC_SIZE:
            nonce = body[:16]
            if nonce not in self.nonces or not relays.mac(
                    self.secret, relays.LINKED_TAG, body[:24]) == body[24:]:
                return
            if not self.linked:
                log.info("relay %s: linked to the main server %s:%d", self.relay_id,
                         *self.main_addr)
            if self.revoked:
                log.info("relay %s: the main server accepts this relay again", self.relay_id)
            self.linked, self.revoked, self.last_linked = True, False, self.clock()
        elif kind == ERROR and len(body) >= 3 and body[0] == ERROR_REFUSED:
            n = struct.unpack_from(">H", body, 1)[0]
            nonce = body[3 + n:3 + n + 16]
            if nonce not in self.nonces:
                return  # not an answer to our link (a forged source address)
            if not self.revoked:
                log.warning("relay %s: the main server refused this relay (%s): its secret was "
                            "revoked or it is not listed; dropping %d slot(s)", self.relay_id,
                            body[3:3 + n].decode("utf-8", "replace"), len(self.slots))
            self.revoked, self.linked = True, False
            self.slots.clear()
            self.by_addr.clear()

    # --- players ---

    def on_bind(self, addr, body, now):
        if not take(self.bind_buckets, addr[0], BINDS_PER_IP, now):
            return
        if self.revoked:
            self.error(addr, ERROR_TICKET, "relay not accepted by the main server", now)
            return
        if self.refused(addr[0]):
            self.counters["refused_private"] += 1
            self.error(addr, ERROR_TICKET, "address refused", now)
            return
        try:
            ticket = relays.check_ticket(self.secret, self.relay_id, body, self.wall())
        except ValueError as e:
            self.counters["refused_tickets"] += 1
            self.error(addr, ERROR_TICKET, str(e), now)
            return
        key = (ticket.session, ticket.peer_id)
        old = self.by_addr.get(addr)
        if old is not None and (old.session, old.peer_id) != key:
            self.remove(old)  # one address, one slot: the player moved to another session
        slot = self.slots.get(key)
        if slot is None:
            if len(self.slots) >= self.max_slots:
                self.error(addr, ERROR_TICKET, "relay full", now)
                return
            slot = self.slots[key] = Slot(ticket, addr, now)
        else:
            if slot.addr != addr and self.by_addr.get(slot.addr) is slot:
                del self.by_addr[slot.addr]  # a new address of the same player (NAT rebinding)
            if ticket.expiry >= slot.expiry:
                slot.expiry, slot.rate, slot.xuid = ticket.expiry, ticket.rate, ticket.xuid
            slot.addr, slot.used = addr, now
        self.by_addr[addr] = slot
        left = max(0, int(slot.expiry - self.wall()))
        self.send(addr, RELAY_BOUND, struct.pack(">QII", slot.session, slot.peer_id, left))

    def remove(self, slot):
        self.slots.pop((slot.session, slot.peer_id), None)
        if self.by_addr.get(slot.addr) is slot:
            del self.by_addr[slot.addr]

    def spend(self, slot, nbytes, now):
        slot.budget = min(4.0 * slot.rate, slot.budget + (now - slot.budget_time) * slot.rate)
        slot.budget_time = now
        if slot.budget < nbytes:
            return False
        slot.budget -= nbytes
        return True

    def on_relay(self, addr, body, now):
        src = self.by_addr.get(addr)
        if src is None or self.wall() > src.expiry:
            if src is not None:
                self.remove(src)
                self.counters["expired_slots"] += 1
            self.error(addr, ERROR_NOT_BOUND, "not bound", now)
            return
        if len(body) < 8 or len(body) - 8 > MAX_PAYLOAD:
            return
        src.used = now
        (dst_id,) = struct.unpack_from(">I", body, 0)
        dst = self.slots.get((src.session, dst_id))
        if dst is None or self.wall() > dst.expiry:
            self.counters["refused_session"] += 1  # not in this ticket's session (or gone)
            return
        if self.refused(dst.addr[0]):
            self.counters["refused_private"] += 1
            return
        if not self.spend(src, len(body), now):
            self.counters["over_rate"] += 1
            return
        src.bytes += len(body) - 8
        self.counters["forwarded_packets"] += 1
        self.counters["forwarded_bytes"] += len(body) - 8
        self.send(dst.addr, FORWARD, struct.pack(">IIHIH", src.peer_id, 0, 0, 0, 0) + body[4:])

    # --- every second ---

    def tick(self):
        now = self.clock()
        if self.linked and now - self.last_linked > LINK_TIMEOUT:
            self.linked = False
            log.warning("relay %s: no answer from the main server for %d s; retrying (the slots "
                        "keep working until their tickets expire)", self.relay_id, LINK_TIMEOUT)
        if now >= self.next_link:
            self.send_link()
            self.next_link = now + (LINK_INTERVAL if self.linked else LINK_RETRY)
        wall = self.wall()
        for slot in [s for s in self.slots.values()
                     if now - s.used > SLOT_IDLE or wall > s.expiry]:
            self.remove(slot)
            self.counters["expired_slots"] += 1
        for buckets in (self.buckets, self.bind_buckets, self.ping_buckets, self.error_buckets):
            if len(buckets) > 5000:
                for ip in [ip for ip, (_, last) in buckets.items() if now - last > 60]:
                    del buckets[ip]

    def status(self):
        return {"relay": self.relay_id, "linked": self.linked, "revoked": self.revoked,
                "slots": len(self.slots),
                "sessions": len({s.session for s in self.slots.values()}), **self.counters}


def split_host_port(text, default_port=21200):
    if text.startswith("["):
        host, _, rest = text[1:].partition("]")
        return host, int(rest[1:]) if rest.startswith(":") else default_port
    host, _, port = text.rpartition(":") if ":" in text else (text, "", "")
    return host, int(port) if port else default_port


def resolve(text):
    host, port = split_host_port(text)
    info = socket.getaddrinfo(host, port, socket.AF_INET, socket.SOCK_DGRAM)
    return info[0][4][:2]


async def run(args):
    """--relay-only: the relay until the process is stopped."""
    if not args.relay_of or not args.relay_secret_file or not args.relay_id:
        raise SystemExit("--relay-only needs --relay-of HOST:PORT, --relay-id ID and "
                         "--relay-secret-file FILE")
    try:
        secret = relays.read_secret(args.relay_secret_file)
    except (OSError, ValueError) as e:
        raise SystemExit(f"--relay-secret-file: {e}")
    loop = asyncio.get_running_loop()
    main_addr = await loop.run_in_executor(None, resolve, args.relay_of)
    transport, node = await loop.create_datagram_endpoint(
        lambda: RelayNode(args.relay_id, secret, main_addr, args.relay_allow_private),
        local_addr=(args.host, args.port))
    if args.relay_allow_private:
        log.warning("relay %s: --relay-allow-private: private addresses are relayed (a LAN "
                    "test only)", args.relay_id)
    log.info("relay %s on UDP %s:%d for the main server %s (%s:%d)", args.relay_id, args.host,
             args.port, args.relay_of, *main_addr)
    try:
        ticks = 0
        while True:
            node.tick()
            await asyncio.sleep(1)
            ticks += 1
            if not node.linked and ticks % 60 == 0:  # the main server's name may point elsewhere
                try:
                    found = await loop.run_in_executor(None, resolve, args.relay_of)
                    if found != node.main_addr:
                        log.info("relay %s: the main server is now %s:%d", args.relay_id, *found)
                        node.main_addr = found
                except OSError as e:
                    log.warning("relay %s: cannot resolve %s: %s", args.relay_id, args.relay_of, e)
            if ticks % 300 == 0:
                log.info("relay %s: %s", args.relay_id, node.status())
    finally:
        transport.close()
