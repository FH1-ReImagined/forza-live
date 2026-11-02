"""Saves that survive a power cut.

The server writes each data file to <name>.tmp and renames it over <name>. The rename alone
is not enough: on XFS (Unraid's cache) and ext4 the rename can reach the disk before the
file's contents, so a power cut leaves an empty file. On 2026-10-26 that emptied
scoreboards.json, friends.json and live_state.json, and the server started with none.

replace() flushes the new contents to disk before the rename and the folder entry after it,
and keeps the file it replaces as <name>.bak (a second link to it: no copy, no moment
without the file). load() reads <name>, and when that is empty or not valid JSON reads
<name>.bak instead, with a warning in the log.
"""

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


def replace(tmp, path):
    """os.replace(tmp, path) whose result survives a power cut, keeping the previous file
    as path.bak."""
    _fsync_path(tmp)
    backup = path + ".bak"
    try:
        if os.path.getsize(path) > 0:
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


def load(path):
    """json.load of path, or of path.bak when path is empty or damaged. Raises like
    json.load (FileNotFoundError when neither exists)."""
    try:
        with open(path) as f:
            return json.load(f)
    except ValueError as error:
        try:
            with open(path + ".bak") as f:
                data = json.load(f)
        except (OSError, ValueError):
            raise error
        log.warning("%s is damaged (%s); using its previous copy %s.bak", path, error,
                    os.path.basename(path))
        return data
