"""Saves that survive a power cut.

The server writes each data file to <name>.tmp and renames it over <name>. The rename alone
is not enough: on XFS and ext4 the rename can reach the disk before the
file's contents, so a power cut leaves an empty file. On 2026-10-26 that emptied
scoreboards.json, friends.json and live_state.json, and the server started with none.

replace() flushes the new contents to disk before the rename and the folder entry after it,
and keeps the file it replaces as <name>.bak (a second link to it: no copy, no moment
without the file). The .bak itself is replaced in one step too: the link is made as
<name>.bak.tmp and renamed over <name>.bak, so a kill at any moment leaves the file in place
and a .bak that is either the one before or the file it replaces. load() reads <name>, and when
that is empty, not valid JSON or cannot be read at all (a read error, its permissions) reads
<name>.bak instead, with a warning in the log. A file that was not there is no reason to read
the .bak: FileNotFoundError, as json.load gives it.

After a load that had to read the .bak, the next replace() of that file keeps the .bak as it is
(#70): it is the copy that could be read, and the file in place is the one that could not. Only
that next replace: once the new file is in place, the one after it keeps it as the .bak again.

A file too big to write at every change has a journal beside it (journal.py): load_digest()
gives it the SHA-256 of the file that was read, the name by which a journal knows the file it
continues, and replace(backup=False) is for the journal's own file, which needs no copy.
"""

import hashlib
import json
import logging
import os

log = logging.getLogger("forza_live")

# Files (absolute paths) whose last load() read their .bak: their next replace() keeps that .bak.
_FROM_BAK = set()


def _fsync_path(path, flags=os.O_RDWR):
    # A file needs a writable handle: Windows' fsync (_commit) refuses a read-only one.
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace(tmp, path, backup=True):
    """os.replace(tmp, path) whose result survives a power cut, keeping the previous file
    as path.bak (not with backup=False, nor when the last load of path had to read the .bak)."""
    _fsync_path(tmp)
    backup = path + ".bak" if backup else None
    if backup and os.path.abspath(path) in _FROM_BAK:
        backup = None  # the file in place could not be read: its .bak is the copy to keep
    try:
        if backup and os.path.getsize(path) > 0:
            link = backup + ".tmp"
            try:
                os.remove(link)  # left by a kill between the two steps below
            except FileNotFoundError:
                pass
            os.link(path, link)
            os.replace(link, backup)  # in one step: there is always a .bak, the old or the new
    except OSError:
        pass  # no previous file, or a file system without links: the save still goes on
    os.replace(tmp, path)
    _FROM_BAK.discard(os.path.abspath(path))
    try:
        _fsync_path(os.path.dirname(os.path.abspath(path)), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        pass  # Windows cannot open a folder; its rename is already durable enough


def _text(path):
    with open(path) as f:
        return json.load(f)


def _bytes(path):
    with open(path, "rb") as f:
        raw = f.read()
    return json.loads(raw), hashlib.sha256(raw).hexdigest()


def _load(path, read):
    key = os.path.abspath(path)
    try:
        found = read(path)
    except FileNotFoundError:
        _FROM_BAK.discard(key)
        raise
    except (OSError, ValueError) as error:
        try:
            found = read(path + ".bak")
        except (OSError, ValueError):
            _FROM_BAK.discard(key)
            raise error
        _FROM_BAK.add(key)
        log.warning("%s %s (%s); using its previous copy %s.bak", path,
                    "is damaged" if isinstance(error, ValueError) else "cannot be read", error,
                    os.path.basename(path))
        return found
    _FROM_BAK.discard(key)
    return found


def load(path):
    """json.load of path, or of path.bak when path is empty, damaged or cannot be read. Raises
    like json.load: FileNotFoundError when path is not there, and when neither can be used the
    error of path itself (ValueError for damaged content, OSError for a read error)."""
    return _load(path, _text)


def load_digest(path):
    """load(), and the SHA-256 (hex) of the bytes of the file it read (path, or path.bak)."""
    return _load(path, _bytes)


def digest(path):
    """The SHA-256 (hex) of a file's bytes, read a megabyte at a time."""
    found = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            found.update(block)
    return found.hexdigest()
