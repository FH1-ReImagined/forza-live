"""Forza Live: Message Center notifications (Xls.WebServices.Notifications).

The game polls RetrieveForUser every nextRetrieveSeconds once its notification session has
started (StartNewSession) and shows what it gets in the Message Center. Each notification
is a record {id, xuidRecipient, notificationType, hasThumbnail, isRead, deleteAction,
expirationDate, cbParameters}; its parameters follow in the reply's parameterBlock, one
after the other, in the same xtsw encoding as service records (cbParameters bytes each).

The type picks the record its parameters decode to and the Message Center entry the game
makes of it (the dispatch in sub_82ABD248; the parameter records' member lists are filled
by static initializers, docs/title_services.md). The high byte is the category (bit 15
set for those that carry something to collect):

    0x0100 news message        0x0101 new DLC            0x0201 banned
    0x8300 auction outbid      0x8301 auction sold       0x8302 auction won
    0x8303 auction failed      0x8304 gift: credits      0x8305 gift: item
    0x8306 gift: car           0x8307 storefront out of stock
    0x8308 storefront money    0x8402 Rivals recommendation
    0x8403 Rivals: your time was beaten
    0x0500 club: request to join (to leaders)    0x0501 club: invitation
    0x0502 club: kick recommendation             0x0503 club: kicked
    0x0504 club: demotion      0x0505 club: membership accepted   0x0506 club: member quit
    0x0507 club: new member    0x0508 club: promotion             0x8501 club: shared car

DATA_DIR/notifications.json keeps them per recipient until the game clears them
(Notifications.Clear) or they expire (KEEP_DAYS after they were sent: the expirationDate the
game is given, after which RetrieveForUser no longer returns them; they leave memory and the
file then, Store.prune and dump). A broadcast (broadcast(), the operator's /broadcast) is
stored once and shown to every player until it expires, players who sign in later included;
each player's read and cleared broadcasts are kept apart (broadcastState).

The file is megabytes on a busy server (11 MB on the public one), and was written whole on the
event loop at every notification sent, read or cleared: a quarter of a second each time, one
per member for a club's news. Now a change is a line in DATA_DIR/notifications.journal,
flushed to disk before the caller is answered (Store.save), and the file is written whole by a
thread every FULL_EVERY seconds while the journal has changes it lacks (keep_written), and once
more as the server stops (Store.stop), as the boards are (core/journal.py, docs/operating.md
"Notifications on disk"). The file is the JSON it always was, so a previous version reads it.
"""

import asyncio
import json
import logging
import os
import threading
import time

from forza_live.network import xtsw as x
from forza_live.core import journal
from forza_live.network.xtsw import BOOL, F32, I32, U8, U16, U32, U64, S, SYSTIME, WSTR

log = logging.getLogger("forza-live")

DATA_DIR = "forza_live_data"
POLL_SECONDS = 60       # nextRetrieveSeconds (0 made the game poll 7 times a second)
FIRST_POLL_SECONDS = 15  # Logon2's notificationPollIntervalSeconds: the first poll
KEEP_DAYS = 30          # expirationDate of a new notification
UNIX_TO_FILETIME = 116444736000000000

# The Message Center's tabs (Logon2's notificationCategories, at most 5): a notification's
# category is its type's high byte (without bit 15). The game needs a tab for the category
# of every notification it shows (without one, opening the Message Center crashed).
CATEGORIES = [
    {"type": 1, "name": "News", "description": "News from the Horizon Festival."},
    {"type": 2, "name": "Account", "description": "Your account."},
    {"type": 3, "name": "Gifts & Auctions", "description": "Gifts, auctions and storefront."},
    {"type": 4, "name": "Rivals", "description": "Your Rivals."},
    {"type": 5, "name": "Car Club", "description": "Your car club."},
]

Message = S(("Message", WSTR(513)))
ClubNames = (("clanName", WSTR(21)), ("clanTag", WSTR(5)))
AuctionCar = (("AuctionId", I32), ("CarId", I32))

