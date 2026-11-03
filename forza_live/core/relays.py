"""Volunteer relays (FH1 #516): the operator's list, the per-relay secrets, the tickets the main
server signs for them and the signed GET /relays.json.

A relay is this same program started with --relay-only (network/relay_node.py) on a volunteer's
machine in another region. It holds no database and no admin token: it links out to the main
server with a secret only the two of them know, and forwards game traffic between players the
main server sent to it, with a ticket the main server signed with that secret.

The operator adds a relay with a command (docs/operating.md, "Volunteer relays"):

    python3 -m forza_live.core.relays --data-dir DIR add eu-1 --address your.host:21200 \\
        --name "Example relay" --region Europe

which writes the entry to DATA_DIR/relay_list.json,

    {"relays": [{"id": "eu-1", "name": "...", "address": "your.host:21200", "region": "...",
                 "contact": "..."}]}

and a new secret to DATA_DIR/relay_secrets/eu-1.txt (64 hex digits, mode 600), the file the
volunteer passes to --relay-secret-file. `remove eu-1` revokes it: the secret file is deleted,
the relay's link is refused from then on and the relay drops every slot it holds.

Tickets: {session, xuid, peer id, relay, expiry, rate} and an HMAC-SHA256 of them with the
relay's secret (the first 16 bytes), so only the main server makes them and only that relay takes
them. A ticket lets one player (its peer id, as the main server numbers players) bind one address
at the relay, inside one session (the players the main server put together there), up to `rate`
bytes a second, until `expiry` (unix time).

GET /relays.json is the list, signed as /servers.json is (server_list.py), with the same key and
its own tag, so the launcher checks both with the public key it ships:

    {"format": "forza-live-relays-1", "key": "<public key, 64 hex>",
     "list": "<the list as JSON text>", "signature": "<128 hex>"}

where the signature is over b"FORZA-RELAYS-V1\\n" + the list text, and the list text is
{"issued": unix time, "expires": unix time, "relays": [{"id", "name", "address", "region"}]}."""

import argparse
import hashlib
import hmac
import json
import logging
import os
import re
import struct
import sys
import time
from collections import namedtuple

from forza_live.core import ed25519, server_list

log = logging.getLogger("forza-live")

