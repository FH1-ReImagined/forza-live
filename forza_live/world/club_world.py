"""Car clubs in the Horizon World (FH1 #145): landing in the same world as your clubmates.

The server cannot move a running game into another world: a game is placed when it searches
(Find Game > Horizon World, MATCH_SEARCH), and the answer is the list of open worlds, the
one it should join first. So a club only changes the order of that list:

- Placement prefers the worlds with one of the searcher's online clubmates in them (the
  world with the most clubmates first), then the fullest, as before. Only worlds that have
  room are in the list at all, so a full world of clubmates changes nothing. Searchers
  without a club, or without a clubmate in an open world, are placed exactly as before.
- /club (in any chat channel) lists the clubmates who are online and where they are.
- /club join [gamertag] puts the world of that clubmate (or the one with the most clubmates)
  first for the next 10 minutes, for a player who wants one clubmate's world among several.
  It needs the player to be out of the world they are in: they open Find Game > Horizon
  World again, as for any placement.

Both sides must have proven their XUID (friends key, as for chat): a HELLO's XUID alone could
be anyone's, and a world is shown to a club as "where the clubmate is". Blocks hide a
player either way, as in chat. Only the Horizon World playlist is ordered; the Co-op Festival
keeps its friends-only placement. Nothing here is written to disk.
"""

import time

from forza_live.social import chat
from forza_live.social import clubs
from forza_live.title_services import lsp
from forza_live.world import world

JOIN_SECONDS = 600
MAX_LISTED = 8

USAGE = ("Club commands: /club lists your clubmates who are online and where; /club join "
         "[gamertag] places you in a clubmate's Horizon World when you open Find Game > "
         "Horizon World")
NOT_IN_CLUB = chat.NO_CLUB


def proven(peer):
    return bool(getattr(peer, "friends_ok", False))


def clubmates(server, peer):
    """The players online now in the same car club as `peer`: [peer], their XUIDs proven,
    not blocked either way. Empty for a player who has no club or has not proven the XUID."""
    if not proven(peer):
        return []
    club_id = chat.Chat.club_of(peer.xuid)[0]
    if not club_id:
        return []
    store = server.friends.store
    return [p for p in server.peers.values()
            if p.xuid != peer.xuid and proven(p) and chat.Chat.club_of(p.xuid)[0] == club_id
            and not store.blocks(peer.xuid, p.xuid) and not store.blocks(p.xuid, peer.xuid)]


class Preference:
    """How a search for the Horizon World orders the open worlds: the world a /club join
    named, then the one with the most clubmates in it."""

    def __init__(self, counts, target):
        self.counts = counts  # session id -> clubmates in it
        self.target = target  # session id of a /club join, or None

    def mates(self, record):
        return self.counts.get(bytes(record[:8]), 0)

    def rank(self, record):
        sid = bytes(record[:8])
        return (0 if sid == self.target else 1, -self.counts.get(sid, 0))


NONE = Preference({}, None)


def preference(server, peer, key):
    """The Preference for a search of `peer` for playlist `key`: NONE (no change in the
    order) unless it is the Horizon World and a clubmate is in a world."""
    if key != lsp.WORLD_HOPPER:
        return NONE
    counts = {}
    for mate in clubmates(server, peer):
        sid = server.world_of_peer(mate.id)
        if sid is not None:
            counts[sid] = counts.get(sid, 0) + 1
    join = getattr(peer, "club_join", None)
    target = join[0] if join and join[1] > time.monotonic() else None
    return Preference(counts, target) if counts else NONE


def where(server, peer, mate, worlds):
    """What /club says about a clubmate: where they are."""
    if mate.room != peer.room:
        return "online in another room"
    sid = server.world_of_peer(mate.id)
    if sid is not None and sid in worlds:
        humans = worlds[sid][1]
        if server.world_keys.get(sid) == lsp.WORLD_HOPPER:
            return "in a Horizon World (%d/%d%s)" % (
                humans, world.MAX_PLAYERS, ", full" if humans >= world.MAX_PLAYERS else "")
        if server.world_keys.get(sid) == lsp.COOP_HOPPER:
            return "in a Co-op Festival world (%d/%d)" % (humans, world.MAX_PLAYERS)
    return "online"


def who(server, peer, club_id):
    notice = server.chat.notice
    tag = chat.Chat.club_of(peer.xuid)[1]
    mates = clubmates(server, peer)
    members = len(clubs.members_of(club_id))
    if not mates:
        notice(peer, "[%s] No other club member is online (%d members)" % (tag, members))
        return
    worlds = server.world_sessions()
    rows = sorted(((where(server, peer, m, worlds), m.name) for m in mates),
                  key=lambda r: (not r[0].startswith("in a Horizon"), r[0], r[1].lower()))
    notice(peer, "[%s] %d of %d members online:" % (tag, len(mates), members))
    for place, name in rows[:MAX_LISTED]:
        notice(peer, "  %s: %s" % (name, place))
    if len(rows) > MAX_LISTED:
        notice(peer, "  and %d more" % (len(rows) - MAX_LISTED))


def join(server, peer, name):
    """/club join [gamertag]: put a clubmate's Horizon World first in the next search."""
    notice = server.chat.notice
    mates = [m for m in clubmates(server, peer) if m.room == peer.room]
    if name:
        named = [m for m in mates if m.name.lower() == name.lower()]
        if not named:
            notice(peer, "%s is not a club member who is online in your room (/club lists them)"
                   % name[:24])
            return
        mates = named
    mine = server.world_of_peer(peer.id)
    in_world = {server.world_of_peer(m.id): m for m in mates}
    if mine is not None and mine in in_world:
        notice(peer, "You are already in %s's world" % in_world[mine].name)
        return
    # the worlds the search would offer once this player is out of its own
    offered = {bytes(record[:8]): (host, record) for host, record, _ in
               server.match_candidates(peer, lsp.WORLD_HOPPER, ignore_own=True)}
    counts = {}
    for mate in mates:
        sid = server.world_of_peer(mate.id)
        if sid in offered:
            counts[sid] = counts.get(sid, 0) + 1
    if not counts:
        if name:
            notice(peer, "%s is not in a Horizon World with room (/club shows where they are)"
                   % mates[0].name)
        else:
            notice(peer, "None of your clubmates is in a Horizon World with room "
                         "(/club shows where they are)")
        return
    sid = max(counts, key=lambda s: (counts[s], server.match_slots(offered[s][1])[0]))
    peer.club_join = (sid, time.monotonic() + JOIN_SECONDS)
    names = sorted(m.name for m in mates if server.world_of_peer(m.id) == sid)
    filled = server.match_slots(offered[sid][1])[0]
    notice(peer, "Open Find Game > Horizon World to join %s (%d/%d)%s" % (
        ", ".join(names[:3]) + (" and %d more" % (len(names) - 3) if len(names) > 3 else ""),
        filled, world.MAX_PLAYERS,
        "; leave your current world first" if mine is not None else ""))


def command(server, peer, text):
    """chat_command for a line starting with /club: always True (answered with notices)."""
    parts = text.split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else "who"
    club_id = chat.Chat.club_of(peer.xuid)[0]
    if not club_id:
        server.chat.notice(peer, NOT_IN_CLUB)
    elif sub in ("who", "online", "list"):
        who(server, peer, club_id)
    elif sub == "join":
        join(server, peer, parts[2].strip() if len(parts) > 2 else "")
    else:
        server.chat.notice(peer, USAGE)
    return True
