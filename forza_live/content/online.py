"""Forza Live: how many players are online now, for the website's home page (FH1-ReImagined
website: "players online" next to the download button, read same-origin through the site's
/live/ pass-through, like the gallery).

Public route, on the title services port (no side effects):

  GET /community/online.json   {"version":1,"generated":<unix>,"players":<int>,"worlds":<int>,
                               "racing":<int>}; Cache-Control: public, max-age=FEED_SECONDS.
                               Other methods (HEAD too): 405, as the other community feeds.

  players  the games connected now (proxied or not), without probe registrations (room
           "probe", tools/server_probe.py): the server runs no players of its own, and the
           game's CPU drivers are never connections
  worlds   the open public Horizon Worlds: each published session of the Horizon World
           playlist once, a world whose host left kept by its members (the Discord message's
           count, social/discord_status.py); Co-op Festival worlds (friends only) are not in it
  racing   the players in a race or match: in a Races or Playground Games lobby, a custom
           game other than Free Roam (public or private), or a world whose event round is
           running (world.WORLDS.racing); each player once (a set of the sessions'
           members, whichever sessions it published), plus the filled slots a host's record
           has beyond its known members; at most `players`

Counts only: never a gamertag, XUID, player id, session id or address. Counted from the
server's memory (ForzaLive.lobby_rooms, the same lobby view as the Discord message and the
operator panel), at most once every MEMO_SECONDS whoever asks: every visitor's browser polls
it through the website's cache. No disk I/O.
"""
import json
import time

from forza_live.content import community
from forza_live.social import discord_status
from forza_live.world import world

PATH = "/community/online.json"
FEED_SECONDS = 15   # the feed's Cache-Control max-age
MEMO_SECONDS = 5.0  # the counts are made at most this often
FREE_ROAM = 8       # the Free Roam custom game type (discord_status.CUSTOM_GAME)

_memo = {"server": None, "at": 0.0, "body": b""}


def free_roam(playlist, game_type, world_playlists):
    """Whether a session (its playlist key and custom game type) is a free roam: a Horizon
    World or Co-op Festival world (Find Game: no game type; Find Custom Game's Point-to-Point
    searches with key 3 too, but carries its game type), or a Free Roam custom game."""
    if game_type == FREE_ROAM:
        return True
    return playlist in world_playlists and game_type != playlist


def counts(status, racing_worlds, world_playlists):
    """(players, worlds, racing) of a status()-shaped dict (its "rooms"; lobby_rooms is
    enough). racing_worlds: the session ids (hex) of the worlds whose event round runs;
    world_playlists: the world playlists' keys (lsp.WORLD_HOPPERS)."""
    players = discord_status.visible_players(status)
    worlds = unnamed = 0
    racers = set()  # each player once: its match record and a standby record can be two sessions
    for (room, sid), s in discord_status.sessions(status).items():
        if s["name"] == discord_status.FIND_GAME[3]:
            worlds += 1
        if sid in racing_worlds or not free_roam(s["playlist"], s["game_type"], world_playlists):
            racers.update((room, member) for member in s["members"])
            unnamed += discord_status.session_players(s) - len(s["members"])  # host's slots only
    return players, worlds, min(len(racers) + unnamed, players)


def feed(server, world_playlists, now=None):
    """The feed's body (bytes), counted again only after MEMO_SECONDS."""
    now = time.monotonic() if now is None else now
    if _memo["server"] is server and 0 <= now - _memo["at"] < MEMO_SECONDS:
        return _memo["body"]
    players = worlds = racing = 0
    if server is not None:
        racing_worlds = {sid.hex() for sid in world.WORLDS.racing}
        players, worlds, racing = counts({"rooms": server.lobby_rooms()}, racing_worlds,
                                         world_playlists)
    body = json.dumps({"version": 1, "generated": int(time.time()), "players": players,
                       "worlds": worlds, "racing": racing}, separators=(",", ":")).encode()
    _memo.update(server=server, at=now, body=body)
    return body


def quiet(path, status):
    """Whether the request log skips this answer: the feed is polled through the website's
    cache, so only its errors (status 400 and up) are logged."""
    return status < 400 and path.split("?", 1)[0] == PATH


def handle(server, method, world_playlists):
    """(status, content type, body, extra headers) for GET /community/online.json."""
    if method != "GET":
        return community.reply(405, {"error": "GET only"})
    extra = community.SAFE + ("Cache-Control: public, max-age=%d" % FEED_SECONDS,)
    return 200, "application/json", feed(server, world_playlists), extra
