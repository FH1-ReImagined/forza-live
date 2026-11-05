"""Daily and weekly challenges (forza-live #1): objectives the server sets, the same for every
player, and each player's progress, from results it already takes from signed-in players.

    GET /challenges           the current daily and weekly sets, with how many players finished
                              each objective (no names, no ids)
    GET /challenges/progress  the same with the caller's own progress, for the player that signed
                              the request (the launcher's card; the proof of /coop/results)

Off unless the server is started with --daily-challenges (FORZA_DAILY_CHALLENGES=1); any server
may turn them on, and the launcher shows the main server's.

The rotation: a daily set from 00:00 UTC to the next, a weekly set from Monday 00:00 UTC to the
next Monday. Each set is chosen from DAILY or WEEKLY by the seed and the period's id with sha256,
not `random`: every player and every restart get the same set, whatever the Python version. The
seed is the server's secret, never served: --daily-challenges-seed, or without it a random one
made at the first start and kept in DATA_DIR/daily_challenges_seed.txt, so the coming sets cannot
be worked out from the code. A set is kept with its progress once made, so a server update during
a period keeps that period's objectives.

What counts (the leaderboards' rule, #517): only title service calls that carry their player's
own key (scores.source(), set by lsp.service_call for every call). Results taken by another
server never reach this one, so a community server's players progress only there, and the
launcher reads the main server's. The steps:

  race   User.WriteRaceStats(Ex) at every race's end (career events, Rivals, online races): a
         finished race, its place, grid, humans and the car's class (scoreData). A win or a
         podium has to beat someone (last of the grid is neither). At most one race every
         RACE_GAP seconds per player. Event times (SubmitScoreEx) are not used: the
         game sends only a lap that beats the player's best (FH1 #569).
  trap   Scoreboard.SubmitScore type 1 (every speed trap pass), zone: type 2 (speed zones). Only
         a speed below the trap's record and the ceiling (scores.speed_over_limit), whether or not
         the leaderboards enforce them. "Different traps" objectives count distinct boards.
  medal  Challenge.ChallengeCompleted (a 1000 Club medal, Title Update 4): distinct (car,
         challenge) pairs in the period.
  photo  a photo mode Big Shot published as a community photo (community.photo_uploaded).

Each player's progress keeps the builds (X-Forza-Build) that made it, for review. No gamertag is
kept. Stored in DATA_DIR/objectives.json: the current and the previous period of each kind, each
player's count of finished objectives (completed) and the weekly gifts (gifts).

Weekly gift (--daily-challenges-gifts, FORZA_DAILY_CHALLENGES_GIFTS=1; for the main server only,
off by default): GIFT_CREDITS credits through the Message Center (market.gift_credits: the game's
NEW GIFT message, collected with Gifting.RetrieveCreditsGift), once a week (the ISO week of the
weekly set) to a player who has finished GIFT_AFTER objectives that week, daily or weekly. An
objective counts toward it only when every step of it came from a ranked build (ranked_build: a
release launcher's build, 0.2.0 or later). The gift is recorded before it is sent, so a week is
never paid twice (a crash between the two loses that gift rather than sending it again).
"""

import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse

from forza_live.competition import scores
from forza_live.core import bans
from forza_live.core import durable
from forza_live.core import limits

log = logging.getLogger("forza-live")

FILE = "objectives.json"
SEED_FILE = "daily_challenges_seed.txt"
ENABLED = False              # --daily-challenges / FORZA_DAILY_CHALLENGES=1 turns them on
SEED = ""                    # --daily-challenges-seed / FORZA_DAILY_CHALLENGES_SEED, else SEED_FILE
GIFTS = False                # --daily-challenges-gifts / FORZA_DAILY_CHALLENGES_GIFTS=1
# The weekly gift: a fraction of one festival event's prize (an early one pays 35,000 CR for
# first place), about what a won Rivals race pays with its bounty (7,440 CR seen).
GIFT_CREDITS = 10000
GIFT_AFTER = 3               # objectives finished in the week (daily or weekly) for the gift
KEEP_GIFT_WEEKS = 8
# A release launcher's build (X-Forza-Build "0.2.1/tu4"): not a source checkout's "0.1.0", a
# "git-<commit>" build or a game before the header.
RANKED_BUILD = re.compile(r"(\d+)\.(\d+)\.(\d+)/tu\d")
FIRST_RANKED = (0, 2, 0)
DAY = 86400
WEEK = 7 * DAY
RACE_GAP = 20.0              # seconds: at most one race a player counts in that time
MAX_BUILDS = 4               # builds kept per player and period
KINDS = ("daily", "weekly")
PER_SET = 3

