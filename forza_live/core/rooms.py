"""Rooms, and the converted worlds' rooms.

Players meet only the others in their room (the game's live_room setting or FORZA_ROOM, sent in
its HELLO; a Title Update 4 game adds "#TU4", so the default room of a TU4 game is "#TU4"). The
launcher plays Forza Horizon 2's and Fast & Furious's maps converted for FH1 (a "world") in a room
of its own: the player's room plus "+fh2" or "+anthem" (the launcher's fh2ffworld.world_room,
which keeps the ending whole within the game's 32 characters), so the players in a world meet
each other and never the players in Colorado.

A world is another map with other roads: the boards, the 1000 Club, the daily and weekly
challenges and the co-op campaign are Colorado's. The title services take nothing of them from
a player whose game is in a world (lsp.service_call answers those calls as accepted and keeps
nothing; docs/title_services.md, "Converted worlds"). The title calls come over HTTP and name
their player (X-UserId, signed); the room is the UDP session's, so server.py's player_world()
looks it up by XUID with world_of_sessions() below.
"""

import contextvars
import logging

log = logging.getLogger("forza-live")

# The world's key (the launcher's fh2ffworld KEY values) -> its name in the log.
WORLDS = {"anthem": "Fast & Furious", "fh2": "Forza Horizon 2"}
TU4 = "#TU4"
# A world session that left (BYE, timed out) still counts for this long when its player has no
# session at all: a call that comes between the game's last datagram and its next HELLO (a
# server restart, a lost connection) is still the world's. A new session of the player decides
# as soon as it says HELLO.
LEFT_KEEP = 600.0


def world_key(room):
    """"anthem" or "fh2" for a converted world's room, else None. Only a room that ENDS in
    "+anthem" or "+fh2" (before a Title Update 4 game's "#TU4"), exactly as the launcher writes
    it: the default room ("" or "#TU4") and the names players give their rooms are never caught.
    A room a player names "club+fh2" by hand is a world's room all the same: the server cannot
    tell it from the launcher's, and the players of the world "club" would be in it."""
    if not isinstance(room, str):
        return None
    if room.endswith(TU4):
        room = room[:-len(TU4)]
    for key in WORLDS:
        if room.endswith("+" + key):
            return key
    return None


def world_of_sessions(sessions, left=None, now=0.0):
    """Which world a player's game is in, from its sessions on the server.

    sessions: [(room, session id, last heard)] of the player's live sessions that count (the
    server leaves out a session that did not prove an XUID that has a key on record: anyone can
    say HELLO with someone else's XUID). left: (world key, session id, room, when) of the last
    world session of the player that left, or None.

    Returns (world key, session id, room) or None (Colorado). A player with a session in a world
    is in that world, also when it has another one in Colorado: a game in a world must never
    reach Colorado's records, and a second game on the same identity in Colorado loses its
    records for as long as both play (the newest-heard world decides between two worlds). A
    player with no session: the world session that left within LEFT_KEEP, else Colorado."""
    in_world = [(heard, key, sid, room) for room, sid, heard in sessions
                for key in [world_key(room)] if key]
    if in_world:
        _, key, sid, room = max(in_world, key=lambda s: s[0])
        return key, sid, room
    if sessions:
        return None
    if left is not None and now - left[3] < LEFT_KEEP:
        return left[0], left[1], left[2]
    return None


# --- the call being handled ---

# (world key, session id, room, XUID) of the title service call being handled, or None
# (lsp.service_call sets it for every call).
_call = contextvars.ContextVar("world_call", default=None)
_told = set()  # (session id, XUID, kind) already logged
TOLD_MAX = 50000


def set_call(found, xuid):
    """found: player_world()'s answer for the caller. Returns the token for reset_call."""
    return _call.set((found[0], found[1], found[2], xuid) if found else None)


def reset_call(token):
    _call.reset(token)


def call_world():
    """The world key of the call being handled ("anthem", "fh2"), or None (Colorado)."""
    c = _call.get()
    return c[0] if c else None


def left_out(kind, what, found=None, xuid=None):
    """Logs, once per session and kind, that something of Colorado's from a world's session was
    not kept: kind ("scores", "race stats", ...), what (the first call it was, for the log).
    found/xuid: the session's (world key, session id, room) and XUID, else the call's."""
    if found is None:
        c = _call.get()
        if c is None:
            return
        found, xuid = c[:3], c[3]
    key, sid, room = found
    told = (sid, xuid, kind)
    if told in _told:
        return
    if len(_told) >= TOLD_MAX:
        _told.clear()
    _told.add(told)
    log.info("world: %016X in room %r (%s's world): not kept, as Colorado's: %s (first: %s; "
             "told once per session)", xuid or 0, room, WORLDS.get(key, key), kind, what)
