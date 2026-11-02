"""Text chat on the Forza Live server (#92, step 2): the session or party a player is in,
the Horizon World, and direct messages between friends.

The server only relays: a message goes to the players online now and is gone. For
moderation it keeps the last RING messages in memory (time, sender, channel, recipient,
text; never written to disk), which the admin reads at /admin/chat. Admin actions (with
the admin token, see forza_live_lsp.is_admin): /admin/mute?who=<gamertag|XUID>&minutes=N
(0 lifts it) stops a player's messages, /admin/kick?who=...&minutes=N drops the player and
refuses its sign-in for N minutes (a ban; 0 lifts it). Mutes and bans are kept in
DATA_DIR/moderation.json until they run out. Players mute others for themselves (MUTE;
kept in friends.json with their friends); a player's blocks (forza_friends.py) hide its
messages too, both ways.

Games say they have chat with HELLO features 0x04 (FEATURE); games without it neither send
nor get messages.

Channels: SESSION, the players in a session with the sender (the game reports the ids of
the sessions it hosts or joined, Horizon World sessions excepted); WORLD, the players in the
sender's Horizon World or Co-op Festival world (what matchmaking knows); DIRECT, one friend;
CLUB, every other member of the sender's car club (forza_clubs.py) who is online, wherever
they are (rooms and worlds do not matter, as for DIRECT). A game says it knows the CLUB
channel by asking for it (CLUB below) and is told the tag of the club it is in (CLUBTAG),
so older games neither show the channel nor get club messages.
A line starting with "/" may be a server command instead (Chat.commands: /event and /events
in a Co-op Festival world, forza_campaign.py; /club, forza_club_world.py); commands are
answered with NOTICEs.

Message CHAT (UDP kind 23), body op u8 then (big endian; text u16 length + UTF-8):
  client -> server
    1 SESSIONS count u8, session ids u64 (at most 4; sent when they change and every 10 s)
    2 SAY      channel u8, friend's XUID u64 (DIRECT; else 0), text
    3 MUTE     xuid u64, on u8
    4 CLUB     (no more data; sent with SESSIONS every 10 s) asks for CLUBTAG
    5 CONTENT  text (u16 length + UTF-8): what this game has that changes which cars exist,
               "dlc=729,732;mods=Car pack,Traffic unlocker" (offers of the installed DLC
               packages, names of the loaded mods that change cars or the database; sent at
               sign-in and every 30 s). The server compares it with the other players of each
               of the sender's sessions and tells both sides what they do not share
               (check_content): a car one game lacks is a plain placeholder in the other (#567,
               the first step of mod sync, #152). Kept in memory only, not in any file.
  server -> client
    16 MESSAGE channel u8, sender XUID u64, sender gamertag (u8 length + UTF-8), text
    17 NOTICE  text
    18 MUTED   count u16, XUIDs u64 (the players this one muted; after every change and on
               request: SESSIONS with count 0xFF asks)
    19 CLUBTAG tag (u8 length + UTF-8; length 0: no club) of the player's car club, after CLUB
               when it differs from the last one sent (so also the first time)
"""

import collections
import json
import logging
import os
import struct
import unicodedata
import time

import forza_auth as auth
import forza_clubs as clubs
import forza_friends as friends
import forza_limits as limits
import forza_durable as durable

log = logging.getLogger("forza-live")

KIND = 23
FEATURE = 0x04
SESSIONS, SAY, MUTE, CLUB_SYNC, CONTENT = 1, 2, 3, 4, 5
R_MESSAGE, R_NOTICE, R_MUTED, R_CLUBTAG = 16, 17, 18, 19
SESSION, WORLD, DIRECT, CLUB = 1, 2, 3, 4
CHANNELS = {SESSION: "session", WORLD: "world", DIRECT: "direct", CLUB: "club"}

