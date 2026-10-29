"""Forza Live: scoreboards (leaderboards), Rivals and their ghosts.

The game submits a score at the end of an event (Scoreboard.SubmitScore / SubmitScoreEx:
the scoreboard id of the event, the time or score, the car, its class and performance
index, assists and tuning) and reads scoreboards for its leaderboard screens and for
Rivals (GetRows, GetRival, GetRivalAndPersonalBest, GetBestScores). The answer to a
submission asks for the ghost (uploadFiles) when the score is the player's new best; the
game then uploads it (UploadFile: the lap's recording, up to 250 KB, checked by forza_ghost.py
and kept without its driver's name) and another player racing that score as a rival
downloads it (DownloadFile, by the row's ghostFileId). docs/title_services.md has the details.

The 1000 Club (Title Update 4) reports each car challenge a player completes
(Challenge.ChallengeCompleted: the car and the challenge's thumbprint); its leaderboard
(GetRows type 5) ranks the players by the challenges (medals) they completed.

Integrity (FH1 #517). The boards (the game's and the public JSON, forza_leaderboards.py) take
results only from players signed in to this server: a call that carries its player's own key
(forza_live_lsp.caller_trusted). Each new row keeps the server that took it (SERVER_ID) and
the build the game names (X-Forza-Build). A result kept off the boards is "unranked", as osu!
does: kept for review (STORE.unranked, the player's best one per board) but never shown. That
is a result from an unverified caller, one whose ghost fails the checks (forza_ghost.py; the
player's previous row stays), and rows an operator unranks. /admin/scores (admin()) lists,
unranks, ranks again or deletes rows by server, build or player. Rows from before the tags
stay as they are.

Everything is kept in DATA_DIR/scoreboards.json (the best score of each player on each
scoreboard, the unranked ones, the 1000 Club medals), DATA_DIR/ghosts/<id>.bin and
DATA_DIR/ghosts/review/<scoreId>.bin (refused ghosts, kept with their unranked rows).
"""

import contextvars
import json
import logging
import os
import re
import secrets
import time
import urllib.parse
import uuid

import forza_ghost as ghostfile
import forza_limits as limits
import forza_xtsw as x
import forza_xtsw_all as A
import forza_durable as durable

log = logging.getLogger("forza-live")

DATA_DIR = "forza_live_data"
GHOST_MAX = ghostfile.GHOST_MAX  # the game's download buffer (DownloadFile maxSize)
RIVAL_BOUNTY = 1000          # credits for beating a rival, shown as BOUNTY BONUS

# Score types: SubmitScore's `type`, GetRows' `type`, GetBestScores' scoreType, NotifyRival's
# `type`. A board is a (type, gameScoreboardId, isServerTimeTrial): the ids overlap, e.g. the
# I-70 speed trap is board 230 and so is the event with Id 230 (docs/title_services.md,
# "Every leaderboard").
SPEED_TRAP = 1      # speed cameras (Colorado/speed_camera.xml, boards 201-230): the speed, m/s
SPEED_ZONE = 2      # average speed zones (Colorado/average_speed.xml, boards 101-112): m/s
EVENT_TIME = 3      # events and Rivals (SubmitScoreEx): the best lap or race time, s
ONLINE_XP_TYPE = 4  # Race Central's ONLINE XP: GetRows(type 4, board 1); SubmitScore type 4
CLUB_TYPE = 5       # the 1000 Club's leaderboard: GetRows(type 5, board 1)
# Types whose scores are better when bigger (speeds, XP); times are better when smaller.
BIGGER_IS_BETTER = {SPEED_TRAP, SPEED_ZONE, ONLINE_XP_TYPE}
KEPT_TYPES = {SPEED_TRAP, SPEED_ZONE, EVENT_TIME}  # submissions kept on a board
GHOST_TYPES = {EVENT_TIME}  # a new best asks for its ghost (laps; a speed trap has none)
# GetRows view 5 ("Friends & Club"): what the speed traps and zones read at the trap.
FRIENDS_AND_CLUB = 5
# Speed trap limits (#349), km/h. A fast travel onto a trap recorded the speed the car had
# before the fast travel (fixed in the game by src/hooks/teleport_velocity.cpp). The public
# server sets no limit: players drive modified and modded cars. With --trap-limits a speed
# trap score above its trap's SPEED_TRAP_LIMIT_KMH (the best known Xbox 360 result on that
# trap: Cyberscore's Forza Horizon speed trap charts, cyberscore.me.uk charts 230405-230426)
# or any trap/zone speed above SPEED_CEILING_KMH is not kept, and such kept speeds are
# removed at load. Without it they are only logged.
SPEED_CEILING_KMH = 470.0   # fastest stock results on long straights: about 435-442 km/h
ENFORCE_TRAP_LIMITS = False  # --trap-limits turns it on
SPEED_TRAP_LIMIT_KMH = {
    201: 423.1,   # Gladstone
    202: 350.8,   # C-987
    203: 412.6,   # East Rt. 20
    205: 390.3,   # Carson
    206: 233.0,   # Red Rock
    209: 229.1,   # C-203
    210: 267.7,   # Sth Rt. 38
    211: 362.3,   # East Rt. 27
    212: 354.5,   # Rt. 38
    213: 411.2,   # Finley Dam
    214: 340.3,   # Rt. 25
    215: 371.5,   # Beaumont
    216: 275.3,   # West Rt. 27
    218: 436.8,   # I-70
    220: 298.8,   # C-642
    224: 436.9,   # I-76
    225: 439.6,   # Rt. 20
    226: 241.3,   # C-201
    227: 372.9,   # Rt. 27
    228: 348.9,   # Rt. 6
    229: 260.4,   # Rt. 33
    230: 348.3,   # Rt. 29
}
MAX_BOARDS = 4096   # boards on the server (the game has about 160)
MAX_PENDING_GHOSTS = 4096  # new bests waiting for their ghost (UploadFile follows at once)

# The server that takes a row (--server-id; without it an id made at the first start and kept
# in DATA_DIR/server_id.txt) and who the call is from (forza_live_lsp.service_call sets it for
# each call: whether it carries its player's own key, and the build the game names). Outside a
# call (tests, tools) a submission is the server's own: trusted, no build.
SERVER_ID = ""
_server_id = None
_source = contextvars.ContextVar("score_source", default=None)
ID_CHARS = re.compile(r"[^A-Za-z0-9._+/-]")