# type: (name, parameter record)
TYPES = {
    0x0000: ("permissions changed", S(("Flags", U64))),
    0x8000: ("badge granted", S(("BadgeId", I32))),
    0x8001: ("set credits", S(("creditAmount", U32), ("action", I32))),
    0x0100: ("news", Message),
    0x0101: ("new DLC", S(("Message", WSTR(513)), ("OfferId", U64))),
    0x0102: ("message", Message),
    0x0200: ("message", Message),
    0x0201: ("banned", S(("BanExpireTime", SYSTIME), ("BannedFromFeatureAreas", U64))),
    0x0202: ("unbanned", S(("BannedFromFeatureAreas", U64))),
    0x0300: ("message", Message),
    0x8300: ("auction outbid", S(*AuctionCar)),
    0x8301: ("auction sold", S(*AuctionCar, ("SalePrice", U32))),
    0x8302: ("auction won", S(*AuctionCar)),
    0x8303: ("auction failed", S(*AuctionCar)),
    0x8304: ("gift: credits", S(("GiftId", I32), ("Amount", I32),
                                ("UserSuppliedReason", WSTR(513)), ("SenderXuid", U64),
                                ("SenderGamerTag", WSTR(16)))),
    0x8305: ("gift: item", S(("GiftId", I32), ("ItemId", I32), ("GiftFileType", I32),
                             ("SenderXuid", U64), ("SenderGamerTag", WSTR(16)),
                             ("Reason", I32))),
    0x8306: ("gift: car", S(("GiftId", I32), ("MakeId", I32), ("ModelId", I32),
                            ("SenderXuid", U64), ("SenderGamerTag", WSTR(16)),
                            ("Reason", I32))),
    0x8307: ("storefront: out of stock", S(("StorefrontFileId", I32),
                                           ("StorefrontFileType", I32),
                                           ("StorefrontFileTitle", WSTR(17)))),
    0x8308: ("storefront: money", S(("Amount", U32))),
    0x8309: ("profile import", S(("awardAmount", U32))),
    0x8401: ("message", Message),
    0x8402: ("Rivals recommendation", S(("TimeTrialId", I32),
                                        ("localizedTimeTrialName", WSTR(129)),
                                        ("trackName", WSTR(65)),
                                        ("description", WSTR(513)), ("category", I32))),
    0x8403: ("Rivals: time beaten", S(("senderXuid", U64), ("senderGamertag", WSTR(16)),
                                      ("senderClanTag", WSTR(5)), ("senderClanRole", I32),
                                      ("senderFlags", U8), ("sentTime", SYSTIME),
                                      ("timeTrialId", I32), ("carId", U16),
                                      ("localizedTimeTrialName", WSTR(129)),
                                      ("trackName", WSTR(65)), ("trackId", U16),
                                      ("score", F32), ("scoreType", I32), ("category", I32))),
    0x0500: ("club: join request", S(("requestorXuid", U64), ("requestorGamertag", WSTR(16)),
                                     ("clanId", I32))),
    0x0501: ("club: invitation", S(("sponsorXuid", U64), ("clanId", I32), *ClubNames,
                                   ("sponsorGamertag", WSTR(16)))),
    0x0502: ("club: kick recommendation", S(
        ("recommendingRemovalXuid", U64), ("recommendingRemovalGamertag", WSTR(16)),
        ("recommendedForRemovalXuid", U64), ("recommendedForRemovalGamertag", WSTR(16)),
        ("clanId", I32))),
    0x0503: ("club: kicked", S(("kickedXuid", U64), ("kickedGamertag", WSTR(16)), *ClubNames)),
    0x0504: ("club: demotion", S(*ClubNames)),
    0x0505: ("club: membership accepted", S(*ClubNames)),
    0x0506: ("club: member quit", S(("exMemberXuid", U64), ("exMemberGamertag", WSTR(16)),
                                    *ClubNames)),
    0x0507: ("club: new member", S(("newMemberXuid", U64), ("newMemberGamertag", WSTR(16)),
                                   *ClubNames)),
    0x0508: ("club: promotion", S(("newRole", I32), ("newPromotedXuid", U64),
                                  ("newPromotedGamertag", WSTR(16)), *ClubNames)),
    0x8501: ("club: shared car", S(("sharedCarAction", I32), ("playerXuid", U64),
                                   ("playerGamertag", WSTR(16)), ("MakeName", WSTR(31)),
                                   ("ModelName", WSTR(81)), ("perfIndex", F32),
                                   ("Year", U16))),
}
NEWS, DLC, BANNED = 0x0100, 0x0101, 0x0201
AUCTION_OUTBID, AUCTION_SOLD, AUCTION_WON, AUCTION_FAILED = 0x8300, 0x8301, 0x8302, 0x8303
GIFT_CREDITS, GIFT_ITEM, GIFT_CAR = 0x8304, 0x8305, 0x8306
RIVALS_RECOMMENDATION, RIVALS_BEATEN = 0x8402, 0x8403
CLUB_REQUEST, CLUB_INVITE, CLUB_KICK_RECOMMENDATION, CLUB_KICKED, CLUB_DEMOTION, \
    CLUB_ACCEPTED, CLUB_MEMBER_QUIT, CLUB_NEW_MEMBER, CLUB_PROMOTION = range(0x500, 0x509)