MAX_CHARS = 200
MAX_BYTES = 800
NO_WORLD = "World chat is for a Horizon World or Co-op Festival world (Find Game): use Session chat here"
NO_CLUB = "You are not in a car club: join or start one in Multiplayer > Car Clubs"
NO_CLUBMATE = "No other member of your club is online with club chat"
RATE = (0.5, 5)  # messages per second and burst, per player
RING = 200
MAX_SESSIONS = 4
MAX_MUTED = 200
MAX_CONTENT_BYTES = 700     # the game's manifest is at most this long (kernel/content_manifest.h)
MAX_CONTENT_ITEMS = 120
SHOWN_ITEMS = 3             # content items named in one notice
CONTENT_CHANGE_SECONDS = 20  # at most one change of a player's content in this time
FILE = "moderation.json"


def text16(s):
    b = s.encode()
    return struct.pack(">H", len(b)) + b


def clean(text):
    """The text as it is relayed: no control characters, one line, at most MAX_CHARS."""
    # Control and format characters (C0/C1, bidi overrides like U+202E, zero-width) become
    # spaces, so a message can't hide or reverse text in other players' overlays.
    # Combining marks (Zalgo text) are dropped.
    text = "".join(" " if unicodedata.category(c) in ("Cc", "Cf", "Zl", "Zp") else c
                   for c in text if unicodedata.category(c) not in ("Mn", "Me"))
    return " ".join(text.split())[:MAX_CHARS]


def parse_content(text):
    """The game's content manifest, "dlc=729,732;mods=A,B": (frozenset of DLC offers, frozenset of
    mod names), or None when it is not one."""
    dlc, mods = set(), set()
    seen = set()
    for part in text.split(";"):
        key, sep, value = part.partition("=")
        if not sep or key not in ("dlc", "mods") or key in seen:
            return None
        seen.add(key)
        items = [v for v in value.split(",") if v]
        if len(items) > MAX_CONTENT_ITEMS:
            return None
        if key == "dlc":
            for v in items:
                if not v.isdigit() or len(v) > 9:
                    return None
                dlc.add(int(v))
        else:
            mods = {clean(v)[:32] for v in items if clean(v)}
    if seen != {"dlc", "mods"}:
        return None
    return frozenset(dlc), frozenset(mods)


def content_names(items):
    """Content items as players read them: DLC offers as "DLC 3060", mods by name; the first
    SHOWN_ITEMS, then how many more."""
    names = sorted("DLC %d" % i for i in items if isinstance(i, int)) + \
        sorted(i for i in items if isinstance(i, str))
    text = ", ".join(names[:SHOWN_ITEMS])
    if len(names) > SHOWN_ITEMS:
        text += " and %d more" % (len(names) - SHOWN_ITEMS)
    return text


def tagged(content):
    """The manifest as one set: offers as ints, mods as strings."""
    dlc, mods = content
    return set(dlc) | set(mods)


