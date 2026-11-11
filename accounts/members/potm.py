"""Photo of the Month (website #4, the first event): every calendar month (UTC), the community
photo with the most likes that count for the contest wins, once the operator confirmed it.

  GET /api/potm/standings[?month=YYYY-MM]   anyone: a month's top entries (default: this month)
  GET /api/potm/winners                     anyone: every confirmed winner, newest first

Both are the same for everybody, with a short public cache; the website's builder copies them
into data/photo-of-the-month.json and the leaderboard page asks for them live. Without
POTM_FROM (FORZA_POTM_FROM, the first contest month) there is no contest and both answer 404.

The rules (the website's photo-of-the-month-rules.html says the same to players):
- An entry is a public community photo (members.items: live, its owner not banned, in the
  gallery feed) that was uploaded during the month, [start, end) in UTC, and that the operator
  did not disqualify for that month (Disqualified).
- A like is a vote when it was given during the month, not by the photo's owner, by a key whose
  likes count in the public totals now (members.likes.counts_of: the link active and pinned, the
  account not suspended, the XUID not banned, a result on the boards), and, when
  POTM_ACCOUNT_DAYS is set (0, no age rule, by default), the account linked to that key now was
  at least that many days old when the like was given.
  Other likes still show on the photo (the public totals) but are not votes. A like taken back
  is gone, and so is its vote.
- The most votes wins. A tie goes to the entry that reached its count first: the time of its
  latest vote (the earliest "reached"); then the earlier upload; then the photo's id.
- The operator verifies the result once the month is over: voids farmed likes, suspends
  accounts, bans keys or disqualifies entries (members.moderation, the admin), then confirms the
  leader (confirm), which keeps a copy of the result (MonthWinner). Until then the month's state
  is "verifying". A confirmed winner stays, whatever happens to the photo or its likes later.

Nothing here names a voter: the public answers carry photo ids, titles, gamertags (the gallery
shows the same) and counts. The operator's review (voters) is in the admin only.
"""

import json
import logging
import re
import threading
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone as dt_timezone
from typing import NamedTuple, Optional

from django.conf import settings
from django.db import IntegrityError, transaction
from django.http import HttpResponse
from django.utils import timezone

from members import game, items
from members.api import api
from members.game import Refused
from members.models import Disqualified, GameLink, Like, MonthWinner

log = logging.getLogger("members")

MONTH_RE = re.compile(r"[0-9]{4}-(0[1-9]|1[0-2])")  # ASCII digits only
CACHE_SECONDS = 60  # Cache-Control of the public answers
NOTE_MAX = 200

_lock = threading.Lock()
_cache = {}  # month -> (monotonic time, Standings)


class PotmError(Exception):
    """An operator's step that cannot be done (the message says why)."""


class Entry(NamedTuple):
    id: str
    owner: str               # XUID hex: never in a public answer
    title: str
    created: float           # unix time of the upload
    votes: int
    reached: Optional[datetime]  # the time of its latest vote: when it reached its count
    likes: int               # every like given to it during the month, votes or not
    not_counted: dict        # why the other likes are not votes: {reason: n}


class Standings(NamedTuple):
    month: str
    start: datetime
    end: datetime
    closed: bool
    entries: list            # [Entry], in the contest's order (the leader first)
    generated: int


# ---- Months (UTC) ----

def parse(month):
    if not isinstance(month, str) or not MONTH_RE.fullmatch(month):
        raise ValueError("a month is YYYY-MM")
    return int(month[:4]), int(month[5:])


def window(month):
    """[start, end) of a month, UTC."""
    y, m = parse(month)
    start = datetime(y, m, 1, tzinfo=dt_timezone.utc)
    end = datetime(y + (m == 12), m % 12 + 1, 1, tzinfo=dt_timezone.utc)
    return start, end


def month_of(when):
    when = when.astimezone(dt_timezone.utc)
    return "%04d-%02d" % (when.year, when.month)


