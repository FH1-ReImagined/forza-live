"""A journal beside a JSON file that is too big to write at every change.

scoreboards.json is 26 MB on a busy server. Written whole at every new score, it took 1.7 s of
the event loop each time, and nothing else was answered meanwhile (#67). With a Journal a
change costs one line appended to <file>.journal and flushed to disk: milliseconds, whatever
the file's size. The file itself is written whole only every so often, by a thread
(checkpoint). A start reads the file, then the journal's lines the file does not hold yet.

The journal's first line names the files it continues, each by the SHA-256 of its bytes, with
how many of the journal's lines that file already holds. Every other line is one change, a
JSON object whose meaning is its store's:

    {"journal": 1, "continues": {"<the .bak's SHA-256>": 0, "<the file's SHA-256>": 12}}
    {"boards": {"3:199:0": {"0009000000000A01": {...}}}}
    ...

A file that is none of those was written by something else: a previous version of the server,
a restored backup, an edit by hand. The journal is not applied to it and is kept as
<file>.journal.unused. NO_FILE and UNREADABLE stand for a file that is not there and for one
that cannot be read.

A checkpoint, in this order: the new file is written beside the one in place (<file>.tmp) and
flushed; the journal is rewritten to start where the file in place starts and to name both;
the new file takes the place of the old one, which durable.replace keeps as .bak. Stopped at
any point, what is left is a file and a journal that continues it. As the journal reaches back
to the .bak, the previous copy durable falls back to is made whole by it too.

Threads: append(), mark() and behind() are the event loop's, checkpoint() a thread's (or the
stop's). A lock keeps the journal's file and what is known of it: the loop waits for it only
while a checkpoint rewrites the journal, a file of two checkpoints' changes at most. And the
new file is flushed as it is written (Paced), so that megabytes of it are never waiting for the
disk at once: the journal's own flush, on the loop, would wait behind them.
"""

import json
import logging
import os
import threading

from forza_live.core import durable

log = logging.getLogger("forza-live")

NO_FILE, UNREADABLE = "none", "unreadable"
FLUSH_EVERY = 1 << 20  # characters of a new file between two flushes to disk (Paced)


class Paced:
    """What a checkpoint's dump writes to: the text file f, flushed to disk every FLUSH_EVERY
    characters."""

    def __init__(self, f):
        self.f, self.waiting = f, 0

    def write(self, text):
        self.f.write(text)
        self.waiting += len(text)
        if self.waiting >= FLUSH_EVERY:
            self.flush()

    def flush(self):
        self.f.flush()
        os.fsync(self.f.fileno())
        self.waiting = 0


