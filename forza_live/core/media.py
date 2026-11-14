"""The public media routes (/community/img/*, /community/video/*): how their files are sent and
what one address may take of them (#63).

Sending. A photo is up to 16 MiB and a video up to 32 MiB. FileBody is such a file, or a byte
range of one, as an answer's body: the HTTP handler (server.send) reads and writes it PIECE bytes
at a time and waits for the reader to take each piece, so no file is in memory whole and a
reader that takes nothing for server.DRAIN_SECONDS is dropped.

Budgets. Each route has one budget for the whole server (community.IMAGES_PER_MINUTE,
videos.MEDIA_PER_MINUTE). Alone it let one address use it up for everyone, so each address has
its own besides, for the two routes together (Budget):

- REQUESTS: so many at once, then so many a second. An address over it gets 429 with
  Retry-After before its request is looked at, and that request does not count in the server's
  budget.
- BYTES: so many at once, then so many a second, counted as they are sent: an answer is charged
  whole when it starts and what was not sent (the reader left, or was dropped) is given back. An
  address with nothing left gets 429; one with anything left gets its answer, so the bucket can
  go below nothing by one answer at most.

An address is what the server already counts connections and signature checks under
(auth.Client.address): the peer's, an IPv6 one by its /64 (limits.address_key), and behind the
tunnel the one its PROXY header names. No budget of its own (the server's still applies) for:

- a private, link-local or loopback address (limits.local_address) that is not the tunnel's
  agent: the home network and this machine, where the website's own web server and builder
  fetch the feeds' files from one address for every visitor;
- the addresses of --website-address (FORZA_WEBSITE_ADDRESSES), for a web server or builder
  that reaches the server from a public address or through the tunnel;
- the tunnel's connections that name no address (no --proxy-protocol-tcp): they cannot be told
  apart, as for every other per-address limit.

At most MAX_ADDRESSES are remembered; one whose buckets are full again is forgotten (it has
nothing to remember), and while the table is full the new ones share one budget (OTHERS), so
making up addresses buys no budget.
"""

import ipaddress
import math
import os
import time

from forza_live.core import auth
from forza_live.core import limits

PIECE = 256 << 10  # bytes of a file read and written at a time (the game's own upload chunk)
# One address, both routes together: (per second, at once).
REQUESTS = (2.0, 300)
BYTES = (512 << 10, 2 << 30)
MAX_ADDRESSES = 20000
PRUNE_SECONDS = 10.0  # the table is looked through for full buckets at most this often
OTHERS = "others"     # the budget of the addresses over MAX_ADDRESSES
WEBSITE = []          # --website-address: ip_network objects


class FileBody:
    """The bytes [start, start + length) of a file as an answer's body. len() is its
    Content-Length. The file is opened for each piece and held open between none: a file
    deleted meanwhile ends the answer there (OSError), on Windows too, where an open file
    cannot be deleted. `source`: the address charged for it (charge), or None."""
    __slots__ = ("path", "start", "length", "source")

    def __init__(self, path, start, length):
        self.path, self.start, self.length, self.source = path, start, length, None

    def __len__(self):
        return self.length

    def pieces(self, size=None):
        """The bytes, at most `size` (PIECE) at a time. OSError when the file is gone or ends
        before the answer does."""
        size, done = size or PIECE, 0
        while done < self.length:
            with open(self.path, "rb") as src:
                src.seek(self.start + done)
                piece = src.read(min(size, self.length - done))
            if not piece:
                raise OSError("%s ends before its answer" % os.path.basename(self.path))
            done += len(piece)
            yield piece

    def read(self):
        """All of it at once (a test, a small file)."""
        return b"".join(self.pieces())


