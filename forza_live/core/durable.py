"""Saves that survive a power cut.

The server writes each data file to <name>.tmp and renames it over <name>. The rename alone
is not enough: on XFS and ext4 the rename can reach the disk before the
file's contents, so a power cut leaves an empty file. On 2026-10-26 that emptied
scoreboards.json, friends.json and live_state.json, and the server started with none.

replace() flushes the new contents to disk before the rename and the folder entry after it,
and keeps the file it replaces as <name>.bak (a second link to it: no copy, no moment
without the file). load() reads <name>, and when that is empty or not valid JSON reads
<name>.bak instead, with a warning in the log.

A file too big to write at every change has a journal beside it (journal.py): load_digest()
gives it the SHA-256 of the file that was read, the name by which a journal knows the file it
continues, and replace(backup=False) is for the journal's own file, which needs no copy.
"""

import hashlib
import json
import logging
import os

log = logging.getLogger("forza_live")


def _fsync_path(path, flags=os.O_RDWR):
    # A file needs a writable handle: Windows' fsync (_commit) refuses a read-only one.
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def replace(tmp, path, backup=True):
    """os.replace(tmp, path) whose result survives a power cut, keeping the previous file
    as path.bak (not with backup=False)."""
    _fsync_path(tmp)
    backup = path + ".bak" if backup else None
    try:
        if backup and os.path.getsize(path) > 0:
            try:
                os.remove(backup)
            except FileNotFoundError:
                pass
            os.link(path, backup)
    except OSError:
        pass  # no previous file, or a file system without links: the save still goes on
    os.replace(tmp, path)
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
    try:
        return read(path)
    except ValueError as error:
        try:
            found = read(path + ".bak")
        except (OSError, ValueError):
            raise error
        log.warning("%s is damaged (%s); using its previous copy %s.bak", path, error,
                    os.path.basename(path))
        return found


def load(path):
    """json.load of path, or of path.bak when path is empty or damaged. Raises like
    json.load (FileNotFoundError when neither exists)."""
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
