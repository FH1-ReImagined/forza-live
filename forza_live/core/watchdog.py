"""The event loop's watchdog (#74): what holds the loop too long, in the log and on /status.

The server is one asyncio loop. Whatever runs on it long pauses every player: twice that was a
whole file written on the loop (#69, #73), found from players' reports. This times the work
where it enters the loop and logs what held it longer than a limit (--stall-limit, 50 ms):

- a datagram, by its packet type (server.ForzaLive.datagram_received calls report());
- each step of a task: the task factory that start() puts on the loop wraps every task's
  coroutine (Timed), so the stretch a coroutine runs between two awaits that wait is timed. An
  HTTP connection is a task: a title-service call, a web route or an operator route is the
  step that answers it, named by serving() (server.http_handler). The periodic tasks
  (keep_written, keep_counted) are tasks too;
- the main loop's tick, a call at a time (call());
- anything else (a plain callback, the transports' own work, the main loop between its timed
  calls) only shows as the loop being late: a heartbeat task wakes every few milliseconds and
  logs a stall when it woke late by more than the limit and no unit timed above explains it.

The work is over when it is measured, so where it was is taken while it runs: a watcher thread
samples the loop thread's stack (sys._current_frames) once the heartbeat is late by the stack
limit (--stall-stack, 250 ms), once per stall, and logs it.

Off (--stall-limit 0): no task factory, no heartbeat, no thread, and the datagram's check is one
comparison with an infinite LIMIT. The lines are rate limited (LINES_PER_MINUTE of each kind;
the rest counted, and a summary says how many were left out and which was the worst). /status
has the count of the last hour and its worst (status()). Nothing here names an address, a key or
a token: a datagram names its player as the server's other datagram lines do (#id 'gamertag'), a
request a route the server knows without its query or ids (route_of: any other path is OTHER),
a title-service call its service and method. /status names no operator route, only its kind.
"""

import asyncio
import collections
import collections.abc
import logging
import os
import re
import sys
import threading
import time
import types

log = logging.getLogger("forza-live")

clock = time.perf_counter  # what the entry points time with (fine-grained on every system)

LIMIT_MS = 50.0  # --stall-limit's default
STACK_MS = 250.0  # --stall-stack's default
LINES_PER_MINUTE = 10  # of each kind; the rest are counted and summed up
HOUR = 3600.0

# Seconds a unit may hold the loop before report() is called: infinite while the watchdog is
# off, so that the entry points' check costs one comparison. configure() and stop() set it.
LIMIT = float("inf")
RUNNING = None  # the Timed whose step runs now (serving() names its request)

# The kinds of the lines, each rate limited by itself.
DATAGRAM, TITLE, HTTP, OPERATOR, TASK, STALL, STACK = (
    "datagram", "title", "http", "operator", "task", "stall", "stack")

METHODS = frozenset(("GET", "HEAD", "POST", "PUT", "DELETE", "OPTIONS", "PATCH"))
OPERATOR_ROUTES = ("/admin", "/operator", "/world/event", "/notify", "/gift", "/broadcast")
SEGMENT = re.compile(r"[a-z][a-z_.-]{0,31}\Z")  # a path's word: lower case, no digits
OTHER = "other"  # the route of a path that no route of the server's starts with
# The routes the server knows, each as its words: ("community", "img"). server.py sets it from
# its routing code (routes_in), so that a route added there is named without a list to keep.
ROUTES = frozenset()
ROUTERS = ("handle", "handles")  # the routing functions of the modules a router calls
NAME = re.compile(r"[^A-Za-z0-9_]")
PACKAGE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # forza_live/
ASYNCIO = os.path.dirname(os.path.abspath(asyncio.__file__))
HERE = os.path.abspath(__file__)
STACK_FRAMES = 8