def clean_id(text, limit=64):
    """A server id or build as kept: printable ASCII from a short set, at most `limit` chars."""
    return ID_CHARS.sub("_", text.strip())[:limit] if isinstance(text, str) else ""


def server_id():
    global _server_id
    if _server_id is None:
        _server_id = clean_id(SERVER_ID)
        path = os.path.join(DATA_DIR, "server_id.txt")
        if not _server_id:
            try:
                with open(path) as f:
                    _server_id = clean_id(f.read())
            except OSError:
                pass
        if not _server_id:
            _server_id = "server-" + secrets.token_hex(4)
            try:
                os.makedirs(DATA_DIR, exist_ok=True)
                with open(path, "w") as f:
                    f.write(_server_id + "\n")
            except OSError as e:
                log.warning("scoreboards: cannot keep the server id in %s: %s", path, e)
    return _server_id


def set_source(trusted, build):
    """Who the title service call being handled is from; returns the token for reset_source."""
    return _source.set((bool(trusted), clean_id(build or "")))


def reset_source(token):
    _source.reset(token)


def source():
    """(trusted, build) of the call being handled."""
    return _source.get() or (True, "")


def board_key(kind, sbid, stt):
    return "%d:%d:%d" % (kind, sbid, 1 if stt else 0)


class Store:
    """Best score per player per scoreboard, and the ghosts."""

    def __init__(self):
        self.boards = {}       # "type:sbid:stt" -> {xuid hex: entry}
        self.unranked = {}     # the same, of the results kept off the boards (for review)
        # scoreId hex -> (board key, xuid hex, the row it replaced or None): waiting for UploadFile
        self.ghosts = {}
        self.club = {}         # 1000 Club: xuid hex -> {gamertag, cars: {carId: [thumbprints]}}
        self.next_file = 1
        self.loaded = False

    def path(self):
        return os.path.join(DATA_DIR, "scoreboards.json")

    def load(self):
        if self.loaded:
            return
        self.loaded = True
        try:
            data = durable.load(self.path())
            self.boards = migrate(data.get("boards", {}))
            self.unranked = data.get("unranked", {})
            self.club = data.get("club1000", {})
            self.next_file = data.get("nextFileId", 1)
        except (OSError, ValueError):
            return
        # Speeds kept before the limits (or by --no-trap-limits) would otherwise stay the
        # players' bests and block their real ones (#349).
        if drop_impossible_speeds(self.boards):
            self.save()

    def save(self):
        os.makedirs(DATA_DIR, exist_ok=True)
        tmp = self.path() + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"boards": self.boards, "unranked": self.unranked, "club1000": self.club,
                       "nextFileId": self.next_file}, f, indent=1)
        durable.replace(tmp, self.path())

    def board(self, kind, sbid, stt, create=False):
        """A board's entries by player: {} for a board nobody is on, or with create the
        stored board (made when missing; None when the server has MAX_BOARDS)."""
        self.load()
        key = board_key(kind, sbid, stt)
        if create and key not in self.boards:
            if len(self.boards) >= MAX_BOARDS:
                return None
            self.boards[key] = {}
        return self.boards[key] if create else self.boards.get(key, {})

    def ranked(self, kind, sbid, stt):
        """The board's entries, best first."""
        entries = list(self.board(kind, sbid, stt).values())
        bigger = kind in BIGGER_IS_BETTER
        entries.sort(key=lambda e: (-e["score"] if bigger else e["score"], e["submitted"]))
        return entries, bigger


def migrate(boards):
    """Boards kept before the type was part of the key ("sbid:stt", 2026-10): each entry
    goes to the board of its type. Where one player had a speed and a time on the same id,
    only the later submission was kept; it stays."""
    out = {}
    for key, board in boards.items():
        parts = key.split(":")
        if len(parts) == 3:
            out.setdefault(key, {}).update(board)
            continue
        sbid, stt = int(parts[0]), int(parts[1])
        for xuid, e in board.items():
            out.setdefault(board_key(e.get("type", EVENT_TIME), sbid, stt), {})[xuid] = e
    return out


STORE = Store()


def configure(data_dir, server=None):
    global DATA_DIR, SERVER_ID, _server_id
    DATA_DIR = data_dir
    if server is not None:
        SERVER_ID = server
    _server_id = None
    STORE.__init__()


def better(new, old, bigger):
    return new > old if bigger else new < old


def speed_over_limit(kind, sbid, score):
    """(limit in km/h, enforced) when a speed on a trap or zone board is above a limit: the
    trap's record or SPEED_CEILING_KMH, enforced with ENFORCE_TRAP_LIMITS; else None."""
    if kind not in (SPEED_TRAP, SPEED_ZONE):
        return None
    kmh = score * 3.6
    if kmh > SPEED_CEILING_KMH:
        return SPEED_CEILING_KMH, ENFORCE_TRAP_LIMITS
    limit = SPEED_TRAP_LIMIT_KMH.get(sbid) if kind == SPEED_TRAP else None
    if limit is not None and round(kmh, 1) > limit:   # at the records' precision, 0.1 km/h
        return limit, ENFORCE_TRAP_LIMITS
    return None


def implausible_speed(xuid, kind, sbid, score):
    """True when a speed on a trap or zone board is not to be kept (logged either way)."""
    over = speed_over_limit(kind, sbid, score)
    if over is None:
        return False
    limit, enforced = over
    if limit == SPEED_CEILING_KMH:
        log.warning("title: score %s: board %d type %d: %.1f km/h is more than any stock car "
                    "reaches (%.0f km/h)%s", public_id(xuid), sbid, kind, score * 3.6,
                    limit, "; not kept" if enforced else "")
    else:
        log.warning("title: score %s: speed trap %d: %.1f km/h is above the trap's record "
                    "(%.1f km/h)%s", public_id(xuid), sbid, score * 3.6, limit,
                    "; not kept" if enforced else "")
    return enforced


