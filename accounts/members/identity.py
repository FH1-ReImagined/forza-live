"""Email identities without the address: HMAC-SHA256 under a pepper made once and kept beside
the database. allauth stores and looks up "<hmac>@email.invalid"; the real address lives only in
the request that mails the code."""

import hashlib
import hmac
import os
import secrets
import threading

from django.conf import settings

DOMAIN = "email.invalid"
_lock = threading.Lock()
_pepper = {}


def pepper():
    path = settings.EMAIL_PEPPER_FILE
    with _lock:
        if path in _pepper:
            return _pepper[path]
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            path.parent.mkdir(parents=True, exist_ok=True)
            data = secrets.token_bytes(32)
            tmp = path.with_suffix(".tmp")
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(data)
                f.flush()
                os.fsync(f.fileno())
            # Never replaces an existing pepper: without it no email account matches again.
            os.link(tmp, path)
            os.remove(tmp)
        if len(data) != 32:
            raise RuntimeError("%s is not a 32-byte pepper: restore it from a backup" % path)
        _pepper[path] = data
        return data


def normalize(address):
    return address.strip().lower()


def stand_in(address):
    """The address allauth keeps for this one."""
    digest = hmac.new(pepper(), normalize(address).encode("utf-8"), hashlib.sha256).hexdigest()
    return "%s@%s" % (digest, DOMAIN)


def is_stand_in(address):
    return isinstance(address, str) and address.endswith("@" + DOMAIN)
