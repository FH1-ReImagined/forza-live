"""Forza Live: an optional "players online" message in a Discord channel.

Set FORZA_DISCORD_STATUS_WEBHOOK to a channel webhook URL. The server posts one message
there and edits it whenever the player count, the number of open Horizon Worlds or the
online lobbies change (each lobby's playlist or custom game type and its players, so
players can see where everyone is), and every 10 minutes, so its "updated" time stays
fresh; on shutdown it marks the server offline. Webhooks may edit their own messages, so no bot is needed. The message id is kept
in DATA_DIR/discord_status.json, so a restart edits the same message instead of posting a
new one. Probe registrations (room "probe") are not counted. With volunteer relays listed
(network/relay_broker.py), a line shows each one's region, whether it is up and the sessions
it relays; a relay going up or down edits the message. The community servers of the server
list (core/server_list.py, DATA_DIR/server_list.json) get a line each, with their players and
open Horizon Worlds from their own /status, checked every COMMUNITY_POLL seconds. With daily
challenges on (competition/objectives.py), a short Challenges block lists today's and this
week's objectives and how many players finished each; a finished objective edits the message. No
other message is posted for them.
"""
import concurrent.futures
import json
import logging
import os
import time
import urllib.error
import urllib.request
from forza_live.core import durable, server_list

log = logging.getLogger("forza-live")

WEBHOOK = os.environ.get("FORZA_DISCORD_STATUS_WEBHOOK", "").strip()
PUBLIC_ADDRESS = os.environ.get("FORZA_PUBLIC_ADDRESS", "forza-reimagined.playit.plus:56677")
# What the message calls this server: a community server sets its own ("US East"); unset, it is
# the public server, the one the launcher uses by default.
SERVER_NAME = os.environ.get("FORZA_DISCORD_STATUS_NAME", "").strip()[:48]
HEARTBEAT = 600  # seconds between edits when nothing changed
HIDDEN_ROOMS = {"probe"}
COMMUNITY_POLL = 120     # seconds between checks of the listed community servers
COMMUNITY_TIMEOUT = 4
MAX_COMMUNITY = 12       # servers checked and shown
MAX_STATUS = 4 << 20     # bytes of a community server's /status read
MAX_MESSAGE = 2000         # characters Discord takes in a message

_state = {"key": None, "sent": 0.0, "polled": None, "community": []}


def enabled():
    return WEBHOOK.startswith("https://discord.com/api/webhooks/")


# Find Game's playlists (lsp.py HOPPERS) and the game types a public
# custom game is found by (docs/title_services.md, Find Custom Game). A private custom game
# publishes 0xFFFFFFFF, which nobody can search for.
FIND_GAME = {3: "Horizon World", 1: "Races", 2: "Playground Games"}
CUSTOM_GAME = {2: "Circuit Race", 3: "Point-to-Point", 4: "Street Race", 5: "Infected",
               6: "Cat and Mouse", 7: "King", 8: "Free Roam", 9: "Rally"}
PRIVATE = 0xFFFFFFFF
MAX_LOBBY_LINES = 12


def lobby_name(playlist, game_type):
    if playlist == PRIVATE:
        return "Private custom games"
    if game_type == playlist and playlist in CUSTOM_GAME:
        return f"Custom {CUSTOM_GAME[playlist]}"
    return FIND_GAME.get(playlist) or CUSTOM_GAME.get(playlist) or f"Playlist {playlist}"


def sessions(status):
    """{(room, session id hex): {"members", "filled", "slots", "playlist", "game_type",
    "name", "hosted"}}: each published session once, without probe registrations, described
    by its host's record (else by a member's standby record: a session whose host left is
    kept by its members' standby records). members: the ids of the players who published
    it (host or member)."""
    found = {}
    for room, peers in status.get("rooms", {}).items():
        if room in HIDDEN_ROOMS:
            continue
        for peer in peers:
            for kind in ("match", "standby"):
                m = peer.get(kind)
                if not m:
                    continue
                key = (room, m["session"])
                s = found.setdefault(key, {"members": set(), "filled": 0, "slots": 0,
                                           "playlist": None, "game_type": None,
                                           "name": None, "hosted": False})
                s["members"].add(peer.get("id"))
                if kind == "match" or not s["hosted"]:
                    s["playlist"], s["game_type"] = m["playlist"], m.get("game_type")
                    s["name"] = lobby_name(m["playlist"], m.get("game_type"))
                    s["slots"] = m.get("slots", 0)
                    if kind == "match":
                        s["hosted"], s["filled"] = True, m.get("players", 0)
    return found


def session_players(s):
    """A session's players (sessions()): the most of its host's filled slots and the
    players who published it."""
    return max(s["filled"], len(s["members"]))


def lobbies(status):
    """{lobby name: [(players, slots), ...]}: each published session once (sessions()), its
    players as session_players() counts them."""
    out = {}
    for s in sessions(status).values():
        players = session_players(s)
        out.setdefault(s["name"], []).append((players, max(s["slots"], players)))
    for name in out:
        out[name].sort(reverse=True)
    return out