class Chat:
    def __init__(self, server, friend_store, path=None):
        self.server = server
        self.store = friend_store
        self.path = path
        self.ring = collections.deque(maxlen=RING)
        self.mutes = {}  # xuid -> until (wall time): the admin's
        self.bans = {}   # xuid -> until
        self.buckets = {}
        # commands(peer, channel, text) -> True when a "/" line was a server command
        # (forza_live_server.chat_command: the Co-op Festival's /event), not relayed
        self.commands = None

    # --- moderation state ---

    def load(self):
        if not self.path:
            return
        try:
            data = durable.load(self.path)
            now = time.time()
            self.mutes = {int(k, 16): v for k, v in data.get("mutes", {}).items() if v > now}
            self.bans = {int(k, 16): v for k, v in data.get("bans", {}).items() if v > now}
        except FileNotFoundError:
            pass
        except (OSError, ValueError, AttributeError, TypeError) as e:
            log.warning("cannot read %s: %s", self.path, e)

    def save(self):
        if not self.path:
            return
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump({"mutes": {"%016X" % k: v for k, v in self.mutes.items()},
                           "bans": {"%016X" % k: v for k, v in self.bans.items()}}, f)
            durable.replace(tmp, self.path)
        except OSError as e:
            log.warning("cannot write %s: %s", self.path, e)

    def banned(self, xuid):
        until = self.bans.get(xuid)
        if until and until <= time.time():
            del self.bans[xuid]
            until = None
        return until

    def admin_muted(self, xuid):
        until = self.mutes.get(xuid)
        if until and until <= time.time():
            del self.mutes[xuid]
            until = None
        return until

    def find(self, who):
        """XUIDs a gamertag or hex XUID names (online players and players the server
        knows)."""
        who = who.strip()
        try:
            if len(who) == 16:
                return [int(who, 16)]
        except ValueError:
            pass
        found = [p.xuid for p in self.server.peers.values() if p.name.lower() == who.lower()]
        found += [x for x in self.store.find(who) if x not in found]
        return found

    def admin(self, action, who, minutes):
        """/admin/mute and /admin/kick: {"players": [...]}, or None (no such player)."""
        xuids = self.find(who)
        if not xuids:
            return None
        until = time.time() + minutes * 60 if minutes > 0 else 0
        out = []
        for x in xuids:
            table = self.mutes if action == "mute" else self.bans
            if until:
                table[x] = until
            else:
                table.pop(x, None)
            dropped = 0
            for p in self.server.peers.values():
                if p.xuid != x:
                    continue
                if action == "mute" and p.features & FEATURE:
                    self.notice(p, "The server admin muted you for %d min" % minutes if until
                                else "The server admin lifted your chat mute")
                elif action == "kick" and until:
                    self.notice(p, "The server admin removed you for %d min" % minutes)
            if action == "kick" and until:
                for p in [p for p in self.server.peers.values() if p.xuid == x]:
                    self.server.drop(p, "kicked by the admin")
                    dropped += 1
            log.info("admin: %s %016X %r for %d min%s", action, x, self.store.name(x), minutes,
                     " (%d dropped)" % dropped if dropped else "")
            out.append({"xuid": "%016X" % x, "gamertag": self.store.name(x),
                        "until": int(until), "dropped": dropped})
        self.save()
        return {"players": out}

    def moderation(self):
        now = time.time()
        return {"messages": list(self.ring),
                "mutes": {self.store.name(k): int(v - now) for k, v in self.mutes.items() if v > now},
                "bans": {self.store.name(k): int(v - now) for k, v in self.bans.items() if v > now}}

    # --- messages ---

    def notice(self, peer, text):
        self.server.send(peer.addr, KIND, bytes([R_NOTICE]) + text16(text))

    def muted_list(self, xuid):
        p = self.store.get(xuid)
        return [int(k, 16) for k in (p.get("muted", {}) if p else {})]

    def send_muted(self, peer):
        xs = self.muted_list(peer.xuid)
        self.server.send(peer.addr, KIND, struct.pack(">BH", R_MUTED, len(xs)) +
                         b"".join(struct.pack(">Q", x) for x in xs))

    def hides(self, reader, sender):
        """The reader does not get the sender's messages (blocked either way, or muted)."""
        if self.store.blocks(reader, sender) or self.store.blocks(sender, reader):
            return True
        p = self.store.get(reader)
        return bool(p) and self.store.hx(sender) in p.get("muted", {})

    def limited(self, xuid):
        rate, burst = RATE
        now = time.monotonic()
        tokens, last = self.buckets.get(xuid, (burst, now))
        tokens = min(burst, tokens + (now - last) * rate)
        ok = tokens >= 1
        self.buckets[xuid] = (tokens - 1 if ok else tokens, now)
        if len(self.buckets) > 20000:
            self.buckets = {k: v for k, v in self.buckets.items() if now - v[1] < 60}
        return not ok

    @staticmethod
    def club_of(xuid):
        """(club id, tag) of the player's car club, or (0, "")."""
        club, _ = clubs.membership(xuid)
        return (club["id"], str(club["tag"])[:8]) if club else (0, "")

    def send_club_tag(self, peer):
        """CLUBTAG: the tag of the club the player is in (empty: none), when it differs
        from the last one sent to this connection. The XUID must be proven, as for chat."""
        tag = self.club_of(peer.xuid)[1] if getattr(peer, "friends_ok", False) else ""
        if getattr(peer, "club_told", None) == tag:
            return
        peer.club_told = tag
        raw = tag.encode()
        self.server.send(peer.addr, KIND, bytes([R_CLUBTAG, len(raw)]) + raw)

    def chatters(self):
        return [p for p in self.server.peers.values()
                if p.features & FEATURE and getattr(p, "friends_ok", False)]

    def on_message(self, peer, body):
        if not peer.features & FEATURE:
            return
        op = body[0]
        if op == SESSIONS:
            count = body[1]
            if count == 0xFF:
                self.send_muted(peer)
                return
            ids = [struct.unpack_from(">Q", body, 2 + 8 * i)[0] for i in range(min(count, MAX_SESSIONS))]
            peer.chat_sessions = {i for i in ids if i}
            self.check_content(peer)
        elif op == CLUB_SYNC:
            peer.club_chat = True  # this game knows the CLUB channel
            self.send_club_tag(peer)
        elif op == CONTENT:
            (size,) = struct.unpack_from(">H", body, 1)
            raw = body[3:3 + size]
            if size > MAX_CONTENT_BYTES or len(raw) != size:
                return
            content = parse_content(raw.decode("utf-8", "replace"))
            if content is None:
                return
            if content != getattr(peer, "content", None):
                now = time.monotonic()
                if getattr(peer, "content", None) is not None and \
                        now - getattr(peer, "content_at", 0) < CONTENT_CHANGE_SECONDS:
                    return  # a game does not change its content while it runs
                peer.content = content
                peer.content_at = now
                self.check_content(peer)
        elif op == MUTE:
            if not getattr(peer, "friends_ok", False):
                return
            other, on = struct.unpack_from(">QB", body, 1)
            p = self.store.get(peer.xuid, True)
            muted = p.setdefault("muted", {})
            k = self.store.hx(other)
            if on and k not in muted and len(muted) < MAX_MUTED and other != peer.xuid:
                muted[k] = time.time()
            elif not on:
                muted.pop(k, None)
            self.store.dirty = True
            self.send_muted(peer)
        elif op == SAY:
            channel, target = struct.unpack_from(">BQ", body, 1)
            (size,) = struct.unpack_from(">H", body, 10)
            raw = body[12:12 + size]
            if size > MAX_BYTES or len(raw) != size:
                return
            self.say(peer, channel, target, clean(raw.decode("utf-8", "replace")))

    def session_mates(self, peer):
        """The other players of the sender's sessions (a Horizon World excepted: it is a room
        of up to 8 that comes and goes) that sent their content."""
        mine = set(getattr(peer, "chat_sessions", ()))
        world = self.server.world_of_peer(peer.id)
        if world is not None:
            mine.discard(struct.unpack(">Q", world)[0])
        if not mine:
            return []
        return [p for p in self.chatters()
                if p.xuid != peer.xuid and p.room == peer.room and getattr(p, "content", None)
                and mine & set(getattr(p, "chat_sessions", ()))]

    def check_content(self, peer):
        """Tells the players of the sender's sessions what they do not share, once each:
        a car one game lacks (a car pack, an unlocker, a mod of the database) shows as a plain
        placeholder in the game that lacks it (#567). The player lacking something hears who
        has it, the player having it hears who lacks it."""
        mine = getattr(peer, "content", None)
        if mine is None or not peer.features & FEATURE:
            return
        for me, other in [(peer, p) for p in self.session_mates(peer)]:
            # the pair is told once for this combination of content
            for a, b in ((me, other), (other, me)):
                missing = tagged(b.content) - tagged(a.content)   # b has it, a has not
                told = a.__dict__.setdefault("content_told", {})
                if not missing or told.get(b.xuid) == frozenset(map(str, missing)):
                    continue
                told[b.xuid] = frozenset(map(str, missing))
                bname = limits.clean_gamertag(b.name, b.xuid)
                if a.features & FEATURE:
                    self.notice(a, "%s has %s that you do not: their car may look like a plain "
                                   "placeholder to you" % (bname, content_names(missing)))
                if b.features & FEATURE:
                    aname = limits.clean_gamertag(a.name, a.xuid)
                    self.notice(b, "%s does not have %s: your car may look like a plain "
                                   "placeholder to them" % (aname, content_names(missing)))

    def recipients(self, peer, channel, target):
        """The players a message goes to, or a reason it goes nowhere."""
        others = [p for p in self.chatters() if p.xuid != peer.xuid]
        if channel == DIRECT:
            if self.store.relation(peer.xuid, target) != friends.FRIEND:
                return "You can only message your friends"
            to = [p for p in others if p.xuid == target]
            return to or "%s is offline" % self.store.name(target)
        if channel == CLUB:
            club_id = self.club_of(peer.xuid)[0]
            if not club_id:
                return NO_CLUB
            to = [p for p in others if getattr(p, "club_chat", False)
                  and self.club_of(p.xuid)[0] == club_id]
            return to or NO_CLUBMATE
        if channel == WORLD:
            mine = self.server.world_of_peer(peer.id)
            if mine is None:  # a custom game, another playlist, single player (#395)
                return NO_WORLD
            to = [p for p in others if p.room == peer.room
                  and self.server.world_of_peer(p.id) == mine]
            return to or "No one else in your Horizon World has chat"
        if channel == SESSION:
            mine = set(getattr(peer, "chat_sessions", ()))
            world = self.server.world_of_peer(peer.id)
            if world is not None:
                mine.discard(struct.unpack(">Q", world)[0])
            if not mine:
                return "You are not in a session"
            to = [p for p in others if p.room == peer.room
                  and mine & set(getattr(p, "chat_sessions", ()))]
            return to or "No one else in your session has chat"
        return "Unknown channel"

    def say(self, peer, channel, target, text):
        if not text:
            return
        if not getattr(peer, "friends_ok", False):
            self.notice(peer, auth.refusal(peer, "Chat is off")
                        or "Chat is off: this XUID belongs to another installation")
            return
        until = self.admin_muted(peer.xuid)
        if until:
            self.notice(peer, "The server admin muted you (%d min left)" %
                        max(1, int((until - time.time()) / 60 + 0.99)))
            return
        if self.limited(peer.xuid):
            self.notice(peer, "Slow down: at most %d messages in %d s" %
                        (RATE[1], int(RATE[1] / RATE[0])))
            return
        if text.startswith("/") and self.commands and self.commands(peer, channel, text):
            return
        to = self.recipients(peer, channel, target)
        if isinstance(to, str):
            self.notice(peer, to)
            return
        delivered = [p for p in to if not self.hides(p.xuid, peer.xuid)]
        name = limits.clean_gamertag(peer.name, peer.xuid).encode()
        msg = struct.pack(">BBQB", R_MESSAGE, channel, peer.xuid, len(name)) + name + text16(text)
        for p in delivered:
            self.server.send(p.addr, KIND, msg)
        entry = {"time": int(time.time()), "from": peer.name,
                 "xuid": "%016X" % peer.xuid, "channel": CHANNELS.get(channel),
                 "to": [p.name for p in to], "text": text}
        if channel == CLUB:
            entry["club"] = self.club_of(peer.xuid)[1]
        self.ring.append(entry)
        log.debug("chat #%d %r %s -> %d", peer.id, peer.name, CHANNELS.get(channel),
                  len(delivered))