CLUB_SHARED_CAR = 0x8501


FULL_EVERY = 60.0  # seconds between two writes of the whole of notifications.json (keep_written)


class Store:
    """The notifications, in memory as notifications.json has them: {"nextId", "byRecipient":
    {xuid hex: [notification, ...]}, "receipts": {idempotency key: id}, "broadcasts":
    [notification, ...], "broadcastState": {xuid hex: {"read": [id], "cleared": [id]}}}.

    A change is a line of notifications.journal, flushed to disk before the caller is answered
    (save); the file is written whole by a thread (keep_written) and at the stop (stop), as the
    boards are (core/journal.py). What lets a thread write it while the loop goes on: nothing a
    snapshot holds is changed in place. A notification is never changed (read: a new record in
    its place), nor a recipient's list, the broadcasts' list or a player's broadcast state (a
    change makes a new one); snapshot copies only the three maps that hold them."""

    def __init__(self):
        if getattr(self, "journal", None) is not None:
            self.journal.close()  # configured again: the journal of the folder before
        self.data = {"nextId": 1, "byRecipient": {}}
        self.loaded = False
        self.lock = threading.Lock()
        self.journal = None    # notifications.journal (load)
        self.changes = {}      # since the last save(), as the journal's line will have them

    def path(self):
        return os.path.join(DATA_DIR, "notifications.json")

    def load(self):
        """The notifications from notifications.json and its journal, once, without the expired
        ones (prune). journal.Unreadable when the file is there but cannot be read, nor its
        .bak, or is damaged and its .bak is there but cannot be read: lsp.configure loads the
        store at the start, which then stops, as for the boards, rather than run without what
        the file holds and write over it. A load that raised is made again at the next call."""
        if self.loaded:
            return self.data
        beside = journal.Journal(self.path())
        found, changes = beside.load()
        self.data = {"nextId": 1, "byRecipient": {}}
        if isinstance(found, dict):
            self.data.update(found)
        elif found is not None:
            log.error("%s holds no notifications (%s): starting without it", self.path(),
                      type(found).__name__)
        self.journal = beside
        skipped = 0
        for change in changes:
            try:
                self.apply(change)
            except (AttributeError, KeyError, TypeError, ValueError):
                skipped += 1
        if changes:
            log.log(logging.WARNING if skipped else logging.INFO,
                    "notifications: %d changes from %s on top of %s%s", len(changes) - skipped,
                    os.path.basename(beside.name), os.path.basename(self.path()),
                    ", %d that cannot be read skipped" % skipped if skipped else "")
        gone = self.prune()
        if gone:
            log.info("notifications: %d expired ones left out (the game is shown none of them); "
                     "%s loses them at its next write", gone, os.path.basename(self.path()))
        self.loaded = True
        return self.data

    def apply(self, change):
        """A line of the journal on data, in this order: {"add": {xuid hex: [notification]},
        "read": {xuid hex: [id]}, "clear": {xuid hex: [id]}, "nextId": n, "receipts": {key: id},
        "broadcasts": [every broadcast kept], "broadcastState": {xuid hex: state, or null for one
        that went}}. Applied twice, or on a file that already has it, it changes nothing."""
        d, by = self.data, self.data["byRecipient"]
        for who, added in change.get("add", {}).items():
            mine = by.get(who, [])
            have = {n["id"] for n in mine}
            new = [n for n in added if n["id"] not in have]
            if new:
                by[who] = mine + new
        for who, ids in change.get("read", {}).items():
            self._read(who, set(ids))
        for who, ids in change.get("clear", {}).items():
            self._clear(who, set(ids))
        if "nextId" in change:
            d["nextId"] = max(d["nextId"], int(change["nextId"]))
        if change.get("receipts"):
            d.setdefault("receipts", {}).update(change["receipts"])
        if "broadcasts" in change:
            d["broadcasts"] = list(change["broadcasts"])
        for who, seen in change.get("broadcastState", {}).items():
            state = d.setdefault("broadcastState", {})
            if seen is None:
                state.pop(who, None)
            else:
                state[who] = seen

    def _read(self, who, ids):
        mine = self.data["byRecipient"].get(who, [])
        if any(n["id"] in ids and not n["read"] for n in mine):
            self.data["byRecipient"][who] = [dict(n, read=True) if n["id"] in ids else n
                                             for n in mine]

    def _clear(self, who, ids):
        by = self.data["byRecipient"]
        kept = [n for n in by.get(who, []) if n["id"] not in ids]
        if kept:
            by[who] = kept
        else:
            by.pop(who, None)

    def prune(self, now=None):
        """The expired notifications out of memory; how many. RetrieveForUser shows none of them
        (pending), so nothing anybody can see changes, and no line of the journal is needed: a
        start drops them again. Read or not, a notification is kept until it expires: the game
        lists what each poll returns, and a read gift is still collected from the Message
        Center."""
        now = time.time() if now is None else now
        by, gone = self.data["byRecipient"], 0
        for who, mine in list(by.items()):
            live = [n for n in mine if n["expires"] > now]
            if len(live) != len(mine):
                gone += len(mine) - len(live)
                if live:
                    by[who] = live
                else:
                    del by[who]
        return gone

    def add(self, recipient, ntype, params, keep_days, now):
        """A new notification in memory and in the changes for save() (the lock is held): its
        id."""
        d, who = self.data, key(recipient)
        nid = d["nextId"]
        d["nextId"] += 1
        n = {"id": nid, "type": ntype, "params": params, "sent": now,
             "expires": now + keep_days * 86400, "read": False}
        # The recipient's expired ones go now: their list is made anew anyway.
        d["byRecipient"][who] = [m for m in d["byRecipient"].get(who, [])
                                 if m["expires"] > now] + [n]
        self.changes.setdefault("add", {}).setdefault(who, []).append(n)
        self.changes["nextId"] = d["nextId"]
        return nid

    def save(self):
        """The changes since the last save, on disk when this returns: one line appended to the
        journal and flushed, milliseconds whatever the file's size. OSError when they could not
        be written: they stay in memory and go with the next save."""
        if not self.changes:
            return
        os.makedirs(DATA_DIR, exist_ok=True)
        self.journal.append(self.changes)
        self.changes = {}

    def snapshot(self):
        """What a write of the whole file needs, taken on the event loop: the journal, the place
        in it, and a copy of the maps that change in place (byRecipient, receipts,
        broadcastState). What they hold is never changed (the class's docstring)."""
        with self.lock:
            self.load()
            d = self.data
            tables = dict(d)  # keys this version does not know stay as they are
            tables["byRecipient"] = dict(d["byRecipient"])
            for name in ("receipts", "broadcastState"):
                if name in d:
                    tables[name] = dict(d[name])
            return self.journal, self.journal.mark(), tables

    def write(self, snapshot=None, stopping=False):
        """notifications.json written whole and flushed, its journal cut back to what the file
        before it lacked (journal.checkpoint). A thread's work (keep_written): on an event loop
        it runs only as the server stops. Without a snapshot it takes one, which is right only
        where nothing changes the store meanwhile (the stop, a tool, a test)."""
        if not stopping:
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass
            else:
                raise RuntimeError("Store.write() writes the whole of notifications.json: run "
                                   "it in a thread (asyncio.to_thread), never on the event loop")
        beside, mark, tables = snapshot or self.snapshot()
        os.makedirs(os.path.dirname(beside.path) or ".", exist_ok=True)
        now = time.time()
        return beside.checkpoint(mark, lambda f: dump(f, tables, now))

    def stop(self):
        """The server is stopping: notifications.json gets what only its journal had, so the
        file alone is complete (all a previous version of the server reads). The log says which
        it was, in one line: written, nothing to write, or not written (the journal then has
        what the file lacks, unless it could not take the last changes either)."""
        if not self.loaded:
            return
        name = os.path.basename(self.path())
        try:
            self.save()
            unsaved = None
        except OSError as e:
            unsaved = e  # still in memory: the whole file takes them, if it can be written
        if unsaved is None and not self.journal.behind():
            if self.journal.base not in (journal.NO_FILE, journal.UNREADABLE):
                log.info("notifications: nothing to write at the stop: %s has every change: %s",
                         name, self.held())
            else:
                log.info("notifications: %s, and no journal to apply: nothing was read, nothing "
                         "to write at the stop", ("no %s in %s" if self.journal.base ==
                                                  journal.NO_FILE else "%s in %s could not be "
                                                  "read") % (name, os.path.abspath(DATA_DIR)))
            return
        began = time.monotonic()
        try:
            written = self.write(stopping=True)
        except OSError as e:
            if unsaved is None:
                log.warning("notifications: %s not written at the stop (%s); its journal has "
                            "every change", self.path(), e)
            else:
                log.warning("notifications: %s not written at the stop (%s), and the last "
                            "changes could not be added to its journal either (%s): those are "
                            "lost, the journal keeps the changes before them", self.path(), e,
                            unsaved)
            return
        if written:
            log.info("notifications: %s written whole at the stop in %.2f s: it has every "
                     "change: %s", name, time.monotonic() - began, self.held())

    def held(self):
        """What the store holds, for the log: "120 notifications for 40 players, 1 broadcast"."""
        by = self.data["byRecipient"]
        count = sum(len(mine) for mine in by.values())
        casts = len(self.data.get("broadcasts", []))
        return "%d notification%s for %d player%s, %d broadcast%s" % (
            count, "" if count == 1 else "s", len(by), "" if len(by) == 1 else "s",
            casts, "" if casts == 1 else "s")