def summary(status):
    """(players, worlds, lobbies) from the server's status() dict, without probe
    registrations; lobbies as lobbies() gives them. The worlds are its Horizon World
    sessions: status()["worlds"] also counts a custom lobby whose members last searched
    the Horizon World playlist."""
    found = lobbies(status)
    return visible_players(status), len(found.get(FIND_GAME[3], [])), found


def visible_players(status):
    """The players of a status() dict, without probe registrations."""
    return sum(len(peers) for room, peers in status.get("rooms", {}).items()
               if room not in HIDDEN_ROOMS)


def render_lobbies(found):
    """One line per kind of lobby, the busiest first."""
    order = sorted(found.items(), key=lambda kv: (kv[0] == "Private custom games",
                                                  -sum(p for p, _ in kv[1]), kv[0]))
    lines = []
    for name, sessions in order[:MAX_LOBBY_LINES]:
        total = sum(p for p, _ in sessions)
        who = "1 player" if total == 1 else f"{total} players"
        if name == "Private custom games":
            lines.append(f"• {name}: {who}")
        else:
            rooms = " · ".join(f"{p}/{n}" for p, n in sessions[:6])
            more = f" (+{len(sessions) - 6} more)" if len(sessions) > 6 else ""
            lines.append(f"• **{name}**: {who} — {rooms}{more}")
    return lines


def relay_states(status):
    """[(region or name, up, sessions now, sessions relayed)] of the volunteer relays in the
    server's status() (none without relays)."""
    return [(r.get("region") or r.get("name") or r.get("id", "?"), bool(r.get("up")),
             r.get("sessions", 0), r.get("sessions_relayed", 0))
            for r in status.get("relays", [])]


def render_relays(states):
    parts = []
    for where, up, sessions, relayed in states:
        if not up:
            parts.append(f"🔴 {where}: down")
        else:
            now_text = "1 session" if sessions == 1 else f"{sessions} sessions"
            parts.append(f"🟢 {where}: {now_text} ({relayed} relayed)")
    return "**Relays**: " + " · ".join(parts)


CHALLENGE_SETS = {"daily": "Today's challenges", "weekly": "This week's challenges"}


def challenge_states(now=None):
    """((kind, ends, ((title, players who finished), ...)), ...) of this server's current
    challenges; () when they are off."""
    from forza_live.competition import objectives
    if not objectives.ENABLED:
        return ()
    try:
        doc = objectives.document(None, now)
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("discord status: challenges: %s", e)
        return ()
    return tuple((s["kind"], s["ends"], tuple((o["title"], o["completed"]) for o in s["objectives"]))
                 for s in doc["sets"])


def render_challenges(states):
    """One line per set: its objectives and how many players finished each."""
    lines = []
    for kind, ends, objectives in states:
        parts = " · ".join(f"{title} ({done} done)" for title, done in objectives)
        lines.append(f"🏁 **{CHALLENGE_SETS.get(kind, kind)}** (until <t:{ends}:R>): {parts}")
    return lines


def render(players, worlds, online=True, now=None, found=None, relays=None, community=None,
           challenges=None):
    """The message: this server's block, then each community server's in the same form, then this
    server's challenges."""
    now = int(now if now is not None else time.time())
    lines = render_server(SERVER_NAME, PUBLIC_ADDRESS, online, players, worlds, found,
                          since=now)
    for name, address, up, c_players, c_worlds, c_found in community or ():
        lines.append("")
        lines += render_server(name, address, up, c_players, c_worlds,
                               {k: list(v) for k, v in c_found})
    lines.append("")
    if challenges and online:
        lines += render_challenges(challenges)
    if relays and online:
        lines.append(render_relays(relays))
    if SERVER_NAME:
        lines.append(f"Pick it on the launcher's Servers page · updated <t:{now}:R>")
    elif community:
        lines.append("The first server is the launcher's default. The others are community "
                     "servers with their own friends lists and leaderboards: pick one on the "
                     f"launcher's Servers page · updated <t:{now}:R>")
    else:
        lines.append(f"Set by default in the launcher · updated <t:{now}:R>")
    text = "\n".join(lines)
    if len(text) > MAX_MESSAGE:  # Discord's limit: drop whole lines from the end of the blocks
        footer = lines[-1]
        while len("\n".join(lines[:-1] + ["…", footer])) > MAX_MESSAGE and len(lines) > 2:
            lines.pop(-2)
        text = "\n".join(lines[:-1] + ["…", footer])
    return text


def status_url(address):
    """http://host:<port + 1>/status of a server address as players type it (host[:port], the
    UDP port, 21200 by default; the HTTP port is the next one)."""
    host, port = address, 21200
    if address.startswith("["):
        end = address.index("]")
        host, rest = address[:end + 1], address[end + 1:]
        if rest:
            port = int(rest[1:])
    elif ":" in address:
        host, port = address.rsplit(":", 1)
        port = int(port)
    return f"http://{host}:{port + 1}/status"