def previous(month):
    y, m = parse(month)
    return "%04d-%02d" % (y - (m == 1), (m - 2) % 12 + 1)


def running():
    return bool(settings.POTM_FROM)


def current(now=None):
    """The month the leaderboard shows: this month, or the first contest month before it starts."""
    this = month_of(now or timezone.now())
    return max(this, settings.POTM_FROM)


def contest_month(month, now=None):
    """True for a month the contest has run in or runs in now (POTM_FROM to this month)."""
    try:
        parse(month)
    except ValueError:
        return False
    return running() and settings.POTM_FROM <= month <= current(now)


# ---- The standings ----

def standings(month, now=None, fresh=False):
    """The month's Standings, counted again when POTM_SECONDS passed (or fresh)."""
    with _lock:
        hit = _cache.get(month)
        if not fresh and hit is not None and time.monotonic() - hit[0] < settings.POTM_SECONDS:
            return hit[1]
    result = count(month, now)
    with _lock:
        _cache[month] = (time.monotonic(), result)
    return result


def forget():
    with _lock:
        _cache.clear()


def entries_of(month, start, end):
    """{photo id: items.Item} of the month's entries."""
    lo, hi = start.timestamp(), end.timestamp()
    out = {i.id: i for i in items.index().items.values()
           if i.kind == items.PHOTO and i.public and lo <= i.created < hi}
    for pid in Disqualified.objects.filter(month=month).values_list("photo", flat=True):
        out.pop(pid, None)
    return out


def voters_of(now=None):
    """{XUID hex: (the account's date_joined, the key counts)} of every key whose likes count in
    public now (members.likes.counts_of's rule), with its linked account's age, lazily per key."""
    links = {xuid: (pub, joined) for xuid, pub, joined in GameLink.objects.filter(
        state=GameLink.ACTIVE, user__is_active=True).values_list("xuid", "pub", "user__date_joined")}
    known = {}
    stamp = None if now is None else now.timestamp()

    def check(xuid):
        if xuid not in known:
            link = links.get(xuid)
            known[xuid] = None if link is None or not game.counted(xuid, link[0], stamp) else link[1]
        return known[xuid]
    return check


NOT_LINKED = "the key's likes do not count in public (new, unlinked, frozen, suspended or banned)"
TOO_NEW = "the account was too new when it liked"
OWN = "the owner's own like"


def count(month, now=None):
    now = now or timezone.now()
    start, end = window(month)
    entries = entries_of(month, start, end)
    min_age = timedelta(days=settings.POTM_ACCOUNT_DAYS)
    joined_of = voters_of(now)
    votes, likes, why = defaultdict(list), Counter(), defaultdict(Counter)
    rows = Like.objects.filter(kind=Like.PHOTO, at__gte=start, at__lt=end).values_list("xuid", "item", "at")
    for xuid, pid, at in rows.iterator(chunk_size=10000):
        entry = entries.get(pid)
        if entry is None:
            continue
        likes[pid] += 1
        if entry.owner == xuid:
            why[pid][OWN] += 1
            continue
        joined = joined_of(xuid)
        if joined is None:
            why[pid][NOT_LINKED] += 1
        elif min_age and at - joined < min_age:
            why[pid][TOO_NEW] += 1
        else:
            votes[pid].append(at)
    ranked = [Entry(pid, i.owner, i.title, i.created, len(votes[pid]), max(votes[pid]) if votes[pid] else None,
                    likes[pid], dict(why[pid])) for pid, i in entries.items()]
    ranked.sort(key=order)
    return Standings(month, start, end, now >= end, ranked, int(now.timestamp()))


def order(e):
    """The contest's order: the most votes, then the earliest to reach its count, then the
    earlier upload, then the id (so the order never depends on how the rows came)."""
    return (-e.votes, e.reached.timestamp() if e.reached else float("inf"), e.created, e.id)