LIST_FILE = "relay_list.json"
SECRETS_DIR = "relay_secrets"
FORMAT = "forza-live-relays-1"
TAG = b"FORZA-RELAYS-V1\n"
RESIGN = 86400
EXPIRES = 14 * 86400
MAX_RELAYS = 64
LIMITS = {"name": 48, "address": 253 + 8, "region": 32, "contact": 120}
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Tickets: version, relay tag (the first 8 bytes of SHA-256 of the relay's id), session, XUID,
# peer id, expiry (unix time), rate (bytes per second), then the MAC.
TICKET_VERSION = 1
TICKET_HEAD = struct.Struct(">B8sQQIII")
MAC_SIZE = 16
TICKET_SIZE = TICKET_HEAD.size + MAC_SIZE
TICKET_TAG = b"FORZA-RELAY-TICKET-V1\n"
LINK_TAG = b"FORZA-RELAY-LINK-V1\n"
LINKED_TAG = b"FORZA-RELAY-LINKED-V1\n"

Ticket = namedtuple("Ticket", "session xuid peer_id expiry rate")

_data_dir = "forza_live_data"
_cache = {"stamp": None, "signed": 0.0, "body": None}


def configure(data_dir):
    global _data_dir
    _data_dir = data_dir
    _cache.update(stamp=None, signed=0.0, body=None)


def relay_tag(relay_id):
    return hashlib.sha256(relay_id.encode()).digest()[:8]


def mac(secret, tag, data):
    return hmac.new(secret, tag + data, hashlib.sha256).digest()[:MAC_SIZE]


def make_ticket(secret, relay_id, session, xuid, peer_id, expiry, rate):
    head = TICKET_HEAD.pack(TICKET_VERSION, relay_tag(relay_id), session, xuid, peer_id,
                            int(expiry), int(rate))
    return head + mac(secret, TICKET_TAG, head)


def check_ticket(secret, relay_id, data, now=None):
    """The Ticket in `data` when this relay's secret signed it for this relay and it has not
    expired, else ValueError saying why."""
    if len(data) != TICKET_SIZE:
        raise ValueError("not a ticket")
    head, tag = data[:TICKET_HEAD.size], data[TICKET_HEAD.size:]
    version, relay, session, xuid, peer_id, expiry, rate = TICKET_HEAD.unpack(head)
    if version != TICKET_VERSION:
        raise ValueError("ticket version %d" % version)
    if not hmac.compare_digest(tag, mac(secret, TICKET_TAG, head)):
        raise ValueError("forged ticket")
    if relay != relay_tag(relay_id):
        raise ValueError("ticket for another relay")
    if (time.time() if now is None else now) > expiry:
        raise ValueError("expired ticket")
    return Ticket(session, xuid, peer_id, expiry, rate)


def read_secret(path):
    """The 32-byte secret in a file of 64 hex digits, or ValueError / OSError."""
    with open(path, encoding="ascii") as f:
        secret = bytes.fromhex(f.read().strip())
    if len(secret) != 32:
        raise ValueError(f"{path}: not 64 hex digits")
    return secret


def secret_path(relay_id, data_dir=None):
    return os.path.join(data_dir or _data_dir, SECRETS_DIR, relay_id + ".txt")


def _text(entry, key, required=True):
    value = entry.get(key, "")
    if not isinstance(value, str):
        raise ValueError(f"{key}: not text")
    value = " ".join(value.split())
    if required and not value:
        raise ValueError(f"{key}: missing")
    if len(value) > LIMITS[key]:
        raise ValueError(f"{key}: longer than {LIMITS[key]} characters")
    if any(ord(c) < 32 for c in value):
        raise ValueError(f"{key}: control characters")
    return value


def clean_entry(entry):
    if not isinstance(entry, dict):
        raise ValueError("not an object")
    relay_id = entry.get("id")
    if not isinstance(relay_id, str) or not ID_RE.match(relay_id):
        raise ValueError("id: 1-32 of a-z, 0-9 and -, starting with a letter or digit")
    out = {"id": relay_id, "name": _text(entry, "name"),
           "address": _text(entry, "address").lower(),
           "region": _text(entry, "region", required=False),
           "contact": _text(entry, "contact", required=False)}
    if not server_list.valid_address(out["address"]):
        raise ValueError(f"address: {out['address']!r} is not host[:port]")
    return out


def read_file(data_dir=None):
    path = os.path.join(data_dir or _data_dir, LIST_FILE)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or not isinstance(data.get("relays", []), list):
        raise ValueError(f"{LIST_FILE}: not a JSON object with a relays list")
    return data


def load_entries(data_dir=None):
    """(entries, problems): the operator's relay_list.json read and checked. An entry with a
    problem, a second entry with the same id, and one without its secret file are left out (and
    named in problems). FileNotFoundError when there is no file."""
    data = read_file(data_dir)
    entries, problems, seen = [], [], set()
    for i, entry in enumerate(data.get("relays", [])):
        try:
            entry = clean_entry(entry)
        except ValueError as e:
            problems.append(f"relays[{i}]: {e}")
            continue
        if entry["id"] in seen:
            problems.append(f"relays[{i}]: {entry['id']} is already listed")
            continue
        if not os.path.exists(secret_path(entry["id"], data_dir)):
            problems.append(f"relays[{i}]: {entry['id']} has no secret (revoked)")
            continue
        if len(entries) >= MAX_RELAYS:
            problems.append(f"relays[{i}]: more than {MAX_RELAYS} relays")
            break
        seen.add(entry["id"])
        entries.append(entry)
    return entries, problems


def load_secrets(entries, data_dir=None):
    """{relay id: secret} of the listed relays whose secret file reads (damaged ones logged)."""
    secrets = {}
    for entry in entries:
        try:
            secrets[entry["id"]] = read_secret(secret_path(entry["id"], data_dir))
        except (OSError, ValueError) as e:
            log.error("relays: the secret of %s cannot be read (%s): it is not used", entry["id"], e)
    return secrets


def stamp(data_dir=None):
    """What changes when the list or a secret file changes (the main server reloads then)."""
    out = []
    base = data_dir or _data_dir
    for path in (os.path.join(base, LIST_FILE), os.path.join(base, SECRETS_DIR)):
        try:
            st = os.stat(path)
            out.append((st.st_mtime_ns, st.st_size))
        except OSError:
            out.append(None)
    try:
        for name in sorted(os.listdir(os.path.join(base, SECRETS_DIR))):
            st = os.stat(os.path.join(base, SECRETS_DIR, name))
            out.append((name, st.st_mtime_ns, st.st_size))
    except OSError:
        pass
    return tuple(out)


def _write_list(data, data_dir):
    path = os.path.join(data_dir, LIST_FILE)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, path)


def add_relay(data_dir, relay_id, name, address, region="", contact=""):
    """Adds a relay (or gives a listed one a new secret, revoking the old one) and returns the
    path of its new secret file. ValueError when the entry is not valid."""
    entry = clean_entry({"id": relay_id, "name": name, "address": address, "region": region,
                         "contact": contact})
    try:
        data = read_file(data_dir)
    except FileNotFoundError:
        data = {"relays": []}
    relays = [e for e in data.get("relays", []) if not (isinstance(e, dict) and e.get("id") == relay_id)]
    relays.append(entry)
    data["relays"] = relays
    os.makedirs(os.path.join(data_dir, SECRETS_DIR), mode=0o700, exist_ok=True)
    path = secret_path(relay_id, data_dir)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(os.urandom(32).hex() + "\n")
    os.replace(tmp, path)
    _write_list(data, data_dir)
    return path


