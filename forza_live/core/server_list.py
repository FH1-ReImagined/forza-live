"""The signed list of Forza Live servers (FH1 #587): GET /servers.json.

The launcher's Servers page lists the servers players can pick. Owners do not add themselves:
they apply, and the operator of the main server adds the servers it checked to
DATA_DIR/server_list.json (docs/operating.md, "Listing community servers"):

    {"self":    {"name": "...", "address": "your.host:21200", "region": "...", "contact": "..."},
     "servers": [{"name": "...", "address": "192.0.2.10:21200", "region": "...",
                  "contact": "...", "verified": true, "id": "..."}]}

`self` is this server: it is listed first, always verified, with this server's id (the one its
leaderboard rows carry, scores.server_id(), #517). Community servers keep their own boards: only
rows with the main server's id are the official ones, so the id tells the launcher and the website
which entry that is. An entry's `id` is optional (what that server tags its rows with).

The list is signed with an Ed25519 key of its own (ed25519.py), not a player's: its seed is
DATA_DIR/server_list_key.txt (64 hex digits), made at the first request when it is missing. The
launcher ships the public key and accepts only a list it signed. GET /servers.json answers

    {"format": "forza-live-servers-1", "key": "<public key, 64 hex>",
     "list": "<the list as JSON text>", "signature": "<128 hex>"}

where the signature is over b"FORZA-SERVERS-V1\\n" + the list text as UTF-8, and the list text is
{"issued": unix time, "expires": unix time, "servers": [entries, this server first]}. The list is
signed again when the file changes and once a day, and expires after EXPIRES seconds, so an old
copy cannot be passed off as the current list for long (it travels over plain HTTP).

`python3 -m forza_live.core.server_list --data-dir DIR` checks the file and prints the public key
(making the key when there is none)."""

import argparse
import json
import logging
import os
import sys
import time

from forza_live.core import ed25519

log = logging.getLogger("forza-live")

LIST_FILE = "server_list.json"
KEY_FILE = "server_list_key.txt"
FORMAT = "forza-live-servers-1"
TAG = b"FORZA-SERVERS-V1\n"
RESIGN = 86400       # seconds a signed list is served before it is signed again
EXPIRES = 14 * 86400  # seconds a signed list is valid for
MAX_SERVERS = 200
LIMITS = {"name": 48, "address": 253 + 8, "region": 32, "contact": 120, "id": 64}

_data_dir = "forza_live_data"
_server_id = lambda: ""  # noqa: E731 (scores.server_id once configured)
_cache = {"stamp": None, "signed": 0.0, "body": None}


def configure(data_dir, server_id=None):
    global _data_dir, _server_id
    _data_dir = data_dir
    if server_id is not None:
        _server_id = server_id
    _cache.update(stamp=None, signed=0.0, body=None)


def valid_address(address):
    """Whether `address` is a server address as players type it: host[:port], the host a name,
    an IPv4 address or an [IPv6] address, the port 1-65534 (the HTTP port is the next one)."""
    if not isinstance(address, str) or not address or len(address) > LIMITS["address"]:
        return False
    host, port = address, None
    if address.startswith("["):
        end = address.find("]")
        if end < 0:
            return False
        host, rest = address[1:end], address[end + 1:]
        if rest:
            if not rest.startswith(":"):
                return False
            port = rest[1:]
        if not host or any(c not in "0123456789abcdefABCDEF:." for c in host):
            return False
    else:
        if address.count(":") > 1:
            return False
        if ":" in address:
            host, port = address.split(":")
        labels = host.rstrip(".").split(".")
        if not host or any(not label or len(label) > 63 or label[0] == "-" or label[-1] == "-"
                           or any(not (c.isascii() and (c.isalnum() or c == "-")) for c in label)
                           for label in labels):
            return False
    if port is not None:
        if not port.isdigit() or not 0 < int(port) < 65535:
            return False
    return True


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


def clean_entry(entry, own=False):
    """A listed server as it is published, or ValueError saying what is wrong with it."""
    if not isinstance(entry, dict):
        raise ValueError("not an object")
    out = {"name": _text(entry, "name"), "address": _text(entry, "address").lower(),
           "region": _text(entry, "region", required=False),
           "contact": _text(entry, "contact", required=False)}
    if not valid_address(out["address"]):
        raise ValueError(f"address: {out['address']!r} is not host[:port]")
    verified = entry.get("verified", False)
    if not isinstance(verified, bool):
        raise ValueError("verified: true or false")
    out["verified"] = True if own else verified
    server = _server_id() if own else _text(entry, "id", required=False)
    if server:
        out["id"] = server
    if own:
        out["main"] = True
    return out


