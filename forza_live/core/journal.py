"""A journal beside a JSON file that is too big to write at every change.

scoreboards.json is 26 MB on a busy server. Written whole at every new score, it took 1.7 s of
the event loop each time, and nothing else was answered meanwhile (#67). With a Journal a
change costs one line appended to <file>.journal and flushed to disk: milliseconds, whatever
the file's size. The file itself is written whole only every so often, by a thread
(checkpoint). A start reads the file, then the journal's lines the file does not hold yet.

The journal's first line names the files it continues, the oldest first, each by the SHA-256
of its bytes, with how many of the journal's lines that file already holds. Every other line is
one change, a JSON object whose meaning is its store's:

    {"journal": 1, "continues": {"<the .bak's SHA-256>": 0, "<the file's SHA-256>": 12}}
    {"boards": {"3:199:0": {"0009000000000A01": {...}}}}
    ...

A file that is none of those was written by something else: a previous version of the server,
a restored backup, an edit by hand. The journal is not applied to it and is kept as
<file>.journal.unused.<n>, a new n each time. NO_FILE and UNREADABLE stand for a file that is
not there and for one whose content is damaged, with no previous copy (or one damaged too). A
file that is there but cannot be read at all (a read error, its permissions), with no previous
copy that can be, is no store to start from, nor is a damaged file whose previous copy is there
but cannot be read: load() raises Unreadable, and leaves every file as it is.

A checkpoint, in this order: the new file is written beside the one in place (<file>.tmp) and
flushed; the journal is rewritten to name it too, without the lines the .bak already holds;
the new file takes the place of the old one, which durable.replace keeps as .bak. Stopped at
any point, the file in place and its .bak are both files the journal continues: the previous
copy durable falls back to is made whole by it as the file itself is. Which file the .bak is, the
checkpoint reads from the .bak itself (its SHA-256), not from the order of the names: a new file
can have the very bytes of the .bak (#70). With no .bak (a file system without links has none),
or one the journal does not name, the journal keeps only the lines the file in place lacks; with
a .bak that is there but cannot be read, it keeps every name and line until it can be.

Threads: append(), mark() and behind() are the event loop's, checkpoint() a thread's (or the
stop's). A lock keeps the journal's file and what is known of it: the loop waits for it only
while a checkpoint rewrites the journal, a file of three checkpoints' changes at most (unless
the .bak cannot be read, above). And the
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
NOT_READ = object()  # Journal._bak(): a .bak that is there but cannot be read
FLUSH_EVERY = 1 << 20  # characters of a new file between two flushes to disk (Paced)


class Unreadable(OSError):
    """The file is there but cannot be read, and neither can its previous copy; or the file is
    damaged and its previous copy is there but cannot be read: a start would have none of what
    they hold. Logged by Journal.load, which raises it."""


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
        self._whole = False     # the journal's file is whole; if not, the next append makes it anew
        self.closed = False
        self._warned = False    # the log was told the .bak cannot be read, at a checkpoint
        self.base = NO_FILE     # the file in place, by its SHA-256
        # As the first line has it: how many of `lines` each file holds, the oldest file first
        self.continues = {NO_FILE: 0}
        self.lines = []         # the changes in the journal's file, a line (bytes) each
        self.first = 0          # how many changes went before lines[0], dropped by checkpoints

    def load(self):
        """(what the file holds, or None; the changes it lacks, in order). A file whose content
        is damaged, with no previous copy or a damaged one, is logged and counts as none, a
        journal that cannot be used is put aside. Unreadable when the file is there but cannot
        be read, nor its previous copy, or when it is damaged and its previous copy is there but
        cannot be read: nothing is changed then, nor put aside."""
        try:
            data, self.base = durable.load_digest(self.path)
        except FileNotFoundError:
            data, self.base = None, NO_FILE
        except ValueError as e:
            try:
                durable.digest(self.path + ".bak")
            except FileNotFoundError:
                pass
            except OSError as b:
                # Damaged, and the .bak that could have taken its place is there but cannot be
                # read: running on empty boards would make the damaged file the next .bak.
                log.error("%s is damaged (%s), and its previous copy %s.bak is there but cannot "
                          "be read (%s): not started, rather than run without what they hold. "
                          "Make the .bak readable again, or move both out of the folder to start "
                          "without them", self.path, e, os.path.basename(self.path), b)
                raise Unreadable(b.errno, "%s.bak cannot be read: %s" % (self.path, b)) from b
            log.error("%s cannot be read (%s): starting without what it held", self.path, e)
            data, self.base = None, UNREADABLE
        except OSError as e:
            log.error("%s cannot be read (%s), and there is no previous copy %s.bak that can be: "
                      "not started, rather than run without what it holds. Make it readable "
                      "again, or move it out of the folder to start without it", self.path, e,
                      os.path.basename(self.path))
            raise Unreadable(e.errno, "%s cannot be read: %s" % (self.path, e)) from e
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
        if damaged or held > len(rest):
            # Not whole: made anew at the next append, with the changes the file lacks only.
            self.lines = lacking
            log.warning("%s: %d damaged lines skipped, %d changes applied", self.name,
                        damaged, len(changes))
            return changes
        if cut:
            # Only the last line is cut short: every whole line is where the first line counts
            # it, so the names stay, with every line, and the previous copy is still covered
            # (#70). The file is made anew at the next append, without the cut part.
            log.info("%s: its last line was cut short by the stop (a change nobody was told "
                     "was kept); %d changes applied", self.name, len(changes))
        self.continues, self.lines = known, [line + b"\n" for line in rest]
        self._whole = not cut
        return changes

    def _put_aside(self, known, changes):
        """The journal's file renamed <name>.unused.<n>, n one more than the last there is (an
        earlier one is never written over: it may be the only copy of changes that were
        acknowledged), and the log told what the server knows of its changes. Whether the file
        that is here has them it cannot know, and does not say."""
        folder, start = os.path.split(self.name + ".unused.")
        try:
            taken = []
            for name in os.listdir(folder or "."):
                if name.startswith(start) and name[len(start):].isdecimal():
                    try:
                        taken.append(int(name[len(start):]))
                    except ValueError:
                        pass  # a name of someone else's: not one of the numbers
            unused = "%s%d" % (self.name + ".unused.", max(taken, default=0) + 1)
            os.replace(self.name, unused)
            where = "Kept as %s" % unused
        except OSError as e:
            where = "It could not be renamed (%s): the next change writes over it" % e
        if known is None:
            log.warning("%s is damaged: its %d lines are not applied. %s", self.name, changes,
                        where)
            return
        newer = changes - max(known.values(), default=0)
        if not changes:
            told = "It held no change"
        elif newer > 0:
            told = "At least %d of its %d changes were in no whole file the server had written" \
                % (newer, changes)
        else:
            told = "The server had written its %d changes to a whole file, but cannot tell " \
                "whether the file here has them" % changes
        log.log(logging.WARNING if changes else logging.INFO,
                "%s does not continue the %s that is here (written by a previous version of the "
                "server, restored or edited): it is not applied. %s. %s", self.name,
                os.path.basename(self.path), told, where)

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
            if not self._whole:
                self._rewrite(self.continues, self.lines)
            try:
                # opened for each line: nothing holds the file between two (Windows renames
                # and deletes no file that is open)
                with open(self.name, "ab") as f:
                    f.write(line)
                    f.flush()
                    os.fsync(f.fileno())
            except OSError:
                self._whole = False
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
            bak = self._bak()
            with self._lock:
                # The journal names the new file too, and loses the lines the .bak holds (the
                # file in place has them as well). Not the lines the file in place holds: it is
                # not the .bak before the rename below, and a start that falls back to the
                # .bak, at whatever moment this stopped, needs them.
                held = self.continues.get(self.base, 0)
                if bak is NOT_READ:
                    # The .bak is there and could not be read: it may be any file the journal
                    # names, so every name stays, with every line, until it can be read again.
                    if not self._warned:
                        log.warning("%s.bak cannot be read: %s keeps every change until it "
                                    "can be", self.path, self.name)
                        self._warned = True
                    older, drop = list(self.continues.items()), 0
                elif bak in self.continues:
                    older = [(bak, self.continues[bak])]
                    drop = min(self.continues[bak], held)
                else:
                    # No .bak (a file system without links has none), or one the journal does
                    # not continue: a start that falls back to it does not apply the journal.
                    # Only the file in place needs lines, those it lacks.
                    older, drop = [], held
                continues = {}
                for name, count in older + [(self.base, held), (new, mark - self.first)]:
                    continues.pop(name, None)  # the same bytes twice: once, as the later file
                    continues[name] = count - drop
                self._rewrite(continues, self.lines[drop:])
                self.first += drop
            durable.replace(tmp, self.path)
            with self._lock:
                self.base = new
            return True

    def _bak(self):
        """The SHA-256 of the .bak there is now: what a start that falls back to it reads. Read
        from the file, the order of the names cannot say it (#70). None when there is no .bak,
        NOT_READ when it is there but could not be read."""
        try:
            found = durable.digest(self.path + ".bak")
        except FileNotFoundError:
            return None
        except OSError:
            return NOT_READ
        self._warned = False
        return found

    def _rewrite(self, continues, lines):
        """The journal's file made anew with these (the lock is held): written beside it and
        renamed over it, so that it is whole at any moment."""
        head = json.dumps({"journal": 1, "continues": continues}).encode() + b"\n"
        tmp = self.name + ".tmp"
        with open(tmp, "wb") as f:
            f.write(head + b"".join(lines))
        durable.replace(tmp, self.name, backup=False)
        self.continues, self.lines, self._whole = continues, lines, True

    def close(self):
        """No more checkpoints (the store is configured again: a thread still writing the file
        of the folder before stops at its next step)."""
        with self._lock:
            self.closed = True