def words_of(path):
    """A path's first words (SEGMENT, up to three) without its query, and whether more follows."""
    words = path.partition("?")[0].partition("#")[0].strip("/")
    words = words.split("/") if words else []
    kept = []
    for word in words[:3]:
        if not SEGMENT.match(word):
            break
        kept.append(word)
    return tuple(kept), len(kept) < len(words)


def route_of(path):
    """A request's route for the log and /status: the longest route the server knows (ROUTES)
    that its path starts with, and `*` for the rest (an id, a file name, a code); OTHER for a
    path that starts with none, so that no word of a requester's own is ever shown."""
    kept, more = words_of(path)
    if not kept:
        return OTHER if more else "/"
    for n in range(len(kept), 0, -1):
        if kept[:n] in ROUTES:
            return "/" + "/".join(kept[:n]) + ("/*" if more or n < len(kept) else "")
    return OTHER


def routes_in(*functions):
    """The routes these routing functions know (ROUTES): every path among their constants, the
    module constants they name (owners.PREFIX = "/owner/") and those of the routing functions
    (ROUTERS) of the modules they call, theirs too, as the words route_of matches."""
    found, seen, todo = set(), set(), list(functions)

    def add(value):
        if isinstance(value, str) and value.startswith("/"):
            words = words_of(value)[0]
            if words:
                found.add(words)
        elif isinstance(value, (tuple, frozenset)):
            for item in value:
                add(item)

    while todo:
        fn = todo.pop()
        code = getattr(fn, "__code__", None)
        if code is None or code in seen:
            continue
        seen.add(code)
        codes, names = [code], set()
        while codes:  # the function's own code and what is nested in it (a lambda, a genexpr)
            c = codes.pop()
            names.update(c.co_names)
            for const in c.co_consts:
                if isinstance(const, types.CodeType):
                    codes.append(const)
                else:
                    add(const)
        for name in names:
            value = fn.__globals__.get(name)
            if isinstance(value, types.ModuleType):
                for attr in names:
                    item = getattr(value, attr, None)
                    if attr in ROUTERS and callable(item):
                        todo.append(item)
                    else:
                        add(item)
            else:
                add(value)
    return frozenset(found)


def describe_request(method, path, headers):
    """(kind, what) of an HTTP request: a title-service call by its service and method, the
    others by their route."""
    method = method if method in METHODS else "?"
    url = path.partition("?")[0]
    if method == "POST" and url.endswith("/o.xtsw"):
        cls = headers.get("x-classname", "").split(",")[0].strip()
        short = NAME.sub("", cls.rsplit(".", 1)[-1])[:48] or "?"
        if cls.startswith("Xls.WebServices."):
            short = "Xls." + short
        return TITLE, "%s.%s" % (short, NAME.sub("", headers.get("x-method", ""))[:48] or "?")
    route = route_of(path)
    kind = OPERATOR if any(route == r or route.startswith(r + "/") for r in OPERATOR_ROUTES) \
        else HTTP
    return kind, method + " " + route


def serving(method, path, headers):
    """The task step running now answers this request (server.http_handler): its line names it.
    Nothing to do while the watchdog is off."""
    if RUNNING is not None:
        RUNNING.what = (method, path, headers)


class Timed(collections.abc.Coroutine):
    """A task's coroutine, each of its steps timed (Watch.task_factory). Every call goes on to
    the coroutine it wraps; there is no __next__, so a task always calls send() and throw()."""

    __slots__ = ("coro", "what")

    def __init__(self, coro):
        self.coro = coro
        self.what = None  # (method, path, headers) of the request it answers (serving())

    def send(self, value):
        return step(self, self.coro.send, value)

    def throw(self, *args):
        return step(self, self.coro.throw, *args)

    def close(self):
        return self.coro.close()

    def __await__(self):
        return self.coro.__await__()

    @property
    def __name__(self):  # a task's repr names the coroutine it runs
        return getattr(self.coro, "__qualname__", None) or type(self.coro).__name__

    def describe(self):
        if self.what is not None:
            return describe_request(*self.what)
        return TASK, self.__name__