def drop_impossible_speeds(boards):
    """Removes the kept speeds a submission would not be kept with now (speed_over_limit,
    enforced): the store's scores from before the limits, or kept with --no-trap-limits.
    Logs each one; returns how many went."""
    removed = 0
    for key in list(boards):
        try:
            kind, sbid = (int(p) for p in key.split(":")[:2])
        except ValueError:
            continue
        board = boards[key]
        for xuid_hex, e in list(board.items()):
            score = e.get("score", 0) if isinstance(e, dict) else 0
            over = speed_over_limit(kind, sbid, score)
            if over is None or not over[1]:
                continue
            del board[xuid_hex]
            removed += 1
            log.warning("title: scoreboards: removed %s's %.1f km/h from board %d type %d, above "
                        "%.1f km/h", public_id(xuid_hex), score * 3.6, sbid, kind,
                        over[0])
        if not board:
            del boards[key]
    return removed


def public_id(xuid):
    """The player (an XUID, or a board's key for one) as the public leaderboards name them
    (forza_leaderboards.player_id)."""
    import forza_leaderboards  # it imports this module
    return forza_leaderboards.player_id(xuid if isinstance(xuid, str) else "%016X" % xuid)


def tag(entry, trusted, build):
    """The row's source: this server, the build the game named, whether the call was its own."""
    entry.update(server=server_id(), build=build, verified=trusted)
    return entry


def unrank(key, entry, reason, ghost=None):
    """Keeps a result off the boards, for review: the player's best unranked one on the board
    (a better one replaces it), with its refused ghost (DATA_DIR/ghosts/review). False when it is
    not kept (not better, or the server has too many)."""
    STORE.load()
    review = STORE.unranked.get(key)
    if review is None:
        if len(STORE.unranked) >= MAX_BOARDS:
            return False
        review = STORE.unranked[key] = {}
    old = review.get(entry["xuid"])
    if old and not better(entry["score"], old["score"], int(key.split(":")[0]) in BIGGER_IS_BETTER):
        return False
    if not old and len(review) >= limits.MAX_PLAYERS:
        return False
    if old:
        remove_review_ghost(old)
    entry = dict(entry, unranked=reason, unrankedAt=int(time.time()), ghostFileId=-1)
    entry.pop("reviewGhost", None)
    if ghost and len(ghost) <= GHOST_MAX:
        try:
            limits.accept(len(ghost))
            os.makedirs(os.path.dirname(review_ghost_path(entry["scoreId"])), exist_ok=True)
            with open(review_ghost_path(entry["scoreId"]), "wb") as f:
                f.write(ghost)
            entry["reviewGhost"] = True
        except (OSError, ValueError) as e:
            log.info("title: refused ghost of score %s not kept: %s", entry["scoreId"], e)
    review[entry["xuid"]] = entry
    return True


def submit(xuid, gamertag, kind, data, extra=None):
    """Keeps a submission if it is the player's best; returns the reply (scoreId, and
    uploadFiles when we want its ghost). A submission from an unverified caller is kept
    unranked."""
    sbid, stt, score = data["gameScoreboardId"], data["isServerTimeTrial"], data["score"]
    score_id = uuid.uuid4().bytes
    trusted, build = source()
    log.info("title: score %016X '%s': board %d%s type %d score %.3f car %d class %d PI %.3f%s%s",
             xuid, gamertag, sbid, " (time trial)" if stt else "", kind, score, data["carId"],
             data["carClassId"], data["carPerformanceIndex"],
             " passed %d" % extra if extra is not None else "",
             (" build %s" % build if build else "") + ("" if trusted else " (unverified)"))
    nothing = x.reply(x.ScoreboardSubmitScore_Results, {"scoreId": score_id, "uploadFiles": False})
    if implausible_speed(xuid, kind, sbid, score):
        return nothing
    # Online races submit board -1 (type 4, the XP of the game): not kept.
    if kind not in KEPT_TYPES or sbid < 0 or not 0 < score < 1e9:
        return nothing
    key = "%016X" % xuid
    entry = {"xuid": key, "gamertag": limits.clean_gamertag(gamertag, xuid), "type": kind, "score": score,
             "submitted": int(time.time()), "scoreId": score_id.hex(),
             "carId": data["carId"], "carClassId": data["carClassId"],
             "carPerformanceIndex": data["carPerformanceIndex"], "ghostFileId": -1}
    for k in ("usedSTM", "usedTCS", "usedABS", "usedAutoBrake", "usedAutoShifting", "usedClutch"):
        entry[k] = data[k]
    tag(entry, trusted, build)
    if not trusted:
        # Not this server's player (an older game, or someone else): the boards are for players
        # signed in here.
        if unrank(board_key(kind, sbid, stt), entry, "unverified"):
            STORE.save()
            log.info("title: score %016X: unverified caller, kept unranked", xuid)
        return nothing
    board = STORE.board(kind, sbid, stt, create=True)
    if board is None:
        return nothing
    old = board.get(key)
    bigger = kind in BIGGER_IS_BETTER
    if old and not better(score, old["score"], bigger):
        return nothing
    if not old and len(board) >= limits.MAX_PLAYERS:
        return nothing
    board[key] = entry
    ghost = kind in GHOST_TYPES
    if ghost:
        # The row it replaces stays (with its ghost) until the new one's ghost passes: a refused
        # ghost puts it back. A row still waiting for its own ghost passes on the one before it.
        pending = STORE.ghosts.pop(old["scoreId"], None) if old else None
        previous = pending[2] if pending else old
        if len(STORE.ghosts) >= MAX_PENDING_GHOSTS:  # uploads that never came
            dropped = STORE.ghosts.pop(next(iter(STORE.ghosts)))[2]
            if dropped and dropped.get("ghostFileId", -1) >= 0:
                remove_ghost(dropped["ghostFileId"])
        STORE.ghosts[score_id.hex()] = (board_key(kind, sbid, stt), key, previous)
    elif old and old.get("ghostFileId", -1) >= 0:
        remove_ghost(old["ghostFileId"])
    STORE.save()
    log.info("title: score %016X: new best on board %d type %d%s", xuid, sbid, kind,
             ", asking for the ghost" if ghost else "")
    return x.reply(x.ScoreboardSubmitScore_Results, {"scoreId": score_id, "uploadFiles": ghost})


def ghost_path(file_id):
    return os.path.join(DATA_DIR, "ghosts", "%d.bin" % file_id)