def voters(month, photo, now=None):
    """The operator's view of one entry's likes during the month: each with its account, the
    account's age when it liked, the key's signed sign-ins, and whether it is a vote. For the
    admin only: it names accounts and keys."""
    now = now or timezone.now()
    start, end = window(month)
    entry = entries_of(month, start, end).get(photo)
    owner = entry.owner if entry else None
    min_age = timedelta(days=settings.POTM_ACCOUNT_DAYS)
    joined_of = voters_of(now)
    rows = list(Like.objects.filter(kind=Like.PHOTO, item=photo, at__gte=start, at__lt=end).order_by("at"))
    links = {l.xuid: l for l in GameLink.objects.filter(xuid__in=[r.xuid for r in rows]).select_related("user")}
    month_likes = Counter(Like.objects.filter(kind=Like.PHOTO, at__gte=start, at__lt=end,
                                              xuid__in=[r.xuid for r in rows]).values_list("xuid", flat=True))
    out = []
    for r in rows:
        link = links.get(r.xuid)
        record = game.player(int(r.xuid, 16)) if game.available() else {}
        first = record.get("firstSigned")
        joined = joined_of(r.xuid)
        if r.xuid == owner:
            reason = OWN
        elif joined is None:
            reason = NOT_LINKED
        elif min_age and r.at - joined < min_age:
            reason = TOO_NEW
        else:
            reason = ""
        out.append({
            "at": r.at, "key": r.xuid,
            "account": link.user.public_id if link else None,
            "account_created": link.user.date_joined if link else None,
            "account_days": (r.at - link.user.date_joined).days if link else None,
            "linked_at": link.linked_at if link else None,
            "first_signed": datetime.fromtimestamp(first, dt_timezone.utc)
            if isinstance(first, int) and not isinstance(first, bool) else None,
            "signed_days": record.get("signedDays"),
            "likes_this_month": month_likes[r.xuid],
            "vote": not reason, "why": reason,
        })
    return out


# ---- The operator's steps ----

def confirm(month, photo, by, expect_votes=None, note="", now=None):
    """Confirms the month's leader as its Photo of the Month (MonthWinner), PotmError if not:
    the month must be over, have no winner yet, and `photo` must lead it with at least one vote
    (with expect_votes, exactly that many: the standings the operator reviewed)."""
    now = now or timezone.now()
    if not contest_month(month, now):
        raise PotmError("%s is not a contest month" % month)
    if now < window(month)[1]:
        raise PotmError("%s is not over yet" % month)
    if MonthWinner.objects.filter(month=month).exists():
        raise PotmError("%s already has a winner: withdraw it first" % month)
    s = standings(month, now, fresh=True)
    if not s.entries or s.entries[0].votes == 0:
        raise PotmError("no entry of %s has a vote" % month)
    lead = s.entries[0]
    if lead.id != photo:
        raise PotmError("%s does not lead %s: %s does" % (photo, month, lead.id))
    if expect_votes is not None and lead.votes != expect_votes:
        raise PotmError("the standings changed (%d votes, not %d): review them again" % (lead.votes, expect_votes))
    gamertag = game.gamertag(int(lead.owner, 16)) or ""
    try:
        with transaction.atomic():
            winner = MonthWinner.objects.create(
                month=month, photo=lead.id, votes=lead.votes, reached=lead.reached, title=lead.title[:40],
                gamertag=gamertag[:15], owner=lead.owner, confirmed_at=now, confirmed_by=by[:150],
                note=clean_note(note))
    except IntegrityError:
        raise PotmError("%s already has a winner" % month) from None
    forget()
    log.info("accounts: %s confirmed photo %s as the Photo of the Month %s (%d votes)", by, lead.id, month, lead.votes)
    return winner


def withdraw(month, by):
    """Removes a month's confirmed winner (a mistake, or a disqualification found later)."""
    deleted = MonthWinner.objects.filter(month=month).delete()[0]
    if not deleted:
        raise PotmError("%s has no confirmed winner" % month)
    forget()
    log.info("accounts: %s withdrew the Photo of the Month %s", by, month)