STORE = Store()


def dump(f, tables, now):
    """A snapshot's tables as notifications.json, to the text file f: plain JSON as every
    version reads it, a notification to a line, without the ones expired at `now`. One call of
    the C encoder per notification: json.dump with an indent is the pure-Python encoder, and
    one call for the whole file would hold the interpreter (and the event loop with it, thread
    or not) for as long as it ran."""
    def lines(items, pad):
        return ",\n".join(pad + item for item in items)

    f.write("{")
    for n, (name, value) in enumerate(tables.items()):
        f.write("%s\n %s: " % ("," if n else "", json.dumps(name)))
        if name == "byRecipient":
            m = 0
            f.write("{")
            for who, mine in value.items():
                live = [json.dumps(x) for x in mine if x["expires"] > now]
                if live:
                    f.write("%s\n  %s: [\n%s\n  ]" % ("," if m else "", json.dumps(who),
                                                       lines(live, "   ")))
                    m += 1
            f.write("\n }" if m else "}")
        elif isinstance(value, dict) and value:
            f.write("{\n%s\n }" % lines(("%s: %s" % (json.dumps(k), json.dumps(v))
                                         for k, v in value.items()), "  "))
        elif isinstance(value, list) and value:
            f.write("[\n%s\n ]" % lines((json.dumps(v) for v in value), "  "))
        else:
            f.write(json.dumps(value))
    f.write("\n}\n")