def review_ghost_path(score_id):
    return os.path.join(DATA_DIR, "ghosts", "review", "%s.bin" % score_id)


def remove_ghost(file_id):
    try:
        os.remove(ghost_path(file_id))
    except OSError:
        pass


def remove_review_ghost(entry):
    if entry.get("reviewGhost") and re.fullmatch(r"[0-9a-f]{32}", entry.get("scoreId", "")):
        try:
            os.remove(review_ghost_path(entry["scoreId"]))
        except OSError:
            pass


def upload_file(xuid, body):
    """UploadFile(scoreId, data): the ghost of a new best (submit asked for it). A ghost that
    fails the checks (forza_ghost.check) is refused: the row goes unranked with it, the player's
    previous row comes back, and the game gets a failure (it carries on to the results as after
    a success; it does not upload again). A ghost that passes is kept without its driver's name."""
    params, _ = x.decode(x.ScoreboardUploadFile_Params, body)
    sid = params["scoreId"].hex()
    data = params["data"]
    found = STORE.ghosts.get(sid)
    if not found or found[1] != "%016X" % xuid:
        log.info("title: UploadFile %016X: unknown score %s (%d bytes)", xuid, sid, len(data))
        return x.reply()
    del STORE.ghosts[sid]
    key, player, previous = found
    board = STORE.boards.get(key, {})
    entry = board.get(player)
    if not entry or entry["scoreId"] != sid:
        return x.reply()  # beaten again since
    problem, kept = ghostfile.check(data, entry["score"], entry["carId"], tracks_of(key))
    if problem is None:
        try:
            limits.accept(len(data))  # the disk quota: the row stays, without a ghost
        except ValueError:
            if previous and previous.get("ghostFileId", -1) >= 0:
                remove_ghost(previous["ghostFileId"])
            raise
    else:
        if previous:
            board[player] = previous
        else:
            del board[player]
            if not board:
                del STORE.boards[key]
        unrank(key, entry, "ghost: " + problem, data)
        STORE.save()
        log.warning("title: ghost from %016X for board %s refused (%d bytes): %s; the score "
                    "%.3f is unranked%s", xuid, key, len(data), problem, entry["score"],
                    ", the previous one stays" if previous else "")
        return x.failure(2, "ghost refused: " + problem)
    if previous and previous.get("ghostFileId", -1) >= 0:
        remove_ghost(previous["ghostFileId"])
    file_id = STORE.next_file
    STORE.next_file += 1
    os.makedirs(os.path.dirname(ghost_path(file_id)), exist_ok=True)
    with open(ghost_path(file_id), "wb") as f:
        f.write(kept)
    entry["ghostFileId"] = file_id
    STORE.save()
    log.info("title: ghost %d from %016X for board %s (%d bytes, %d kept)", file_id, xuid, key,
             len(data), len(kept))
    return x.reply()


def download_file(xuid, body):
    params, _ = x.decode(x.ScoreboardDownloadFile_Params, body)
    try:
        with open(ghost_path(params["id"]), "rb") as f:
            data = f.read()
    except OSError:
        log.info("title: DownloadFile %d for %016X: no such file", params["id"], xuid)
        return x.failure(1, "no such file")
    if params["maxSize"] > 0 and len(data) > params["maxSize"]:
        return x.failure(1, "file too big")
    log.info("title: DownloadFile %d for %016X (%d bytes)", params["id"], xuid, len(data))
    return x.reply(x.ScoreboardDownloadFile_Results, {"data": data})


def user_data(entry):
    """The row's player, with their car club when they are in one (forza_clubs)."""
    import forza_clubs
    u = {"qwXuid": int(entry["xuid"], 16), "wzGamerTag": entry["gamertag"],
         "currentCareerLevel": entry.get("level", 0)}
    club, role = forza_clubs.membership(u["qwXuid"])
    if club:
        u.update(clanId=club["id"], clanTag=club["tag"], roleInClan=role)
    return u


def row(entry, position):
    r = {k: entry[k] for k in ("score", "carId", "carClassId", "carPerformanceIndex",
                                "usedSTM", "usedTCS", "usedABS", "usedAutoBrake",
                                "usedAutoShifting", "usedClutch", "ghostFileId")}
    r.update(position=position, rank=position, user=user_data(entry),
             submittedTime=entry["submitted"],
             bounty=RIVAL_BOUNTY)
    return r


NO_ROW = {"position": 0, "rank": 0, "ghostFileId": -1}