def disqualify(month, photo, note, by, now=None):
    """Takes a photo out of a month's contest; it stays in the gallery and keeps its likes."""
    now = now or timezone.now()
    if not contest_month(month, now):
        raise PotmError("%s is not a contest month" % month)
    if not re.fullmatch(items.ITEM_ID, photo or ""):
        raise PotmError("a photo id is 12 letters and digits")
    note = clean_note(note)
    if not note:
        raise PotmError("say why, for the record")
    if MonthWinner.objects.filter(month=month, photo=photo).exists():
        raise PotmError("%s is %s's confirmed winner: withdraw it first" % (photo, month))
    try:
        with transaction.atomic():
            Disqualified.objects.create(month=month, photo=photo, note=note, at=now, by=by[:150])
    except IntegrityError:
        raise PotmError("%s is already out of %s" % (photo, month)) from None
    forget()
    log.info("accounts: %s disqualified photo %s from the Photo of the Month %s", by, photo, month)


def requalify(month, photo, by):
    if not Disqualified.objects.filter(month=month, photo=photo).delete()[0]:
        raise PotmError("%s is not disqualified from %s" % (photo, month))
    forget()
    log.info("accounts: %s put photo %s back in the Photo of the Month %s", by, photo, month)


def clean_note(note):
    text = "".join(c if c.isprintable() else " " for c in (note or ""))
    return " ".join(text.split())[:NOTE_MAX]


# ---- The public answers ----

def state_of(s, winner, now=None):
    now = now or timezone.now()
    if now < s.start:
        return "upcoming"
    if not s.closed:
        return "open"
    if winner is not None:
        return "confirmed"
    return "verifying" if s.entries and s.entries[0].votes else "no winner"


def winner_doc(w):
    return {"month": w.month, "id": w.photo, "title": w.title, "gamertag": w.gamertag, "votes": w.votes,
            "reached": int(w.reached.timestamp()), "confirmed": int(w.confirmed_at.timestamp())}


def standings_doc(month, now=None):
    from members import likes  # registers its routes on import; the public totals
    s = standings(month, now)
    winner = MonthWinner.objects.filter(month=month).first()
    totals = likes.snapshot(items.PHOTO)["counts"]
    top = []
    for rank, e in enumerate(s.entries[:settings.POTM_TOP], 1):
        if not e.votes:
            break  # entries without a vote are not listed
        top.append({"rank": rank, "id": e.id, "title": e.title, "gamertag": game.gamertag(int(e.owner, 16)) or "",
                    "votes": e.votes, "likes": totals.get(e.id, 0), "reached": int(e.reached.timestamp())})
    return {"version": 1, "month": s.month, "start": int(s.start.timestamp()), "end": int(s.end.timestamp()),
            "state": state_of(s, winner, now), "accountDays": settings.POTM_ACCOUNT_DAYS, "generated": s.generated,
            "entries": len(s.entries), "top": top, "winner": winner_doc(winner) if winner else None}


def public(doc):
    response = HttpResponse(json.dumps(doc, separators=(",", ":")), content_type="application/json")
    response["Cache-Control"] = "public, max-age=%d" % CACHE_SECONDS  # kept by ApiHeaders
    return response


@api.get("/potm/standings", auth=None)
def potm_standings(request, month: Optional[str] = None):
    if not running():
        raise Refused(404, "not running")
    if not game.available():
        raise Refused(503, "not available")
    month = month or current()
    if not contest_month(month):
        raise Refused(404, "no such month")
    return public(standings_doc(month))


@api.get("/potm/winners", auth=None)
def potm_winners(request):
    if not running():
        raise Refused(404, "not running")
    return public({"version": 1, "generated": int(time.time()), "from": settings.POTM_FROM,
                   "winners": [winner_doc(w) for w in MonthWinner.objects.order_by("-month")]})