async def keep_written(every=None):
    """The server's task: every FULL_EVERY seconds, when the journal has changes that
    notifications.json lacks, the file is written whole by a thread from a snapshot taken here.
    A write that fails is logged and made again at the next round; nothing is lost meanwhile,
    the journal has every change."""
    while True:
        await asyncio.sleep(FULL_EVERY if every is None else every)
        store = STORE
        if not store.loaded or not store.journal.behind():
            continue
        began = time.monotonic()
        try:
            snapshot = store.snapshot()
            written = await asyncio.to_thread(store.write, snapshot)
        except Exception:
            log.exception("notifications: writing %s failed; the journal keeps every change",
                          store.path())
            continue
        if written:
            log.info("notifications: %s written whole in %.2f s, off the event loop",
                     os.path.basename(store.path()), time.monotonic() - began)


def configure(data_dir):
    global DATA_DIR
    DATA_DIR = data_dir
    STORE.__init__()


def key(xuid):
    return "%016X" % xuid


def logged(nid, recipient, ntype, params):
    log.info("title: notification %d to %016X: %s %s", nid, recipient, TYPES[ntype][0],
             {k: v for k, v in params.items() if not k.endswith("Xuid")})


def send(recipient, ntype, params, keep_days=KEEP_DAYS, idempotency_key=None):
    """Queues a notification of type ntype (TYPES) for recipient; returns its id once it is on
    disk (a line of the journal). OSError when it could not be written: it is kept in memory
    and goes with the next save, and a retry with the same idempotency_key writes it then."""
    if ntype not in TYPES:
        raise ValueError("unknown notification type %#x" % ntype)
    x.encode(TYPES[ntype][1], params)  # fails now rather than in a reply
    with STORE.lock:
        d = STORE.load()
        # Keep receipts even after the notification is cleared or expires.
        receipts = d.setdefault("receipts", {})
        if idempotency_key is not None and idempotency_key in receipts:
            STORE.save()  # retry a previous failed persistence before acknowledging it
            return receipts[idempotency_key]
        nid = STORE.add(recipient, ntype, params, keep_days, time.time())
        if idempotency_key is not None:
            receipts[idempotency_key] = nid
            STORE.changes.setdefault("receipts", {})[idempotency_key] = nid
        STORE.save()
    logged(nid, recipient, ntype, params)
    return nid