def load_entries(path=None):
    """(entries, problems): the operator's file read and checked, this server first. Entries with
    a problem are left out (and named in problems); a later entry with an address already listed
    too. FileNotFoundError when there is no file, ValueError when it is not a JSON object."""
    path = path or os.path.join(_data_dir, LIST_FILE)
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise ValueError(f"{LIST_FILE}: not a JSON object")
    entries, problems, seen = [], [], set()
    own = data.get("self")
    if own is not None:
        try:
            entries.append(clean_entry(own, own=True))
            seen.add(entries[0]["address"])
        except ValueError as e:
            problems.append(f"self: {e}")
    others = data.get("servers", [])
    if not isinstance(others, list):
        problems.append("servers: not a list")
        others = []
    for i, entry in enumerate(others):
        try:
            entry = clean_entry(entry)
        except ValueError as e:
            problems.append(f"servers[{i}]: {e}")
            continue
        if entry["address"] in seen:
            problems.append(f"servers[{i}]: {entry['address']} is already listed")
            continue
        if len(entries) >= MAX_SERVERS:
            problems.append(f"servers[{i}]: more than {MAX_SERVERS} servers")
            break
        seen.add(entry["address"])
        entries.append(entry)
    return entries, problems


def load_key(data_dir=None, create=True):
    """The list's signing seed (32 bytes) from DATA_DIR/server_list_key.txt; a new one is written
    when there is none and `create`. None when there is none (or it is damaged: logged)."""
    path = os.path.join(data_dir or _data_dir, KEY_FILE)
    try:
        with open(path, encoding="ascii") as f:
            seed = bytes.fromhex(f.read().strip())
        if len(seed) != 32:
            raise ValueError("not 64 hex digits")
        return seed
    except FileNotFoundError:
        if not create:
            return None
    except (OSError, ValueError) as e:
        log.error("server list: %s is damaged (%s): the list is not published", path, e)
        return None
    seed = os.urandom(32)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(seed.hex() + "\n")
    log.warning("server list: made a new signing key %s; its public key is %s (the launcher "
                "accepts lists signed by the key it ships)", path, ed25519.public_key(seed).hex())
    return seed


def signed_document(seed, entries, now=None):
    """The /servers.json document of `entries`, signed with `seed`."""
    now = int(time.time() if now is None else now)
    text = json.dumps({"issued": now, "expires": now + EXPIRES, "servers": entries},
                      ensure_ascii=False, separators=(",", ":"))
    return {"format": FORMAT, "key": ed25519.public_key(seed).hex(), "list": text,
            "signature": ed25519.sign(seed, TAG + text.encode()).hex()}


def check_document(document, public, now=None):
    """The servers of a /servers.json document when `public` (32 bytes) signed it and it has not
    expired, else ValueError (the launcher's server_list.py does the same)."""
    if not isinstance(document, dict) or document.get("format") != FORMAT:
        raise ValueError("not a server list")
    text, signature = document.get("list"), document.get("signature")
    if not isinstance(text, str) or not isinstance(signature, str):
        raise ValueError("not a server list")
    try:
        signature = bytes.fromhex(signature)
    except ValueError:
        raise ValueError("bad signature") from None
    if not ed25519.verify(public, TAG + text.encode(), signature):
        raise ValueError("bad signature")
    try:
        data = json.loads(text)
        expires, servers = data["expires"], data["servers"]
        if not isinstance(servers, list):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        raise ValueError("not a server list") from None
    if (time.time() if now is None else now) > expires:
        raise ValueError("expired")
    return servers


def document():
    """The current /servers.json body (bytes), or None when there is no list to publish."""
    path = os.path.join(_data_dir, LIST_FILE)
    try:
        st = os.stat(path)
    except OSError:
        return None
    stamp = (st.st_mtime_ns, st.st_size, _server_id())
    now = time.time()
    if _cache["body"] is not None and _cache["stamp"] == stamp and now - _cache["signed"] < RESIGN:
        return _cache["body"]
    try:
        entries, problems = load_entries(path)
    except (OSError, ValueError) as e:
        log.error("server list: %s: %s", path, e)
        return _cache["body"]  # the last good list, if there was one
    for problem in problems:
        log.warning("server list: %s: %s (left out)", LIST_FILE, problem)
    seed = load_key()
    if seed is None:
        return None
    body = json.dumps(signed_document(seed, entries, now), ensure_ascii=False).encode()
    _cache.update(stamp=stamp, signed=now, body=body)
    log.info("server list: signed %d server(s)", len(entries))
    return body


def handle(method):
    """lsp.handle's answer to /servers.json."""
    if method != "GET":
        return 405, "text/plain", b"GET only"
    body = document()
    if body is None:
        return 404, "text/plain", b"no server list"
    return 200, "application/json", body, ("Cache-Control: max-age=300",)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Check DATA_DIR/server_list.json and print the "
                                                 "public key of the list's signing key.")
    parser.add_argument("--data-dir", default=os.environ.get("FORZA_LIVE_DATA", "forza_live_data"))
    args = parser.parse_args(argv)
    configure(args.data_dir)
    try:
        entries, problems = load_entries()
    except FileNotFoundError:
        entries, problems = [], [f"no {LIST_FILE} in {args.data_dir}"]
    except ValueError as e:
        entries, problems = [], [str(e)]
    for entry in entries:
        print(f"listed: {entry['name']} ({entry['address']}, {entry['region'] or 'no region'}"
              f"{', verified' if entry['verified'] else ''})")
    for problem in problems:
        print(f"left out: {problem}")
    seed = load_key(args.data_dir)
    if seed is None:
        return 1
    print(f"public key: {ed25519.public_key(seed).hex()}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