def remove_relay(data_dir, relay_id):
    """Revokes a relay: its secret file is deleted and its entry removed. False when it was not
    listed and had no secret."""
    found = False
    try:
        os.remove(secret_path(relay_id, data_dir))
        found = True
    except FileNotFoundError:
        pass
    try:
        data = read_file(data_dir)
    except FileNotFoundError:
        return found
    relays = data.get("relays", [])
    kept = [e for e in relays if not (isinstance(e, dict) and e.get("id") == relay_id)]
    if len(kept) != len(relays):
        data["relays"] = kept
        _write_list(data, data_dir)
        found = True
    return found


def public_entry(entry):
    return {k: entry[k] for k in ("id", "name", "address", "region")}


def signed_document(seed, entries, now=None):
    now = int(time.time() if now is None else now)
    text = json.dumps({"issued": now, "expires": now + EXPIRES,
                       "relays": [public_entry(e) for e in entries]},
                      ensure_ascii=False, separators=(",", ":"))
    return {"format": FORMAT, "key": ed25519.public_key(seed).hex(), "list": text,
            "signature": ed25519.sign(seed, TAG + text.encode()).hex()}


def check_document(document, public, now=None):
    """The relays of a /relays.json document when `public` signed it and it has not expired,
    else ValueError."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ValueError("not a relay list")
    text, signature = document.get("list"), document.get("signature")
    if not isinstance(text, str) or not isinstance(signature, str):
        raise ValueError("not a relay list")
    try:
        signature = bytes.fromhex(signature)
    except ValueError:
        raise ValueError("bad signature") from None
    if not ed25519.verify(public, TAG + text.encode(), signature):
        raise ValueError("bad signature")
    try:
        data = json.loads(text)
        expires, relays = data["expires"], data["relays"]
        if not isinstance(relays, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise ValueError("not a relay list") from None
    if (time.time() if now is None else now) > expires:
        raise ValueError("expired")
    return relays


def document():
    """The current /relays.json body (bytes), or None when no relay is listed."""
    current = stamp()
    now = time.time()
    if _cache["body"] is not None and _cache["stamp"] == current and now - _cache["signed"] < RESIGN:
        return _cache["body"]
    try:
        entries, problems = load_entries()
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as e:
        log.error("relays: %s: %s", LIST_FILE, e)
        return _cache["body"]
    for problem in problems:
        log.warning("relays: %s: %s (left out)", LIST_FILE, problem)
    seed = server_list.load_key(_data_dir)
    if seed is None:
        return None
    body = json.dumps(signed_document(seed, entries, now), ensure_ascii=False).encode()
    _cache.update(stamp=current, signed=now, body=body)
    return body


def handle(method):
    """lsp.handle's answer to /relays.json."""
    if method != "GET":
        return 405, "text/plain", b"GET only"
    body = document()
    if body is None:
        return 404, "text/plain", b"no relays"
    return 200, "application/json", body, ("Cache-Control: max-age=300",)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Add, revoke and list the volunteer relays of "
                                                 "this server (DATA_DIR/relay_list.json).")
    parser.add_argument("--data-dir", default=os.environ.get("FORZA_LIVE_DATA", "forza_live_data"))
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="add a relay, or give a listed one a new secret")
    add.add_argument("id", help="a short name: a-z, 0-9 and -, at most 32")
    add.add_argument("--address", required=True, help="host:port the players reach it at")
    add.add_argument("--name", required=True)
    add.add_argument("--region", default="")
    add.add_argument("--contact", default="")
    remove = sub.add_parser("remove", help="revoke a relay: its secret stops working")
    remove.add_argument("id")
    sub.add_parser("list", help="the listed relays")
    args = parser.parse_args(argv)
    if args.command == "add":
        try:
            path = add_relay(args.data_dir, args.id, args.name, args.address, args.region,
                             args.contact)
        except ValueError as e:
            print(f"not added: {e}")
            return 1
        print(f"added {args.id}; its secret is in {path}")
        print("give that file to the relay's operator (privately): it is the relay's "
              "--relay-secret-file")
        return 0
    if args.command == "remove":
        if not remove_relay(args.data_dir, args.id):
            print(f"{args.id} is not listed")
            return 1
        print(f"revoked {args.id}")
        return 0
    try:
        entries, problems = load_entries(args.data_dir)
    except FileNotFoundError:
        entries, problems = [], [f"no {LIST_FILE} in {args.data_dir}"]
    except ValueError as e:
        entries, problems = [], [str(e)]
    for entry in entries:
        print(f"listed: {entry['id']} {entry['name']} ({entry['address']}, "
              f"{entry['region'] or 'no region'})")
    for problem in problems:
        print(f"left out: {problem}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