def send_many(recipients, ntype, params, keep_days=KEEP_DAYS):
    """The same notification for each of recipients (a club's members): their ids, all on disk
    in one line of the journal."""
    if ntype not in TYPES:
        raise ValueError("unknown notification type %#x" % ntype)
    x.encode(TYPES[ntype][1], params)
    now = time.time()
    with STORE.lock:
        STORE.load()
        ids = [STORE.add(r, ntype, params, keep_days, now) for r in recipients]
        STORE.save()
    for nid, recipient in zip(ids, recipients):
        logged(nid, recipient, ntype, params)
    return ids


def set_state(who, seen):
    """A player's broadcast state ({"read", "cleared"}), or None for none: in memory and in
    the changes (the lock is held)."""
    state = STORE.data.setdefault("broadcastState", {})
    if seen is None:
        state.pop(who, None)
    else:
        state[who] = seen
    STORE.changes.setdefault("broadcastState", {})[who] = seen


def keep_broadcasts(live):
    """The broadcasts kept from now (the lock is held), each player's state without the ids
    that are not among them."""
    STORE.data["broadcasts"] = live
    STORE.changes["broadcasts"] = live
    ids = {b["id"] for b in live}
    for who, seen in list(STORE.data.get("broadcastState", {}).items()):
        kept = {k: [i for i in seen.get(k, []) if i in ids] for k in ("read", "cleared")}
        if any(kept[k] != seen.get(k, []) for k in kept):
            set_state(who, kept if kept["read"] or kept["cleared"] else None)


def broadcast(ntype, params, keep_days=KEEP_DAYS):
    """Queues a notification for every player (stored once); returns its id."""
    if ntype not in TYPES:
        raise ValueError("unknown notification type %#x" % ntype)
    x.encode(TYPES[ntype][1], params)  # fails now rather than in a reply
    now = time.time()
    with STORE.lock:
        d = STORE.load()
        nid = d["nextId"]
        d["nextId"] += 1
        STORE.changes["nextId"] = d["nextId"]
        keep_broadcasts([b for b in d.get("broadcasts", []) if b["expires"] > now] + [
            {"id": nid, "type": ntype, "params": params, "sent": now,
             "expires": now + keep_days * 86400}])  # forget expired broadcasts
        STORE.save()
    log.info("title: broadcast %d: %s %s", nid, TYPES[ntype][0], params)
    return nid


def broadcasts():
    """The broadcasts players can still get, oldest first (copies)."""
    now = time.time()
    with STORE.lock:
        d = STORE.load()
        return [dict(b) for b in d.get("broadcasts", []) if b["expires"] > now]


def delete_broadcast(nid):
    """Takes a broadcast back: it leaves every Message Center at the next poll. True when there
    was one with that id."""
    with STORE.lock:
        d = STORE.load()
        live = d.get("broadcasts", [])
        kept = [b for b in live if b["id"] != nid]
        if len(kept) == len(live):
            return False
        keep_broadcasts(kept)
        STORE.save()
    log.info("title: broadcast %d deleted by the operator", nid)
    return True


def pending(recipient):
    now = time.time()
    d = STORE.load()
    mine = [n for n in d["byRecipient"].get(key(recipient), []) if n["expires"] > now]
    seen = d.get("broadcastState", {}).get(key(recipient), {})
    cleared, read = set(seen.get("cleared", [])), set(seen.get("read", []))
    return mine + [dict(b, read=b["id"] in read) for b in d.get("broadcasts", [])
                   if b["expires"] > now and b["id"] not in cleared]


