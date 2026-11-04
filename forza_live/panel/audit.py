"""The operator panel's audit log: one JSON line for every change made from the panel, in
DATA_DIR/operator_audit.jsonl (who, from where, what, the target, the answer's status, when).

Only what the operator sent is kept, cut to a few hundred characters a value: the panel never
sends the admin token in a body, and keys that name a secret are dropped all the same. When the
file passes MAX_BYTES it becomes operator_audit.jsonl.1 (the one before that is dropped)."""

import json
import logging
import os
import threading
import time

log = logging.getLogger("forza-live")

FILE = "operator_audit.jsonl"
MAX_BYTES = 4 << 20
MAX_TEXT = 300          # characters kept of one value
MAX_ITEMS = 32          # keys or items kept of one object or list
SHOWN = 500             # entries the panel can list
SECRET_KEYS = ("token", "secret", "password", "webhook")

_lock = threading.Lock()
_data_dir = ["forza_live_data"]


def configure(data_dir):
    _data_dir[0] = data_dir


def path():
    return os.path.join(_data_dir[0], FILE)


def clean(value, depth=0):
    """value as it is kept: short text, small objects, no secrets."""
    if isinstance(value, str):
        text = "".join(c if c.isprintable() else " " for c in value)
        return text[:MAX_TEXT]
    if isinstance(value, bool) or value is None or isinstance(value, (int, float)):
        return value
    if depth >= 3:
        return "..."
    if isinstance(value, dict):
        return {clean(str(k))[:64]: clean(v, depth + 1) for k, v in list(value.items())[:MAX_ITEMS]
                if not any(s in str(k).lower() for s in SECRET_KEYS)}
    if isinstance(value, (list, tuple)):
        return [clean(v, depth + 1) for v in list(value)[:MAX_ITEMS]]
    return clean(str(value))


def record(action, target, detail, status, source=""):
    """Appends one entry; never raises (the action itself is done)."""
    entry = {"at": int(time.time()), "who": "operator", "from": clean(str(source))[:64],
             "action": clean(action)[:64], "target": clean(str(target or ""))[:128],
             "status": int(status), "detail": clean(detail or {})}
    line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
    with _lock:
        try:
            os.makedirs(_data_dir[0], exist_ok=True)
            p = path()
            try:
                if os.path.getsize(p) + len(line) > MAX_BYTES:
                    os.replace(p, p + ".1")
            except FileNotFoundError:
                pass
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            log.warning("operator panel: cannot write the audit log: %s", e)
    log.warning("operator panel: %s %s -> %d", entry["action"], entry["target"] or "-",
                entry["status"])
    return entry


def _read(p):
    try:
        with open(p, encoding="utf-8") as f:
            lines = f.readlines()
    except OSError:
        return []
    out = []
    for line in lines:
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if isinstance(entry, dict):
            out.append(entry)
    return out


def recent(limit=SHOWN):
    """The newest entries first, at most `limit`."""
    with _lock:
        entries = _read(path())
        if len(entries) < limit:
            entries = _read(path() + ".1") + entries
    return list(reversed(entries))[:max(0, limit)]


def last_reason(action, target):
    """The reason of the newest successful `action` on `target` (e.g. a ban's), or ""."""
    for entry in recent():
        if entry.get("action") == action and entry.get("target") == target and \
                entry.get("status") == 200:
            detail = entry.get("detail") or {}
            return detail.get("reason", "") if isinstance(detail, dict) else ""
    return ""
