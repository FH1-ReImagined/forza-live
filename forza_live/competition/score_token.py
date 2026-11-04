"""Forza Live: score tokens (FH1 #517). A result counts only if the time since its event started
is at least the time it claims (osu!'s play tokens, SBRW's legitTime).

The game's protocol has no start call and no field for a token, but two calls the game already
makes mark a start, and both are signed with the player's own key (lsp.caller_trusted):

  - Scoreboard.GetBestScores(type 3, board, isServerTimeTrial, car, ...): an event's race asks
    it as it loads (seen for a festival race, its Rivals and RACE AGAIN). The scoreboard
    manager's race start (TU4 sub_82948E28, the call at 0x82948F1C; base 0x828976FC) sets the
    player's best to FLT_MAX and asks it; the answer is the best a lap must beat to be submitted
    (FH1 #569).
  - Scoreboard.GetRows(type 2, view 5, board, 1, 200): a speed zone's HUD board
    (CAmbientChallengeLeaderboard, TU4 sub_82979C60), fetched as the car enters the zone; the
    result (SubmitScore type 2, the average speed) follows at its far end.

Either call, from a signed-in caller, starts a token: the time it came, kept for that player
and board. Only the player's own signed calls start or use one, so the token is the server's
and the game never carries it. A result on the board spends the seconds it claims from the
token, and counts when everything the token has paid for, one result after another, fits in the
time since the start (plus TOLERANCE and SLACK of the claims):

  - a time (type 3): its seconds. A race submits its best lap, or a sprint its race time; the
    race takes longer than either.
  - a speed zone (type 2): the zone's gates' distance (ZONE_METRES, a straight line, so
    no more than the road between them) over the average speed.

So each result needs its own time on the clock, as a single-use token would, and a token is
not used up by its first result: RESTART on the results screen races the event again without
a new GetBestScores (seen in the game, a Rivals attempt), and its lap is paid for by the time
that has passed since. A new start replaces the token.

A result without a token, or faster than its token allows, is kept unranked ("start: ..."),
never an error to the game. A speed trap (type 1) is a speed at one point: no start, nothing
to time. Tokens live in memory: after a restart the server gives a result without one
NO_START_GRACE before it counts that as missing (a race started before the restart).
"""

import collections
import time

EVENT_TIME, SPEED_ZONE = 3, 2   # scores.EVENT_TIME, SPEED_ZONE
TOLERANCE = 2.0           # s: the start call's trip, a clock's rounding
SLACK = 0.02              # of the claimed time
TTL = 24 * 3600           # s a token lasts (RESTART after RESTART keeps the first start)
MAX_TOKENS = 100000       # players' open starts on the server
NO_START_GRACE = 1800     # s after the server's start when a missing token is let pass

# Speed zones (Colorado/average_speed.xml scoreboard_id): the straight line between the middles
# of the start gate (GameObjs.xml average_speed_NN_left1/right1) and the end gate (left2/right2),
# in metres, rounded down.
ZONE_METRES = {101: 645, 102: 576, 104: 437, 105: 1010, 106: 923, 107: 900, 108: 238,
               109: 582, 112: 399}

clock = time.monotonic
_tokens = collections.OrderedDict()   # (xuid, kind, board, stt) -> [started, seconds paid for]
_since = [clock()]                    # the server's start (configure)


def configure():
    _tokens.clear()
    _since[0] = clock()


def timed(kind):
    return kind in (EVENT_TIME, SPEED_ZONE)


def start(xuid, kind, sbid, stt, trusted):
    """A start of the player's event on a board: its token (replacing an older one). Only a
    signed-in caller's: an unverified result is unranked anyway, and a forged call must not move
    another player's start."""
    if not trusted or not timed(kind):
        return
    now = clock()
    key = (xuid, kind, sbid, bool(stt))
    _tokens.pop(key, None)
    _tokens[key] = [now, 0.0]
    while _tokens:
        oldest_key, (oldest, _) = next(iter(_tokens.items()))
        if len(_tokens) <= MAX_TOKENS and now - oldest <= TTL:
            break
        del _tokens[oldest_key]


def needed(kind, sbid, score):
    """The seconds a result claims (None: nothing to time)."""
    if kind == EVENT_TIME:
        return score
    if kind == SPEED_ZONE and sbid in ZONE_METRES and score > 0:
        return ZONE_METRES[sbid] / score
    return None


def check(xuid, kind, sbid, stt, score):
    """The reason the player's result on the board is unranked, or None; a result that counts
    spends its seconds from the token."""
    claim = needed(kind, sbid, score) if timed(kind) else None
    if claim is None:
        return None
    now = clock()
    key = (xuid, kind, sbid, bool(stt))
    token = _tokens.get(key)
    if token is not None and now - token[0] > TTL:
        del _tokens[key]
        token = None
    if token is None:
        if now - _since[0] < NO_START_GRACE:
            return None
        return "no start"
    elapsed, paid = now - token[0], token[1] + claim
    if elapsed + TOLERANCE + SLACK * paid < paid:
        if token[1]:
            return "%.1f s after its start, it needs %.1f s (%.1f s of it for earlier results)" % (
                elapsed, paid, token[1])
        return "%.1f s after its start, it needs %.1f s" % (elapsed, paid)
    token[1] = paid
    return None