class Budget:
    """What each address may take of the media routes: a bucket of requests and one of bytes."""

    def __init__(self):
        self.buckets = {}  # address -> [requests left, bytes left, when they were last filled]
        self.pruned = float("-inf")

    def reset(self):
        self.buckets.clear()
        self.pruned = float("-inf")

    def bucket(self, key, now):
        found = self.buckets.get(key)
        if found is None:
            if len(self.buckets) >= MAX_ADDRESSES and now - self.pruned >= PRUNE_SECONDS:
                self.pruned = now
                for old in [k for k, b in self.buckets.items() if self.full(b, now)]:
                    del self.buckets[old]
            if len(self.buckets) >= MAX_ADDRESSES:
                key = OTHERS
                found = self.buckets.get(key)
            if found is None:
                found = self.buckets[key] = [float(REQUESTS[1]), float(BYTES[1]), now]
        elapsed = max(0.0, now - found[2])
        found[0] = min(float(REQUESTS[1]), found[0] + elapsed * REQUESTS[0])
        found[1] = min(float(BYTES[1]), found[1] + elapsed * BYTES[0])
        found[2] = now
        return found

    @staticmethod
    def full(bucket, now):
        elapsed = max(0.0, now - bucket[2])
        return (bucket[0] + elapsed * REQUESTS[0] >= REQUESTS[1]
                and bucket[1] + elapsed * BYTES[0] >= BYTES[1])

    def admit(self, key, now=None):
        """A request from `key`: 0 when it is taken (one request spent), else the whole seconds
        until one would be (the Retry-After)."""
        now = time.monotonic() if now is None else now
        b = self.bucket(key, now)
        wait = 0.0
        if b[0] < 1:
            wait = (1 - b[0]) / REQUESTS[0] if REQUESTS[0] > 0 else 3600.0
        if b[1] <= 0:
            wait = max(wait, (1 - b[1]) / BYTES[0] if BYTES[0] > 0 else 3600.0)
        if wait:
            return min(3600, max(1, math.ceil(wait)))
        b[0] -= 1
        return 0

    def charge(self, key, nbytes, now=None):
        """nbytes are about to be sent to `key`."""
        self.bucket(key, time.monotonic() if now is None else now)[1] -= nbytes

    def refund(self, key, nbytes, now=None):
        """nbytes of them were not sent after all."""
        b = self.bucket(key, time.monotonic() if now is None else now)
        b[1] = min(float(BYTES[1]), b[1] + nbytes)


BUDGET = Budget()


def configure(*specs):
    """specs: --website-address values and FORZA_WEBSITE_ADDRESSES (IPs or CIDRs, each possibly a
    comma or space separated list). Forgets what the addresses have used."""
    WEBSITE[:] = limits.parse_proxies(*specs)
    BUDGET.reset()


def listed(address):
    try:
        ip = ipaddress.ip_address(address.split("%")[0].split("/")[0])
    except ValueError:
        return False
    return any(ip in net for net in WEBSITE)


def source(peer):
    """What a media request is counted under: the client's address as the server knows it
    (auth.Client.address for the HTTP connection being handled, else the peer's own), or None
    for a caller without a budget of its own (above)."""
    who = auth.client()
    if who is not None:
        address = who.address
    else:
        ip = peer[0] if peer else ""
        address = None if not ip or limits.is_trusted_proxy(ip) else limits.address_key(ip)
    if not address or limits.local_address(address) or listed(address):
        return None
    return address


def admit(key):
    """Budget.admit for source()'s key: 0 for an address that is taken, or has no budget."""
    return BUDGET.admit(key) if key is not None else 0


def charge(key, answer):
    """The answer (status, content type, body, ...) of a media route, its body charged to `key`:
    only a 200's or a 206's is sent. The answer."""
    body = answer[2]
    if key is not None and answer[0] in (200, 206) and len(body):
        BUDGET.charge(key, len(body))
        if isinstance(body, FileBody):
            body.source = key
    return answer


def unsent(body, sent):
    """The HTTP handler sent `sent` bytes of a FileBody and no more: the rest goes back to the
    address charged for it."""
    if body.source is not None and sent < len(body):
        BUDGET.refund(body.source, len(body) - sent)