def retrieve(xuid, body):
    p, _ = x.decode(x.NotificationsRetrieveForUser_Params, body)
    results, block = [], bytearray()
    with STORE.lock:
        for n in pending(xuid)[:p["maxResults"]]:
            params = x.encode(TYPES[n["type"]][1], n["params"])
            if len(block) + len(params) > p["cbParameterBlockSize"]:
                break
            block += params
            results.append({
                "id": n["id"], "xuidRecipient": xuid,
                "notificationType": n["type"] - 0x10000 if n["type"] & 0x8000 else n["type"],
                "isRead": n["read"],
                "expirationDate": int(n["expires"] * 10 ** 7) + UNIX_TO_FILETIME,
                "cbParameters": len(params)})
    if results:
        log.info("title: notifications for %016X: %s", xuid,
                 ", ".join("%d %#x" % (r["id"], r["notificationType"] & 0xFFFF) for r in results))
    return x.reply(x.NotificationsRetrieveForUser_Results, {
        "results": results, "parameterBlock": bytes(block), "nextRetrieveSeconds": POLL_SECONDS})


def update(xuid, body, clear):
    p, _ = x.decode(x.NotificationsId_Params, body)
    nid, who = p["notificationId"], key(xuid)
    with STORE.lock:
        d = STORE.load()
        mine = next((n for n in d["byRecipient"].get(who, []) if n["id"] == nid), None)
        if mine is not None:
            if clear:
                STORE._clear(who, {nid})
                STORE.changes.setdefault("clear", {}).setdefault(who, []).append(nid)
            elif not mine["read"]:
                STORE._read(who, {nid})
                STORE.changes.setdefault("read", {}).setdefault(who, []).append(nid)
            STORE.save()
        elif any(b["id"] == nid for b in d.get("broadcasts", [])):
            seen = d.get("broadcastState", {}).get(who, {})
            k = "cleared" if clear else "read"
            if nid not in seen.get(k, []):
                new = {"read": list(seen.get("read", [])),
                       "cleared": list(seen.get("cleared", []))}
                new[k].append(nid)
                set_state(who, new)
                STORE.save()
    log.info("title: notification %d %s by %016X", nid, "cleared" if clear else "read", xuid)
    return x.reply()


def services():
    return {
        # A new session (the game starts one when a race loads) shows nothing until its
        # first poll: make that one come soon.
        ("Xls.Notifications", "StartNewSession"):
            lambda xuid, body: x.reply(x.NotificationsStartNewSession_Results,
                                       {"nonce": 1, "nextRetrieveSeconds": FIRST_POLL_SECONDS}),
        ("Xls.Notifications", "RetrieveForUser"): retrieve,
        ("Xls.Notifications", "MarkRead"): lambda xuid, body: update(xuid, body, False),
        ("Xls.Notifications", "Clear"): lambda xuid, body: update(xuid, body, True),
    }


# Samples for --test-notifications (from the sender, to the recipient). Not the gifts,
# which need a gift on the server (market.gift_credits, gift_stock_car), nor the
# auction ones: the Message Center lists those without a title or text.
def samples(sender, sender_tag):
    club = {"clanName": "Horizon Kings", "clanTag": "HZN"}
    return {
        NEWS: {"Message": "Welcome back to the Horizon Festival!"},
        DLC: {"Message": "New cars are in the Autoshow.", "OfferId": 0},
        RIVALS_RECOMMENDATION: {"TimeTrialId": 1001, "localizedTimeTrialName": "Clear Springs",
                                "trackName": "Clear Springs Circuit",
                                "description": "Try this one.", "category": 0},
        RIVALS_BEATEN: {"senderXuid": sender, "senderGamertag": sender_tag,
                        "sentTime": time.time(), "timeTrialId": 199, "carId": 1398,
                        "localizedTimeTrialName": "GoPro Millennia Masters",
                        "trackName": "Millennia", "trackId": 0, "score": 93.294,
                        "scoreType": 3, "category": 0},
        CLUB_REQUEST: {"requestorXuid": sender, "requestorGamertag": sender_tag, "clanId": 1},
        CLUB_INVITE: dict(club, sponsorXuid=sender, sponsorGamertag=sender_tag, clanId=1),
        CLUB_ACCEPTED: club,
        CLUB_NEW_MEMBER: dict(club, newMemberXuid=sender, newMemberGamertag=sender_tag),
        CLUB_PROMOTION: dict(club, newRole=2, newPromotedXuid=sender,
                             newPromotedGamertag=sender_tag),
    }