def rows_view(entries, xuid, view, start_at, max_results):
    """(rows, the caller's row) for a view: 0 the top, 1 around the caller, 2 friends
    (everyone here), others the top."""
    key = "%016X" % xuid
    mine = next((i for i, e in enumerate(entries) if e["xuid"] == key), None)
    max_results = max(0, min(max_results, 200))
    if view == 1 and mine is not None:
        first = max(0, min(mine - max_results // 2, len(entries) - max_results))
    else:
        first = max(0, start_at - 1)
    rows = [row(e, i + 1) for i, e in enumerate(entries)][first:first + max_results]
    user_row = row(entries[mine], mine + 1) if mine is not None else NO_ROW
    return rows, user_row



# 1000 Club: 218 cars with 5 or 6 challenges each, 1145 in all (Title Update 4's
# gamedb.slt, CarChallenges). The limits keep a bad client from growing the store.
CLUB_MAX_PER_CAR = 8
CLUB_MAX_CARS = 400


def challenge_completed(gamertag_of):
    """Challenge.ChallengeCompleted(carId, challengeThumbprint): a 1000 Club challenge
    completed (a medal). Sent once per completion; a challenge done again in the same car
    sends the same thumbprint. No results."""
    def handler(xuid, body):
        p, _ = x.decode(A.ForzaChallengeChallengeCompleted_Params, body)
        car, thumb = p["carId"], p["challengeThumbprint"]
        trusted, build = source()
        if not trusted:  # the board is for players signed in here
            log.info("title: 1000 Club %016X: challenge %08X in car %d from an unverified caller: "
                     "not counted", xuid, thumb, car)
            return x.reply()
        STORE.load()
        key = "%016X" % xuid
        entry = STORE.club.setdefault(key, {"xuid": key, "cars": {}})
        entry["gamertag"] = gamertag_of(xuid)
        cars = entry["cars"]
        done = cars.get(str(car), [])
        new = thumb not in done and car != 0 and len(done) < CLUB_MAX_PER_CAR and \
            (str(car) in cars or len(cars) < CLUB_MAX_CARS)
        if new:
            cars[str(car)] = done + [thumb]
            entry.update(carId=car, submitted=int(time.time()), server=server_id(), build=build)
            STORE.save()
        log.info("title: 1000 Club %016X '%s': challenge %08X in car %d%s, %d medals", xuid,
                 entry["gamertag"], thumb, car, "" if new else " (already counted)",
                 club_medals(entry))
        return x.reply()
    return handler


def club_medals(entry):
    return sum(len(v) for v in entry["cars"].values())


def club_entries():
    """Every player with a 1000 Club medal, most medals first (the first to get there
    first on a tie)."""
    STORE.load()
    out = []
    for e in STORE.club.values():
        medals = club_medals(e)
        if medals:
            out.append({"xuid": e["xuid"], "gamertag": e.get("gamertag", e["xuid"]),
                        "score": float(medals), "submitted": e.get("submitted", 0),
                        "carId": e.get("carId", 0), "carClassId": 0,
                        "carPerformanceIndex": 0.0, "usedSTM": 0, "usedTCS": 0, "usedABS": 0,
                        "usedAutoBrake": 0, "usedAutoShifting": 0, "usedClutch": 0,
                        "ghostFileId": -1})
    out.sort(key=lambda e: (-e["score"], e["submitted"]))
    return out


def club_only(entries, xuid):
    """A club board (isClubBoard): the rows of the caller's car club's members (none when
    the caller is in no club)."""
    import forza_clubs
    club, _ = forza_clubs.membership(xuid)
    if not club:
        return []
    return [e for e in entries
            if (forza_clubs.membership(int(e["xuid"], 16))[0] or {}).get("id") == club["id"]]


def get_rows(xuid, body):
    p, _ = x.decode(x.ScoreboardGetRows_Params, body)
    if p["type"] == ONLINE_XP_TYPE:
        entries = online_xp_entries()
    elif p["type"] == CLUB_TYPE:
        entries = club_entries()
    else:
        entries, _ = STORE.ranked(p["type"], p["gameScoreboardId"], p["isServerTimeTrial"])
    if p["isClubBoard"]:
        entries = club_only(entries, xuid)
    rows, user_row = rows_view(entries, xuid, p["view"], p["startAt"], p["maxResults"])
    # The speed traps and zones (CAmbientChallengeLeaderboard) place a new score by walking up
    # from the caller's position through the rows sent (row[position - 2]): Turn 10's Friends
    # & Club was a small group the caller was in. Here the view sends the top of everyone, so a
    # caller ranked below those rows gets the place right after them (rank keeps the real
    # one); 713 with 200 rows read past the game's array and crashed it (#288).
    if p["view"] == FRIENDS_AND_CLUB and p["type"] in (SPEED_TRAP, SPEED_ZONE) and \
            user_row["position"] > len(rows) + 1:
        user_row = dict(user_row, position=len(rows) + 1)
    log.info("title: GetRows %016X board %d%s type %d view %d%s from %d max %d: %d of %d", xuid,
             p["gameScoreboardId"], " (time trial)" if p["isServerTimeTrial"] else "",
             p["type"], p["view"], " (club)" if p["isClubBoard"] else "", p["startAt"],
             p["maxResults"], len(rows), len(entries))
    return x.reply(x.ScoreboardGetRows_Results, {"rows": rows, "userRow": user_row,
                                                 "available": len(entries),
                                                 "totalRowCount": len(entries)})


# Rollup boards (GetRollupRows: Race Central's LEADERBOARDS, "VIEW OVERALL"): eventTypeId 1
# festival events, 2 street races, 3 showcases; pivotId -1 all, else a wristband level.
# A scoreboard id is the event's Id in the game database (Events: HorizonEventID "FR61",
# Level 6 for board 199, GoPro Millennia Masters).
EVENT_TYPES = {1: "FR", 2: "ST", 3: "EX"}
EVENTS = None  # Id -> (HorizonEventID prefix, wristband level)


def events():
    global EVENTS
    if EVENTS is None:
        EVENTS = {}
        import forza_world
        if forza_world.GAME_DB and os.path.isfile(forza_world.GAME_DB):
            import sqlite3
            try:
                db = sqlite3.connect("file:%s?mode=ro" % forza_world.GAME_DB, uri=True)
                EVENTS = {i: ((h or "")[:2], lvl) for i, h, lvl in
                          db.execute("select Id, HorizonEventID, Level from Events")}
                db.close()
            except sqlite3.Error as e:
                log.warning("scoreboards: game database: %s", e)
    return EVENTS


RACE_TRACKS = None  # event Id -> {track ids of its races} (the game's database, Races)


def tracks_of(key):
    """The tracks a board's ghost may be on: a time trial's, or an event's races' (from the game's
    database); None when the server does not know them."""
    global RACE_TRACKS
    kind, sbid, stt = (int(v) for v in key.split(":"))
    if stt:
        return {t[4] for t in TIME_TRIALS if t[0] == sbid} or None
    if RACE_TRACKS is None:
        RACE_TRACKS = {}
        import forza_world
        if forza_world.GAME_DB and os.path.isfile(forza_world.GAME_DB):
            import sqlite3
            try:
                db = sqlite3.connect("file:%s?mode=ro" % forza_world.GAME_DB, uri=True)
                for event, track in db.execute("select EventId, TrackId from Races"):
                    RACE_TRACKS.setdefault(event, set()).add(track)
                db.close()
            except sqlite3.Error as e:
                log.warning("scoreboards: game database: %s", e)
    return RACE_TRACKS.get(sbid)


def in_rollup(key, event_type, pivot):
    kind, sbid, stt = (int(v) for v in key.split(":"))
    if stt or kind != EVENT_TIME:
        return False
    info = events().get(sbid)
    if info is None:
        return not events()  # without the database every board counts
    prefix, level = info
    if event_type in EVENT_TYPES and prefix != EVENT_TYPES[event_type]:
        return False
    return pivot < 0 or level == pivot


def rollup_entries(event_type, pivot):
    """Every player's total over the boards of a rollup: ranked by the number of events
    they have a time on, then by the total of their best times (the score shown)."""
    STORE.load()
    players = {}
    for key, board in STORE.boards.items():
        if not in_rollup(key, event_type, pivot):
            continue
        for e in board.values():
            p = players.setdefault(e["xuid"], dict(e, score=0.0, boards=0))
            p["boards"] += 1
            p["score"] += e["score"]
            p["submitted"] = max(p["submitted"], e["submitted"])
            p["ghostFileId"] = -1
    out = sorted(players.values(), key=lambda e: (-e["boards"], e["score"]))
    return out


def online_xp_entries():
    """Every player by online XP (their profile's onlineXP, kept from UpdateAll)."""
    folder = os.path.join(DATA_DIR, "players")
    out = []
    for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
        if not name.endswith(".json"):
            continue  # .bak copies (forza_durable) and leftovers
        try:
            record = durable.load(os.path.join(folder, name))
        except (OSError, ValueError):
            continue
        xp = record.get("profile", {}).get("onlineXP", 0)
        if xp > 0:
            out.append({"xuid": name[:16], "gamertag": record.get("gamertag", name[:16]),
                        "score": float(xp), "submitted": record.get("profileUpdated", 0),
                        "carId": 0, "carClassId": 0, "carPerformanceIndex": 0.0,
                        "usedSTM": 0, "usedTCS": 0, "usedABS": 0, "usedAutoBrake": 0,
                        "usedAutoShifting": 0, "usedClutch": 0, "ghostFileId": -1,
                        "level": record.get("careerLevel", 0)})
    out.sort(key=lambda e: -e["score"])
    return out


def get_rollup_rows(xuid, body):
    p, _ = x.decode(x.ScoreboardGetRollupRows_Params, body)
    if p["eventTypeId"] in EVENT_TYPES and p["type"] == 0:
        entries = rollup_entries(p["eventTypeId"], p["pivotId"])
    else:
        entries = online_xp_entries()
    rows, user_row = rows_view(entries, xuid, p["view"], p["startAt"], p["maxResults"])
    log.info("title: GetRollupRows %016X type %d view %d pivot %d event type %d: %d rows", xuid,
             p["type"], p["view"], p["pivotId"], p["eventTypeId"], len(rows))
    return x.reply(x.ScoreboardGetRollupRows_Results,
                   {"rows": rows, "userRow": user_row, "available": len(entries)})


def pick_rival(entries, xuid, bigger):
    """The rival for a player: the score just better than theirs; without a score of
    their own, the slowest other player (an easy first rival)."""
    key = "%016X" % xuid
    others = [e for e in entries if e["xuid"] != key]
    if not others:
        return None
    mine = next((e for e in entries if e["xuid"] == key), None)
    if mine is None:
        return others[-1]
    ahead = [e for e in others if better(e["score"], mine["score"], bigger)]
    return ahead[-1] if ahead else others[0]


def rival_reply(xuid, body, with_best):
    p, _ = x.decode(x.ScoreboardGetRival_Params, body)
    # Rivals race events and the server's time trials: their boards are times.
    entries, bigger = STORE.ranked(EVENT_TIME, p["gameScoreboardId"], p["isServerTimeTrial"])
    if p["rivalXuid"]:
        rival = next((e for e in entries if int(e["xuid"], 16) == p["rivalXuid"]), None)
    else:
        rival = pick_rival(entries, xuid, bigger)
    mine = next((e for e in entries if e["xuid"] == "%016X" % xuid), None)
    log.info("title: rival for %016X on board %d (asked %016X): %s", xuid, p["gameScoreboardId"],
             p["rivalXuid"], "%s %.3f ghost %d" % (rival["gamertag"], rival["score"],
                                                  rival["ghostFileId"]) if rival else "none")
    if rival is None:
        result = {"rival": {"scoreData": NO_ROW, "bounty": 0}}
    else:
        result = {"rival": {"scoreData": row(rival, entries.index(rival) + 1),
                            "bounty": RIVAL_BOUNTY}}
    if with_best:
        result["personalBest"] = {"score": mine["score"] if mine else 0.0}
        return x.reply(x.ScoreboardGetRivalAndPersonalBest_Results, result)
    return x.reply(x.ScoreboardGetRival_Results, result)


def get_best_scores(xuid, body):
    p, _ = x.decode(x.ScoreboardGetBestScores_Params, body)
    entries, _ = STORE.ranked(p["scoreType"], p["gameScoreboardId"], p["isServerTimeTrial"])
    best = entries[0]["score"] if entries else 0.0
    log.info("title: GetBestScores %016X board %d type %d car %d class %d: %.3f", xuid,
             p["gameScoreboardId"], p["scoreType"], p["carId"], p["carClassId"], best)
    return x.reply(x.ScoreboardGetBestScores_Results, {"bestScore": {"score": best}})


AMBIENT_NAMES = {SPEED_TRAP: "Speed Trap %d", SPEED_ZONE: "Speed Zone %d"}


def notify_rival(xuid, body):
    """The game says it beat the rival's score: the rival gets a Message Center message
    (Rivals: "<player> beat your time")."""
    import forza_notifications as notifications
    p, _ = x.decode(x.ScoreboardNotifyRival_Params, body)
    log.info("title: %016X beat rival %016X on board %d (type %d)", xuid, p["rivalXuid"],
             p["gameScoreboardId"], p["type"])
    board = STORE.board(p["type"], p["gameScoreboardId"], p["isServerTimeTrial"])
    mine = board.get("%016X" % xuid)
    if not mine or not p["rivalXuid"] or p["rivalXuid"] == xuid:
        return x.reply()
    me = user_data(mine)
    trial = next((t for t in TIME_TRIALS if t[0] == p["gameScoreboardId"]), None) \
        if p["isServerTimeTrial"] else None
    # A speed trap or zone beaten: the game sends its board and type (1 or 2).
    name = trial[2] if trial else ""
    if not trial and p["type"] in AMBIENT_NAMES:
        name = AMBIENT_NAMES[p["type"]] % (p["gameScoreboardId"] % 100)
    notifications.send(p["rivalXuid"], notifications.RIVALS_BEATEN, {
        "senderXuid": xuid, "senderGamertag": mine["gamertag"],
        "senderClanTag": me.get("clanTag", ""), "senderClanRole": me.get("roleInClan", 0),
        "sentTime": time.time(), "timeTrialId": p["gameScoreboardId"],
        "carId": mine["carId"], "localizedTimeTrialName": name,
        "trackName": trial[5] if trial else "", "trackId": trial[4] if trial else 0,
        "score": mine["score"], "scoreType": p["type"], "category": trial[1] if trial else 0})
    return x.reply()


# Race Central's MONTHLY RIVALS: the server's time trials (GetTimeTrials). Each one is a
# scoreboard of its own (isServerTimeTrial, board = the trial id) and a round of
# TIME_TRIAL_HOPPER (Hopper.GetGameOptions(hopper, gameOptionsIndex): its track and laps).
# Categories (TimeTrial.str names FM4's: 0 time, 1 time with traffic, 2 drift, 3 Top Gear,
# 4 time spec, 5 weekly, 6 autocross): the MONTHLY RIVALS screen shows category 4 only
# (its CTimeTrialDataSource filters by the category at +0x20, which that screen sets to 4).
# With the trials in category 0 the screen had nothing to show and kept its YOUR RIVAL,
# BOUNTY and description spinners going.
TIME_TRIAL_HOPPER = 10
MONTHLY_CATEGORY = 4
TIME_TRIALS_ENABLED = False  # --time-trials
TIME_TRIALS = [
    # id, category, name, description, track id, track name, laps, car class (-1 any)
    (1001, MONTHLY_CATEGORY, "Horizon Monthly: Clear Springs", "Two laps of the Clear "
     "Springs Circuit, any car. Beat the other players' times.", 156,
     "Clear Springs Circuit", 2, -1),
    (1002, MONTHLY_CATEGORY, "Horizon Monthly: Festival Sprint", "One lap on the "
     "festival's road course in a class A car.", 322, "Festival Circuit", 1, 5),
]


def time_trial_rounds():
    """{gameOptionsIndex: (track, laps, class)} of TIME_TRIAL_HOPPER."""
    return {i: (t[4], t[6], t[7]) for i, t in enumerate(TIME_TRIALS)}


def get_time_trials(xuid, body):
    p, _ = x.decode(x.ScoreboardGetTimeTrials_Params, body)
    now = time.time()
    month = time.gmtime(now)
    start = time.mktime((month.tm_year, month.tm_mon, 1, 0, 0, 0, 0, 0, 0))
    end = start + 32 * 86400
    cats, trials = [], []
    for i, (tid, cat, name, desc, track, track_name, laps, cls) in enumerate(
            TIME_TRIALS if TIME_TRIALS_ENABLED else []):
        if not cats or cats[-1]["category"] != cat:
            cats.append({"category": cat, "startIndex": len(trials)})
        trials.append({"id": tid, "scoreType": 3, "name": name, "description": desc,
                       "trackId": track, "trackName": track_name,
                       "hopperId": TIME_TRIAL_HOPPER, "gameOptionsIndex": i,
                       "startDate": start, "endDate": end, "userRegistered": True,
                       "featured": i == 0, "active": True, "displayCarClass": cls})
    log.info("title: GetTimeTrials %016X (max %d categories, %d trials): %d", xuid,
             p["maxTimeTrialCategories"], p["maxTimeTrialResults"], len(trials))
    return x.reply(x.ScoreboardGetTimeTrials_Results, {
        "timeTrialCategories": cats[:p["maxTimeTrialCategories"]],
        "timeTrials": trials[:p["maxTimeTrialResults"]]})


# ---- /admin/scores: rows by source (operators only: forza_live_server.py, lsp.is_admin) ----
#   GET  /admin/scores?<filters>         the matching rows (at most ADMIN_LIST_MAX), their count
#   POST /admin/scores/unrank?<filters>  ranked rows off the boards, kept for review (and ghosts)
#   POST /admin/scores/rank?<filters>    unranked rows back on their boards, where better than
#                                        the player's row there (a kept ghost that passes too)
#   POST /admin/scores/delete?<filters>  rows and their ghosts removed
# Filters: server, build ("none": rows from before the tags), verified (0 or 1), player (an XUID in
# hex), board ("3:199:0" or the public id, event-199), reason (part of an unranked row's reason),
# state (ranked, unranked or all, the default). A change needs one filter besides state.
ADMIN_LIST_MAX = 500
ADMIN_FILTERS = ("server", "build", "verified", "player", "board", "reason")


def admin_filter(query):
    """(predicate over (state, board key, entry), state) of a query; ValueError when bad."""
    arg = {k: v[0] for k, v in query.items() if v}
    unknown = set(arg) - set(ADMIN_FILTERS) - {"state", "token"}
    if unknown:
        raise ValueError("unknown filter: " + ", ".join(sorted(unknown)))
    state = arg.get("state", "all")
    if state not in ("ranked", "unranked", "all"):
        raise ValueError("state: ranked, unranked or all")
    board = arg.get("board")
    if board is not None and not re.fullmatch(r"\d+:-?\d+:[01]", board):
        import forza_leaderboards
        parsed = forza_leaderboards.parse_board(board)
        if parsed is None:
            raise ValueError("board: 3:199:0 or event-199")
        board = board_key(*parsed)
    if "verified" in arg and arg["verified"] not in ("0", "1"):
        raise ValueError("verified: 0 or 1")
    player = arg.get("player", "").upper().removeprefix("0X")

    def tagged(entry, name):
        value = entry.get(name)
        return arg[name] == "none" if value is None else value == arg[name]

    def matches(where, key, entry):
        return ((state == "all" or state == where) and (board is None or key == board) and
                ("server" not in arg or tagged(entry, "server")) and
                ("build" not in arg or tagged(entry, "build")) and
                ("verified" not in arg or
                 bool(entry.get("verified", True)) == (arg["verified"] == "1")) and
                ("player" not in arg or entry.get("xuid") == player.zfill(16)) and
                ("reason" not in arg or arg["reason"] in entry.get("unranked", "")))
    return matches, any(k in arg for k in ADMIN_FILTERS)


def admin_rows(matches):
    STORE.load()
    for where, stored in (("ranked", STORE.boards), ("unranked", STORE.unranked)):
        for key, board in list(stored.items()):
            for player, entry in list(board.items()):
                if matches(where, key, entry):
                    yield where, key, entry


def admin_row(where, key, entry):
    return {"state": where, "board": key, "xuid": entry["xuid"],
            "gamertag": entry.get("gamertag"), "score": entry["score"],
            "carId": entry.get("carId"), "classId": entry.get("carClassId"),
            "submitted": entry.get("submitted"), "server": entry.get("server"),
            "build": entry.get("build"), "verified": entry.get("verified"),
            "unranked": entry.get("unranked"),
            "ghost": entry.get("ghostFileId", -1) >= 0 or bool(entry.get("reviewGhost"))}


def read_file(path):
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        return None


def take_off_board(key, entry):
    """Removes a ranked row (and a pending ghost of it); returns its ghost's bytes."""
    board = STORE.boards.get(key, {})
    if board.get(entry["xuid"]) is entry:
        del board[entry["xuid"]]
        if not board:
            del STORE.boards[key]
    STORE.ghosts.pop(entry.get("scoreId"), None)
    data = None
    if entry.get("ghostFileId", -1) >= 0:
        data = read_file(ghost_path(entry["ghostFileId"]))
        remove_ghost(entry["ghostFileId"])
    return data


def admin_change(action, key, where, entry):
    """One row through an action; True when it changed."""
    if action == "unrank" and where == "ranked":
        if not unrank(key, entry, "operator", take_off_board(key, entry)):
            log.info("title: /admin/scores/unrank: %s's %.3f on board %s removed (a better "
                     "unranked one of the player's is kept)", entry["xuid"], entry["score"], key)
        return True
    if action == "rank" and where == "unranked":
        board = STORE.board(*(int(v) for v in key.split(":")), create=True)
        if board is None:
            return False
        old = board.get(entry["xuid"])
        bigger = int(key.split(":")[0]) in BIGGER_IS_BETTER
        if old and not better(entry["score"], old["score"], bigger):
            return False
        del STORE.unranked[key][entry["xuid"]]
        if not STORE.unranked[key]:
            del STORE.unranked[key]
        data = read_file(review_ghost_path(entry["scoreId"])) if entry.get("reviewGhost") else None
        remove_review_ghost(entry)
        if old:
            take_off_board(key, old)
        row = {k: v for k, v in entry.items() if k not in ("unranked", "unrankedAt", "reviewGhost")}
        row["ghostFileId"] = -1
        problem, kept = ghostfile.check(data, row["score"], row["carId"], tracks_of(key)) \
            if data is not None else ("none", None)
        if problem is None:
            row["ghostFileId"] = STORE.next_file
            STORE.next_file += 1
            os.makedirs(os.path.dirname(ghost_path(row["ghostFileId"])), exist_ok=True)
            with open(ghost_path(row["ghostFileId"]), "wb") as f:
                f.write(kept)
        board[entry["xuid"]] = row
        return True
    if action == "delete":
        if where == "ranked":
            take_off_board(key, entry)
        else:
            del STORE.unranked[key][entry["xuid"]]
            if not STORE.unranked[key]:
                del STORE.unranked[key]
            remove_review_ghost(entry)
        return True
    return False


def admin(method, path, query):
    """(status, content type, body) for /admin/scores..., the caller already an operator."""
    action = path[len("/admin/scores"):].strip("/")
    if action not in ("", "unrank", "rank", "delete"):
        return 404, "text/plain", b"not found"
    if (method == "POST") != bool(action):
        return 405, "text/plain", b"GET lists rows, POST /admin/scores/<unrank|rank|delete> changes"
    try:
        matches, filtered = admin_filter(query)
    except ValueError as e:
        return 400, "application/json", json.dumps({"error": str(e)}).encode()
    rows = list(admin_rows(matches))
    if not action:
        return 200, "application/json", json.dumps({
            "server": server_id(), "matched": len(rows),
            "rows": [admin_row(*r) for r in rows[:ADMIN_LIST_MAX]]}, indent=1).encode()
    if not filtered:
        return 400, "application/json", json.dumps(
            {"error": "a change needs a filter: " + ", ".join(ADMIN_FILTERS)}).encode()
    changed = sum(admin_change(action, key, where, entry) for where, key, entry in rows)
    if changed:
        STORE.save()
        import forza_leaderboards
        forza_leaderboards.forget_answers()
    log.warning("title: /admin/scores/%s %s: %d of %d rows", action,
                urllib.parse.urlencode({k: v[0] for k, v in query.items() if k != "token"}),
                changed, len(rows))
    return 200, "application/json", json.dumps({"action": action, "matched": len(rows),
                                                "changed": changed}).encode()


def submit_score(gamertag_of, ex):
    def handler(xuid, body):
        if ex:
            p, _ = x.decode(x.ScoreboardSubmitScoreEx_Params, body)
            return submit(xuid, gamertag_of(xuid), p["type"], p["data"]["base"],
                          p["data"]["numCarsPassed"])
        p, _ = x.decode(x.ScoreboardSubmitScore_Params, body)
        return submit(xuid, gamertag_of(xuid), p["type"], p["data"])
    return handler


def services(gamertag_of):
    return {
        ("Scoreboard", "SubmitScore"): submit_score(gamertag_of, False),
        ("Scoreboard", "SubmitScoreEx"): submit_score(gamertag_of, True),
        ("Scoreboard", "UploadFile"): upload_file,
        ("Scoreboard", "DownloadFile"): download_file,
        ("Scoreboard", "GetRows"): get_rows,
        ("Scoreboard", "GetRollupRows"): get_rollup_rows,
        ("Scoreboard", "GetRival"): lambda xuid, body: rival_reply(xuid, body, False),
        ("Scoreboard", "GetRivalAndPersonalBest"): lambda xuid, body: rival_reply(xuid, body, True),
        ("Scoreboard", "GetBestScores"): get_best_scores,
        ("Scoreboard", "NotifyRival"): notify_rival,
        ("Scoreboard", "GetTimeTrials"): get_time_trials,
        ("Challenge", "ChallengeCompleted"): challenge_completed(gamertag_of),  # TU4
    }