# CarClasses ids (leaderboards.CLASSES) and a speed trap target each class reaches (km/h).
CLASSES = {2: "D", 3: "C", 4: "B", 5: "A", 6: "S"}
CLASS_TRAP_KMH = {2: 150, 3: 180, 4: 210, 5: 240, 6: 280}


def objective(type_, title, target, **rule):
    """An objective as kept and served: its type, title and target, and how a step matches it
    (place: at most this place; cls: the car's class; humans: at least this many humans in the
    race; kmh: at least this speed; distinct: count distinct boards or medals, not steps)."""
    return dict(type=type_, title=title, target=target, **rule)


def car(cls):
    """'a B class car', 'an A class car'."""
    return ("an %s" if CLASSES[cls] in ("A", "S") else "a %s") % CLASSES[cls] + " class car"


def win_in_class(target):
    return [objective("race", "Win %d races in %s" % (target, car(c)), target, place=1, cls=c)
            for c in CLASSES]


def trap_in_class():
    return [objective("trap", "Hit %d km/h at a speed trap in %s" % (CLASS_TRAP_KMH[c], car(c)), 1,
                      kmh=CLASS_TRAP_KMH[c], cls=c, distinct=True) for c in CLASSES]


# Templates by group: a set takes at most one objective of a group, and a group's weight is how
# often it comes up against the others (a photo a day would be a lot).
DAILY = {
    "win": (2, win_in_class(2)),
    "place": (2, [objective("race", "Finish 3 races on the podium", 3, place=3),
                  objective("race", "Finish 5 races", 5)]),
    "trap": (2, [objective("trap", "Hit %d km/h at 3 different speed traps" % k, 3, kmh=k,
                           distinct=True) for k in (200, 230, 260)] + trap_in_class()),
    "zone": (2, [objective("zone", "Average %d km/h through 2 different speed zones" % k, 2, kmh=k,
                           distinct=True) for k in (160, 190, 220)]),
    "photo": (1, [objective("photo", "Upload a Big Shot from photo mode", 1)]),
}
WEEKLY = {
    "win": (2, win_in_class(10)),
    "online": (2, [objective("race", "Finish 5 races against other players", 5, humans=2),
                   objective("race", "Win 3 races against other players", 3, place=1, humans=2)]),
    "finish": (2, [objective("race", "Finish 25 races", 25),
                   objective("race", "Finish 10 races on the podium", 10, place=3)]),
    "trap": (2, [objective("trap", "Pass 15 different speed traps", 15, kmh=0, distinct=True),
                 objective("trap", "Hit 280 km/h at 5 different speed traps", 5, kmh=280,
                           distinct=True)]),
    "zone": (2, [objective("zone", "Average 200 km/h through 5 different speed zones", 5, kmh=200,
                           distinct=True)]),
    "medal": (1, [objective("medal", "Earn 5 1000 Club medals (Title Update 4)", 5,
                            distinct=True)]),
    "photo": (1, [objective("photo", "Upload 3 Big Shots from photo mode", 3)]),
}
POOLS = {"daily": DAILY, "weekly": WEEKLY}


def pick(seed, *parts):
    """A number from the seed and parts: sha256, the same on every Python and machine."""
    text = "/".join([seed] + [str(p) for p in parts])
    return int.from_bytes(hashlib.sha256(text.encode()).digest()[:8], "big")


def choose(kind, period_id, seed=None):
    """The objectives of a period: PER_SET of the kind's pool, each from another group, a group
    drawn by its weight, then one of its variants."""
    seed = SEED if seed is None else seed
    pool = POOLS[kind]
    groups = sorted(pool)
    out = []
    for i in range(PER_SET):
        if not groups:
            break
        n = pick(seed, period_id, "group", i) % sum(pool[g][0] for g in groups)
        for group in groups:
            n -= pool[group][0]
            if n < 0:
                break
        groups.remove(group)
        variants = pool[group][1]
        out.append(dict(variants[pick(seed, period_id, "variant", i) % len(variants)]))
    return out