class Journal:
    def __init__(self, path):
        """path: the file the journal is beside (scoreboards.json: scoreboards.journal)."""
        self.path = path
        self.name = os.path.splitext(path)[0] + ".journal"
        self._lock = threading.Lock()     # the journal's file and everything below
        self._writing = threading.Lock()  # one checkpoint at a time
        self._file = None       # the journal, open to append; None: made anew at the next append
        self.closed = False
        self.base = NO_FILE     # the file in place, by its SHA-256
        self.continues = {NO_FILE: 0}  # as the first line has it: how many of `lines` each file holds
        self.lines = []         # the changes in the journal's file, a line (bytes) each
        self.first = 0          # how many changes went before lines[0], dropped by checkpoints

    def load(self):
        """(what the file holds, or None; the changes it lacks, in order). Never raises: a file
        that cannot be read is logged and counts as none, a journal that cannot be used is put
        aside."""
        try:
            data, self.base = durable.load_digest(self.path)
        except FileNotFoundError:
            data, self.base = None, NO_FILE
        except (OSError, ValueError) as e:
            log.error("%s cannot be read (%s): starting without what it held", self.path, e)
            data, self.base = None, UNREADABLE
        self.continues, self.lines, self.first = {self.base: 0}, [], 0
        return data, self._read()

    def _read(self):
        try:
            with open(self.name, "rb") as f:
                lines = f.read().split(b"\n")
        except FileNotFoundError:
            return []
        except OSError as e:
            log.warning("%s cannot be read (%s): not applied", self.name, e)
            return []
        cut = lines.pop()  # after the last newline: nothing, or a line the stop cut short
        head, rest = (lines[0] if lines else b""), lines[1:]
        try:
            known = {str(k): int(v) for k, v in json.loads(head)["continues"].items()}
        except (ValueError, TypeError, KeyError, AttributeError):
            known = None
        if known is None or self.base not in known:
            self._put_aside(known, len(rest))
            return []
        held = known[self.base]
        changes, lacking, damaged = [], [], 0
        for n, line in enumerate(rest):
            try:
                change = json.loads(line)
            except ValueError:
                change = None
            if not isinstance(change, dict):
                damaged += 1
            elif n >= held:
                changes.append(change)
                lacking.append(line + b"\n")
        if damaged or cut or held > len(rest):
            # Not whole: made anew at the next append, with the changes the file lacks only.
            self.lines = lacking
            if damaged or held > len(rest):
                log.warning("%s: %d damaged lines skipped, %d changes applied", self.name,
                            damaged, len(changes))
            else:
                log.info("%s: its last line was cut short by the stop (a change nobody was told "
                         "was kept); %d changes applied", self.name, len(changes))
            return changes
        self.continues, self.lines = known, [line + b"\n" for line in rest]
        try:
            self._file = open(self.name, "ab")
        except OSError:
            pass  # made anew at the next append
        return changes

    def _put_aside(self, known, changes):
        unused = self.name + ".unused"
        try:
            os.replace(self.name, unused)
        except OSError as e:
            log.warning("%s cannot be renamed (%s)", self.name, e)
        if known is None:
            log.warning("%s is damaged: its %d lines are not applied. Kept as %s", self.name,
                        changes, unused)
            return
        newer = changes - max(known.values(), default=0)
        log.log(logging.WARNING if newer > 0 else logging.INFO,
                "%s does not continue the %s that is here (written by a previous version of the "
                "server, restored or edited): it is not applied. %s. Kept as %s", self.name,
                os.path.basename(self.path),
                "At least %d of its changes were in no file it knew" % newer if newer > 0
                else "Every change it had was in the last file it knew, so nothing is lost",
                unused)

    def mark(self):
        """The changes appended so far, of all since load(): what a copy of the store made now
        holds, for checkpoint()."""
        with self._lock:
            return self.first + len(self.lines)

    def behind(self):
        """Whether the journal has changes the file in place lacks."""
        with self._lock:
            return len(self.lines) > self.continues.get(self.base, 0)

    def append(self, change):
        """One change (a JSON object) in the journal and flushed to disk when this returns.
        OSError when it could not be written: it is not in the journal then, and the journal's
        file is made anew at the next append (whatever part of the line got there goes)."""
        line = json.dumps(change, separators=(",", ":")).encode() + b"\n"
        with self._lock:
            if self._file is None:
                self._rewrite(self.continues, self.lines)
            try:
                self._file.write(line)
                self._file.flush()
                os.fsync(self._file.fileno())
            except OSError:
                self._shut()
                raise
            self.lines.append(line)

    def checkpoint(self, mark, dump):
        """The file written whole. dump(f) writes it as text with f.write, from a copy of the
        store made when mark() gave `mark`. Slow for a big file: a thread's work. False when a
        later copy is already in place, or the journal is closed. OSError when it failed: the
        file in place and the journal are as good as before."""
        with self._writing:
            with self._lock:
                if self.closed or mark < self.first + self.continues.get(self.base, 0):
                    return False
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                paced = Paced(f)
                dump(paced)
                paced.flush()
            new = durable.digest(tmp)
            with self._lock:
                # The file in place becomes the .bak: the journal starts where it starts.
                held = self.continues.get(self.base, 0)
                self._rewrite({self.base: 0, new: mark - self.first - held}, self.lines[held:])
                self.first += held
            durable.replace(tmp, self.path)
            with self._lock:
                self.base = new
            return True

    def _rewrite(self, continues, lines):
        """The journal's file made anew with these (the lock is held): written beside it and
        renamed over it, so that it is whole at any moment."""
        head = json.dumps({"journal": 1, "continues": continues}).encode() + b"\n"
        tmp = self.name + ".tmp"
        with open(tmp, "wb") as f:
            f.write(head + b"".join(lines))
        self._shut()  # Windows renames nothing over an open file
        durable.replace(tmp, self.name, backup=False)
        self._file = open(self.name, "ab")
        self.continues, self.lines = continues, lines

    def _shut(self):
        file, self._file = self._file, None
        if file is not None:
            try:
                file.close()
            except OSError:
                pass

    def close(self):
        """No more appends or checkpoints (the store is configured again)."""
        with self._lock:
            self.closed = True
            self._shut()