def fetch_status(address, timeout=COMMUNITY_TIMEOUT):
    """A listed server's /status as a dict, or None when it does not answer with one."""
    try:
        req = urllib.request.Request(status_url(address),
                                     headers={"User-Agent": "forza-live (status)"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(MAX_STATUS + 1)
        status = json.loads(body) if len(body) <= MAX_STATUS else None
        return status if isinstance(status, dict) else None
    except Exception:
        return None


def community_states(data_dir, now=None, fetch=fetch_status):
    """[(name, address, up, players, worlds, lobbies)] of the community servers in the server
    list (not this one), checked at most every COMMUNITY_POLL seconds: in between, the last
    answers. lobbies is lobbies() of its status as sorted ((name, ((players, slots), ...)), ...)."""
    now = now if now is not None else time.time()
    if _state["polled"] is not None and now - _state["polled"] < COMMUNITY_POLL:
        return _state["community"]
    _state["polled"] = now
    try:
        entries, _ = server_list.load_entries(os.path.join(data_dir, server_list.LIST_FILE))
    except (OSError, ValueError):
        entries = []
    entries = [e for e in entries if not e.get("main")][:MAX_COMMUNITY]
    states = []
    if entries:
        with concurrent.futures.ThreadPoolExecutor(max_workers=len(entries)) as pool:
            answers = list(pool.map(lambda e: fetch(e["address"]), entries))
        for entry, status in zip(entries, answers):
            players = worlds = 0
            found = ()
            if status is not None:
                try:
                    players, worlds, by_name = summary(status)
                    found = tuple(sorted((k, tuple(v)) for k, v in by_name.items()))
                except (AttributeError, KeyError, TypeError, ValueError):
                    pass  # another server's status must never stop this message
            states.append((entry["name"], entry["address"], status is not None, players, worlds,
                           found))
    _state["community"] = states
    return states


def render_server(name, address, up, players=0, worlds=0, found=None, since=None):
    """One server's block: the same for this server and each community server."""
    title = "Forza Horizon 1: ReImagined" + (f" — {name}" if name else "")
    lines = [f"{'🟢' if up else '🔴'} **{title}** — IP: `{address}`"]
    if not up:
        lines.append("Offline. Back soon" + (f" · since <t:{since}:R>" if since else ""))
        return lines
    who = "1 player online" if players == 1 else f"{players} players online"
    open_worlds = "1 Horizon World open" if worlds == 1 else f"{worlds} Horizon Worlds open"
    lines.append(f"**{who}** · {open_worlds}")
    if found:
        in_lobbies = sum(p for sessions in found.values() for p, _ in sessions)
        lines.append("**Online lobbies** (players/slots, join through Find Game or "
                     "Find Custom Game):")
        lines += render_lobbies(found)
        rest = max(players - in_lobbies, 0)
        if rest:
            lines.append(f"• Single player or menus: {rest}")
    else:
        lines.append("No online lobbies open right now: start one from Find Game!")
    return lines


def _id_path(data_dir):
    return os.path.join(data_dir, "discord_status.json")


def _request(method, url, content):
    body = json.dumps({"content": content, "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url, body, method=method,
                                 headers={"Content-Type": "application/json",
                                          "User-Agent": "forza-live (status)"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read() or b"{}")


def post(data_dir, content):
    """Edit the kept message, or post a new one (and keep its id). Never raises."""
    try:
        msg_id = None
        try:
            msg_id = durable.load(_id_path(data_dir)).get("id")
        except (OSError, ValueError):
            pass
        if msg_id:
            try:
                _request("PATCH", f"{WEBHOOK}/messages/{msg_id}", content)
                return True
            except urllib.error.HTTPError as e:
                if e.code != 404:  # deleted message: post a new one below
                    raise
        msg = _request("POST", WEBHOOK + "?wait=true", content)
        with open(_id_path(data_dir), "w") as f:
            json.dump({"id": msg.get("id")}, f)
        return True
    except Exception as e:  # network trouble must never affect the game server
        log.warning("discord status: %s", e)
        return False


def due(key, now=None):
    """Whether the message needs an edit now (what it shows changed, or the heartbeat is
    due)."""
    now = now if now is not None else time.time()
    return key != _state["key"] or now - _state["sent"] >= HEARTBEAT


def update(status, data_dir, now=None):
    """Called from the server loop; edits only when due."""
    if not enabled():
        return
    players, worlds, found = summary(status)
    states = relay_states(status)
    community = community_states(data_dir, now)
    challenges = challenge_states(now)
    key = (players, worlds, tuple(sorted((k, tuple(v)) for k, v in found.items())),
           tuple((where, up) for where, up, _, _ in states),  # sessions: at the heartbeat
           tuple(community), challenges)
    if not due(key, now):
        return
    if post(data_dir, render(players, worlds, now=now, found=found, relays=states,
                             community=community, challenges=challenges)):
        _state["key"], _state["sent"] = key, (now if now is not None else time.time())


def offline(data_dir):
    if enabled():
        post(data_dir, render(0, 0, online=False, community=_state["community"]))