def period(kind, now):
    """(id, start, end) of the period of `kind` that holds `now` (UTC)."""
    day = int(now // DAY)
    if kind == "daily":
        start = day * DAY
        return time.strftime("daily-%Y-%m-%d", time.gmtime(start)), start, start + DAY
    start = (day - (day + 3) % 7) * DAY  # 1970-01-01 was a Thursday: back to the Monday
    year, week, _ = datetime.datetime.fromtimestamp(start, datetime.timezone.utc).isocalendar()
    return "weekly-%04d-W%02d" % (year, week), start, start + WEEK


class Store:
    def __init__(self):
        self.path = None
        self.data = {"version": 1, "periods": {}, "completed": {}, "gifts": {}}
        self.loaded = False
        self.lock = threading.Lock()
        self.last_race = {}     # XUID hex -> when its last race counted (monotonic)

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            data = durable.load(self.path)
        except (OSError, ValueError, TypeError):
            return
        if isinstance(data, dict) and isinstance(data.get("periods"), dict):
            self.data = {"version": 1, "periods": data["periods"],
                         "completed": data.get("completed") or {},
                         "gifts": data.get("gifts") or {}}

    def save(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1)
        durable.replace(tmp, self.path)

    def current(self, kind, now):
        """The stored period of `kind` at `now`, made (and older ones of the kind pruned) when it
        is new: (id, its record)."""
        pid, start, end = period(kind, now)
        periods = self.data["periods"]
        rec = periods.get(pid)
        if rec is None:
            chosen = choose(kind, pid)
            rec = periods[pid] = {"kind": kind, "starts": start, "ends": end,
                                  "objectives": chosen, "completed": [0] * len(chosen),
                                  "players": {}}
            mine = sorted((p for p, r in periods.items() if r.get("kind") == kind),
                          key=lambda p: periods[p].get("starts", 0))
            for old in mine[:-2]:   # the current one and the one before it
                del periods[old]
            log.info("challenges: %s: %s", pid, "; ".join(o["title"] for o in rec["objectives"]))
        rec.setdefault("completed", [0] * len(rec["objectives"]))
        rec.setdefault("players", {})
        return pid, rec


STORE = Store()


def load_seed(data_dir):
    """The server's secret seed from DATA_DIR/SEED_FILE, made (random, 256 bits) when there is
    none."""
    path = os.path.join(data_dir, SEED_FILE)
    try:
        with open(path, encoding="ascii") as f:
            seed = f.read().strip()
        if len(seed) >= 32:
            return seed
        log.warning("challenges: %s is too short: a new seed is made", path)
    except FileNotFoundError:
        pass
    seed = os.urandom(32).hex()
    os.makedirs(data_dir, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="ascii") as f:
        f.write(seed + "\n")
    durable.replace(tmp, path)
    log.info("challenges: a new secret seed is kept in %s", path)
    return seed


def configure(data_dir, seed=None, enabled=False, gifts=False):
    global SEED, ENABLED, GIFTS
    STORE.__init__()
    STORE.path = os.path.join(data_dir, FILE)
    ENABLED, GIFTS = enabled, enabled and gifts
    if enabled:
        SEED = seed or load_seed(data_dir)


def ranked_build(build):
    m = RANKED_BUILD.fullmatch(build or "")
    return bool(m) and tuple(int(v) for v in m.groups()) >= FIRST_RANKED


def hx(xuid):
    return xuid if isinstance(xuid, str) else "%016X" % xuid


def step(xuid, type_, match, now=None):
    """One result of a player: every current objective of `type_` that `match(objective)` accepts
    (a key: a board or medal for distinct objectives, True otherwise; None when it does not)
    moves on. Only from a trusted call. True when progress changed."""
    if not ENABLED:
        return False
    trusted, build = scores.source()
    if not trusted:
        return False
    key = hx(xuid)
    if bans.refused(key, "challenge progress"):  # core/bans.py: no progress, so no gift either
        return False
    now = time.time() if now is None else now
    changed = False
    finished_ranked = []
    with STORE.lock:
        STORE.load()
        for kind in KINDS:
            pid, rec = STORE.current(kind, now)
            for i, obj in enumerate(rec["objectives"]):
                if obj["type"] != type_:
                    continue
                found = match(obj)
                if found is None:
                    continue
                players = rec["players"]
                if key not in players and len(players) >= limits.MAX_PLAYERS:
                    continue
                mine = players.setdefault(key, {})
                prog = mine.setdefault(str(i), {"n": 0, "done": None})
                if prog["done"]:
                    continue
                if obj.get("distinct"):
                    keys = prog.setdefault("keys", [])
                    if str(found) in keys:
                        continue
                    keys.append(str(found))
                    prog["n"] = len(keys)
                else:
                    prog["n"] += 1
                if build and build not in mine.setdefault("builds", []) and \
                        len(mine["builds"]) < MAX_BUILDS:
                    mine["builds"].append(build)
                if not ranked_build(build):
                    prog["unranked"] = True   # finished, it earns nothing towards a gift
                if prog["n"] >= obj["target"]:
                    prog["done"] = int(now)
                    if not prog.get("unranked"):
                        finished_ranked.append(obj["title"])
                    rec["completed"][i] += 1
                    totals = STORE.data["completed"]
                    if key in totals or len(totals) < limits.MAX_PLAYERS:
                        counts = totals.setdefault(key, {})
                        counts[kind] = counts.get(kind, 0) + 1
                    log.info("challenges: %s finished %s.%d (%s)", key, pid, i, obj["title"])
                changed = True
        if finished_ranked:
            gift_step(xuid, len(finished_ranked), now)
        if changed:
            STORE.save()
    return changed


def gift_record(key, now, create=False):
    """The player's record of the week of `now` ({"n": objectives finished, "sent", "gift"}),
    the weeks older than KEEP_GIFT_WEEKS pruned when one is made."""
    week = period("weekly", now)[0]
    weeks = STORE.data.setdefault("gifts", {})
    if create and week not in weeks:
        weeks[week] = {}
        for old in sorted(weeks)[:-KEEP_GIFT_WEEKS]:
            del weeks[old]
    players = weeks.get(week, {})
    if create and key not in players and len(players) < limits.MAX_PLAYERS:
        players[key] = {"n": 0, "sent": None, "gift": None}
    return week, players.get(key)


def gift_step(xuid, finished, now):
    """`finished` objectives done with results that count (STORE.lock held): the week's count,
    and the gift once it reaches GIFT_AFTER (recorded, then sent)."""
    key = hx(xuid)
    week, rec = gift_record(key, now, create=True)
    if rec is None:
        return
    rec["n"] += finished
    if not GIFTS or rec["sent"] or rec["n"] < GIFT_AFTER:
        return
    rec["sent"] = int(now)
    STORE.save()   # before the gift: a week is never paid twice
    from forza_live.content import market
    try:
        rec["gift"] = market.gift_credits(int(key, 16), GIFT_CREDITS,
                                          "Weekly challenges: %d finished this week" % rec["n"])
    except (OSError, ValueError) as e:
        log.warning("challenges: %s: the %s gift was not sent: %s", key, week, e)
        return
    log.info("challenges: %s: %d credits for %s (gift %s)", key, GIFT_CREDITS, week, rec["gift"])


def race(xuid, stats, class_id=None, now=None):
    """User.WriteRaceStats(Ex) of a trusted caller: `stats` its raceStats, `class_id` the car's
    class from its scoreData (None when the call had none)."""
    if not stats.get("finished") or stats.get("userFinishPosition", 0) < 1:
        return False
    place, places = stats["userFinishPosition"], stats.get("maxPlaces", 0)
    humans = stats.get("numHumans", 0)
    mono = time.monotonic()
    key = hx(xuid)
    if mono - STORE.last_race.get(key, -RACE_GAP) < RACE_GAP:
        log.info("challenges: %s: a race %.0f s after the last one: not counted", key,
                 mono - STORE.last_race[key])
        return False

    def match(obj):
        # a place has to beat someone: last of the grid is no win or podium
        if obj.get("place") is not None and (place > obj["place"] or place >= places):
            return None
        if obj.get("cls") is not None and class_id != obj["cls"]:
            return None
        if humans < obj.get("humans", 0):
            return None
        return True
    changed = step(xuid, "race", match, now)
    if changed or scores.source()[0]:
        STORE.last_race[key] = mono
        if len(STORE.last_race) > 50000:
            STORE.last_race.clear()
    return changed


def speed(xuid, kind, board, score, class_id, now=None):
    """A speed trap (scores.SPEED_TRAP) or speed zone (SPEED_ZONE) pass of a trusted caller,
    score in m/s. A speed above the trap's record or the ceiling does not count."""
    if kind not in (scores.SPEED_TRAP, scores.SPEED_ZONE) or not 0 < score < 1e9:
        return False
    if scores.speed_over_limit(kind, board, score) is not None:
        return False
    kmh = score * 3.6

    def match(obj):
        if kmh < obj.get("kmh", 0) or (obj.get("cls") is not None and class_id != obj["cls"]):
            return None
        return board if obj.get("distinct") else True
    return step(xuid, "trap" if kind == scores.SPEED_TRAP else "zone", match, now)


def medal(xuid, car, thumbprint, now=None):
    """A 1000 Club challenge completed (Challenge.ChallengeCompleted) by a trusted caller."""
    if not car:
        return False
    return step(xuid, "medal", lambda obj: "%d:%08X" % (car, thumbprint), now)


def photo(xuid, now=None):
    """A Big Shot of the player's published as a community photo."""
    return step(xuid, "photo", lambda obj: True, now)


# ---- HTTP ----

def document(xuid=None, now=None):
    """The current sets (with the player's progress when `xuid` is given)."""
    now = time.time() if now is None else now
    key = hx(xuid) if xuid is not None else None
    banned = bans.banned_keys()
    sets = []
    with STORE.lock:
        STORE.load()
        made = len(STORE.data["periods"])
        for kind in KINDS:
            pid, rec = STORE.current(kind, now)
            mine = rec["players"].get(key, {}) if key else {}
            # Banned players' finishes are kept but not counted (core/bans.py)
            hidden = [p for k, p in rec["players"].items() if k in banned]
            objectives = []
            for i, obj in enumerate(rec["objectives"]):
                done = rec["completed"][i] - sum(1 for p in hidden if (p.get(str(i)) or {}).get("done"))
                o = {"id": "%s.%d" % (pid, i), "type": obj["type"], "title": obj["title"],
                     "target": obj["target"], "completed": max(0, done)}
                if key:
                    prog = mine.get(str(i)) or {}
                    o.update(progress=min(prog.get("n", 0), obj["target"]), done=prog.get("done"))
                objectives.append(o)
            sets.append({"kind": kind, "id": pid, "starts": rec["starts"], "ends": rec["ends"],
                         "objectives": objectives})
        if len(STORE.data["periods"]) != made:   # a new period was made: keep its objectives
            STORE.save()
        doc = {"version": 1, "server": scores.server_id(), "now": int(now), "sets": sets}
        if GIFTS:
            doc["gift"] = {"credits": GIFT_CREDITS, "after": GIFT_AFTER}
        if key:
            doc["player"] = {"completed": dict(STORE.data["completed"].get(key, {}))}
            if GIFTS:
                week, rec = gift_record(key, now)
                doc["player"]["gift"] = {"week": week, "finished": (rec or {}).get("n", 0),
                                         "sent": (rec or {}).get("sent")}
    return doc


def handle(server, method, path, headers, body, authenticate):
    """(status, content type, body[, extra headers]) for /challenges...; None for another path.
    authenticate(store, headers, method, path, body) -> XUID or None
    (shader_lists.authenticate)."""
    url = urllib.parse.urlsplit(path)
    if url.path not in ("/challenges", "/challenges/progress") or not ENABLED:
        return None
    if method != "GET":
        return 405, "application/json", json.dumps({"error": "method"}).encode()
    if url.path == "/challenges":
        return (200, "application/json", json.dumps(document()).encode(),
                ("Cache-Control: public, max-age=60",))
    xuid = authenticate(server.friends.store if server is not None else None, headers, method,
                        path, body)
    if xuid is None:
        return 403, "application/json", json.dumps({"error": "unknown player"}).encode()
    return (200, "application/json", json.dumps(document(xuid)).encode(),
            ("Cache-Control: no-store",))
