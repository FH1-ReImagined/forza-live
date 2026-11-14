"""Email identities without the address: HMAC-SHA256 under a pepper made once and kept beside
the database. allauth stores and looks up "<hmac>@email.invalid"; the real address lives only in
the request that mails the code."""

import hashlib
import hmac
import ipaddress
import os
import re
import secrets
import threading
import unicodedata

from django.conf import settings

DOMAIN = "email.invalid"
GMAIL = ("gmail.com", "googlemail.com")  # one mailbox whatever the dots, under either name
# A local part as it is written without quotes (RFC 5322's dot-atom, lower case).
PLAIN_LOCAL = re.compile(r"[-!#$%&'*+/=?^_`{}|~0-9a-z]+(\.[-!#$%&'*+/=?^_`{}|~0-9a-z]+)*")
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


def mailbox(address):
    """The mailbox an address is delivered to, as far as it can be told without asking its mail
    server, or None for an address whose domain is not written the way mail is sent to it. For
    the limits only: the stand-in stays that of the address as typed, so no account moves.

    The domain is the one IDNA names (what the mail is sent to): an address whose domain reads
    differently after that mapping (full-width or other compatibility letters, a soft hyphen) is
    another spelling of someone's address and is refused, so there is one spelling per domain
    besides its xn-- form. The local part loses its quotes, its +tag and its compatibility
    letters, and for Gmail its dots.

    Refused too, as spellings nobody types that a mail server may still deliver to somebody's
    mailbox: a quoted local part that holds anything quotes are needed for (a backslash quotes
    the character after it, which makes one more spelling of every address), and a domain that
    is an address, in brackets or not."""
    local, at, domain = normalize(address).rpartition("@")
    if not at or not local or not domain:
        return None
    if domain.startswith("[") or domain.endswith("]"):
        return None
    try:
        ipaddress.ip_address(domain)
        return None
    except ValueError:
        pass
    try:
        ascii_domain = domain.encode("idna").decode("ascii")
        shown = ascii_domain.encode("ascii").decode("idna")
    except UnicodeError:
        return None
    if domain not in (ascii_domain, shown):
        return None
    local = unicodedata.normalize("NFKC", local).lower()
    if len(local) > 2 and local[0] == local[-1] == '"':
        local = local[1:-1]
        if not PLAIN_LOCAL.fullmatch(local):
            return None
    if '"' in local or "\\" in local:
        return None
    local = local.split("+", 1)[0]
    if ascii_domain in GMAIL:
        local, ascii_domain = local.replace(".", ""), GMAIL[0]
    return "%s@%s" % (local, ascii_domain)


def mailbox_key(box):
    """What the per-address limits count a mailbox() under: its HMAC, never the address."""
    return hmac.new(pepper(), b"mailbox|" + box.encode("utf-8"), hashlib.sha256).hexdigest()