def step(timed, go, *args):
    """One step of a task, timed. What a call() inside it reported is not counted twice."""
    global RUNNING
    outer, RUNNING = RUNNING, timed
    reported = WATCH.reported
    began = clock()
    try:
        return go(*args)
    finally:
        RUNNING = outer
        took = clock() - began
        if took > LIMIT:
            took -= WATCH.reported - reported
            if took > LIMIT:
                try:
                    kind, what = timed.describe()
                except Exception:  # noqa: BLE001 - the watchdog never ends a task
                    kind, what = TASK, "?"
                WATCH.report(kind, what, None, began, took)


def where(frame):
    """The loop thread's stack, innermost first, without asyncio's frames or the watchdog's:
    'function (file:line) < caller (file:line) ...'."""
    out = []
    while frame is not None and len(out) < STACK_FRAMES:
        name = os.path.abspath(frame.f_code.co_filename)
        if name != HERE and not name.startswith(ASYNCIO + os.sep):
            shown = (os.path.relpath(name, PACKAGE).replace(os.sep, "/")
                     if name.startswith(PACKAGE + os.sep) else os.path.basename(name))
            out.append("%s (%s:%d)" % (frame.f_code.co_name, shown, frame.f_lineno))
        frame = frame.f_back
    return " < ".join(out) or "the event loop itself (no code of the server)"


class Lines:
    """At most LINES_PER_MINUTE lines a minute of each kind; the rest are counted, and a
    summary line says how many were left out and which of them was the worst. One thread uses
    one Lines."""

    def __init__(self, wall):
        self.wall = wall
        # kind -> [start of its minute, lines logged, lines left out, worst ms, worst's unit]
        self.kinds = {}

    def say(self, kind, unit, ms, message, *args):
        """message % args, unless the minute's lines of this kind are used up: then `unit`
        (what the line was about, as the summary names it) and its ms are counted."""
        now = self.wall()
        entry = self.kinds.get(kind)
        if entry is None or now - entry[0] >= 60.0:
            if entry is not None:
                self.summary(kind, entry)
            entry = self.kinds[kind] = [now, 0, 0, 0.0, None]
        if entry[1] < LINES_PER_MINUTE:
            entry[1] += 1
            log.warning(message, *args)
        else:
            entry[2] += 1
            if entry[4] is None or ms > entry[3]:
                entry[3], entry[4] = ms, unit

    def flush(self):
        """The summaries of the minutes that are over."""
        now = self.wall()
        for kind, entry in list(self.kinds.items()):
            if now - entry[0] >= 60.0:
                self.summary(kind, entry)
                del self.kinds[kind]

    @staticmethod
    def summary(kind, entry):
        if entry[2]:
            log.warning("loop: %d more %s line(s) were left out in that minute; the worst of "
                        "them held the loop %d ms: %s", entry[2], kind, entry[3], entry[4])
            entry[2], entry[4] = 0, None


class Watch:
    """The watchdog of one server (WATCH): configure(), start() on the loop, stop()."""

    def __init__(self):
        self.limit = 0.0  # seconds; 0: off
        self.stack_limit = 0.0
        self.wall = time.monotonic  # the minutes of the rate limit and of /status
        self.loop = None
        self.factory = None  # the loop's task factory before ours
        self.heart = None
        self.thread = None
        self.stopping = threading.Event()
        self.reset()

    def reset(self):
        self.lines = Lines(lambda: self.wall())  # the loop thread's
        self.thread_lines = Lines(lambda: self.wall())  # the watcher thread's
        self.minutes = collections.deque()  # [minute, count, worst ms, kind, what, when] (an hour)
        self.total = 0
        self.reported = 0.0  # seconds of all that was reported: a step leaves out what was in it
        # Seconds of what was reported since the heartbeat went to sleep, from when it was due:
        # how much of its lateness has a line already (check)
        self.explained = 0.0
        self.due = clock()  # when the heartbeat is due
        self.beats = 0
        self.sampled = -1  # self.beats when the stack was last sampled: once per stall
        self.stalls = 0  # stacks sampled so far: the number a line refers to
        self.sampled_at = 0.0

    @property
    def on(self):
        return self.limit > 0

    def configure(self, limit_ms=LIMIT_MS, stack_ms=STACK_MS):
        """The limits in milliseconds (0: off; a stack limit of 0: no stacks)."""
        global LIMIT
        self.limit = max(0.0, float(limit_ms)) / 1000.0
        self.stack_limit = max(0.0, float(stack_ms)) / 1000.0 if self.limit else 0.0
        LIMIT = self.limit if self.limit else float("inf")
        self.reset()

    @property
    def period(self):
        """How often the heartbeat wakes: half the limit, at most every 25 ms."""
        return max(0.005, min(0.025, self.limit / 2))

    def wrap(self, loop=None):
        """Every task created on the loop from here has its steps timed."""
        if not self.on or self.loop is not None:
            return
        self.loop = loop or asyncio.get_running_loop()
        self.factory = self.loop.get_task_factory()
        self.loop.set_task_factory(self.task_factory)

    def start(self, loop=None):
        """The task factory (wrap), the heartbeat and the watcher thread. Nothing when off."""
        if not self.on:
            return
        self.wrap(loop)
        if self.heart is None:
            self.due = clock() + self.period
            self.heart = self.loop.create_task(self.heartbeat())
        if self.stack_limit and self.thread is None:
            self.stopping.clear()
            self.thread = threading.Thread(target=self.watch, args=(threading.get_ident(),),
                                           name="forza-live loop watcher", daemon=True)
            self.thread.start()

    def stop(self):
        """Nothing more is timed or logged (the stop's saves hold the loop on purpose)."""
        global LIMIT
        LIMIT = float("inf")
        if self.loop is not None:
            if self.loop.get_task_factory() == self.task_factory:
                self.loop.set_task_factory(self.factory)
            self.loop = None
        if self.heart is not None:
            self.heart.cancel()
            self.heart = None
        if self.thread is not None:
            self.stopping.set()
            self.thread.join(1.0)
            self.thread = None

    def task_factory(self, loop, coro, **kwargs):
        timed = Timed(coro)
        if self.factory is not None:
            return self.factory(loop, timed, **kwargs)
        return asyncio.Task(timed, loop=loop, **kwargs)

    def call(self, what, fn, *args):
        """fn(*args) timed as a task's work called `what` (the main loop's tick)."""
        began = clock()
        try:
            return fn(*args)
        finally:
            took = clock() - began
            if took > LIMIT:
                self.report(TASK, what, None, began, took)

    def report(self, kind, what, who, began, took):
        """A unit of work held the loop `took` seconds from `began` (clock()), over the
        limit: counted, and a line unless the rate limit took it. Never raises: it runs where
        the work ended, in a datagram's handler or a task's step."""
        if LIMIT == float("inf"):
            return
        try:
            ended = began + took
            self.reported += took
            # what of it came after the heartbeat was due (it is not awake meanwhile: ended
            # is before its next wake)
            self.explained += max(0.0, ended - max(began, self.due))
            ms = took * 1000.0
            self.count(kind, what, ms)
            sampled = self.sampled_at
            where_ = " (stall %d)" % self.stalls if sampled and began <= sampled <= ended else ""
            unit = "%s %s%s" % (kind, what, " from " + who if who else "")
            self.lines.say(kind, unit, ms, "loop: %s held the event loop %d ms (limit %d ms)%s",
                           unit, ms, self.limit * 1000, where_)
        except Exception:  # noqa: BLE001
            log.debug("loop: the watchdog's report failed", exc_info=True)

    def count(self, kind, what, ms):
        now = self.wall()
        minute = int(now // 60)
        if self.minutes and self.minutes[-1][0] == minute:
            entry = self.minutes[-1]
            entry[1] += 1
            if ms > entry[2]:
                entry[2:6] = [ms, kind, what, now]
        else:
            self.minutes.append([minute, 1, ms, kind, what, now])
        self.prune(now)
        self.total += 1

    def prune(self, now):
        """Only the minutes of the last hour are kept."""
        first = int(now // 60) - int(HOUR // 60) + 1
        while self.minutes and self.minutes[0][0] < first:
            self.minutes.popleft()

    async def heartbeat(self):
        """Wakes every self.period: a wake late by more than the limit that the units reported
        meanwhile do not explain is a stall in work nothing timed."""
        period = self.period
        flushed = clock()
        while True:
            self.due = due = clock() + period
            self.explained = 0.0
            await asyncio.sleep(period)
            now = clock()
            self.due = now + period  # the loop is not held now: the watcher thread waits again
            self.beats += 1
            try:
                self.check(due, now)
                if now - flushed >= 1.0:
                    flushed = now
                    self.lines.flush()
            except Exception:  # noqa: BLE001 - the heartbeat goes on
                log.debug("loop: the heartbeat's check failed", exc_info=True)

    def check(self, due, now):
        late = now - due
        if late <= self.limit:
            return
        held = late - self.explained
        if held > self.limit:
            sampled = self.sampled_at
            where_ = " (stall %d)" % self.stalls if sampled and due <= sampled <= now else ""
            self.count(STALL, "untimed", held * 1000.0)
            self.lines.say(STALL, "untimed work, the event loop %d ms late" % (late * 1000.0),
                           held * 1000.0,
                           "loop: the event loop was %d ms late, held by work that no entry "
                           "point times or by many short pieces of work (limit %d ms)%s",
                           held * 1000.0, self.limit * 1000, where_)

    def watch(self, loop_thread):
        """The watcher thread: while the heartbeat is late by the stack limit, the loop
        thread's stack, once per stall."""
        poll = max(0.01, min(0.05, self.stack_limit / 5))
        flushed = time.monotonic()
        while not self.stopping.wait(poll):
            try:
                self.look(loop_thread)
                if time.monotonic() - flushed >= 1.0:
                    flushed = time.monotonic()
                    self.thread_lines.flush()
            except Exception:  # noqa: BLE001 - the thread goes on
                log.debug("loop: the watcher thread's look failed", exc_info=True)

    def look(self, loop_thread):
        beats = self.beats  # before self.due: a heartbeat in between makes it no stall
        held = clock() - self.due
        if held < self.stack_limit or beats == self.sampled:
            return
        frame = sys._current_frames().get(loop_thread)
        self.sampled = beats
        self.stalls += 1
        self.sampled_at = clock()
        try:
            at = where(frame)
            self.thread_lines.say(STACK, "stall %d, at %s" % (self.stalls, at), held * 1000.0,
                                  "loop: stall %d: the event loop has been held %d ms so far, "
                                  "at %s", self.stalls, held * 1000.0, at)
        finally:
            del frame

    def status(self):
        """/status's numbers: the limit, what went over it in the last hour and its worst."""
        if not self.on:
            return {"event_loop": {"limit_ms": 0}}
        now = self.wall()
        self.prune(now)
        recent = self.minutes
        out = {"limit_ms": round(self.limit * 1000),
               "over_limit_last_hour": sum(m[1] for m in recent),
               "over_limit_since_start": self.total}
        if recent:
            worst = max(recent, key=lambda m: m[2])
            out["worst_last_hour"] = shown = {"kind": worst[3], "what": worst[4],
                                              "ms": round(worst[2]),
                                              "seconds_ago": int(now - worst[5])}
            if worst[3] == OPERATOR:  # the public page says no more than that it was one
                del shown["what"]
        return {"event_loop": out}


WATCH = Watch()
