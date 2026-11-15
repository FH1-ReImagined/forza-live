# Operating a public Forza Live server

Day-to-day running of a server other people play on: the operator endpoints and the [operator panel](#operator-panel), chat, photo and design moderation, the score admin, the list of community servers, [what holds the event loop](#what-holds-the-event-loop), and backups. Options are in [configuration](configuration.md); the first setup is in the [hosting guide](hosting.md).

## Operator endpoints

(The moderation of community photos and designs has stricter rules: [below](#community-photos-and-designs).)

`GET /gift?to=<gamertag|XUID>&credits=<n>[&message=..]` or `&car=<id>`,
`GET /notify?to=..&type=..`, `GET /broadcast?type=0x100&Message=..[&days=1-30]` (one
Message Center notification for every player, new ones included, stored once), and `GET /world/event?session=all|<id>[&event=<id>]` are
operator-only. They are answered for requests from the server's own machine (loopback, not
through a reverse proxy), or with `FORZA_ADMIN_TOKEN` (header `X-Admin-Token: <token>` or
`?token=<token>`; prefer the header: the log leaves a token out of the URLs it prints, but a
shell history or a browser does not); everything else gets 403. Each wrong token is a warning in
the log with the address it came from (an IPv6 address counts under its /64). After 10 from one
address in 5 minutes that address gets 403 for every token, the right one too, until they have
passed; all the requests through the tunnel without an address of their own count as one address,
so nobody outside can lock out the LAN or this machine. One case is the operator's and the
outside's at once: the tunnel's agent on the server's own machine without `--proxy-protocol-tcp`,
where every request through the tunnel comes from loopback as the operator's own do. There a
token of 16 characters or more is taken whatever came before (it cannot be guessed, and wrong
ones are only logged); a shorter one keeps the lock, so wrong tokens from outside can lock this
machine's own requests out until it is replaced. The log never prints a token: a `token=` in a
URL is left out however it is written. The token must be long and random
(`openssl rand -hex 32`): the placeholders of `.env.example` and of these pages are not taken as
a token (the start says so, and the panel stays off), and the start warns about one under 16
characters. From the host's terminal:

    docker exec forza-live python3 -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:21201/gift?to=Name&credits=50000').read())"

or from your LAN: `curl -H "X-Admin-Token: <token>" "http://192.168.1.x:21201/gift?..."`.
Without the token variable, remote use is impossible. Do not expose the token on the
internet side (it travels in plain HTTP).

## Operator panel

The panel is a web page for everything on this page: `/operator/` on the server's HTTP port (the
title services port, 21201 by default), opened in a browser on the home network. It asks for
`FORZA_ADMIN_TOKEN` once, keeps it in that tab only (`sessionStorage`, forgotten when the tab
closes) and sends it as the `X-Admin-Token` header with every request. Every change is written to
the audit log, which the panel shows.

Who can open it:

- Only when `FORZA_ADMIN_TOKEN` is set. Without it the panel is off.
- Never through the tunnel or a reverse proxy. Every panel URL, the page too, answers 404 to a
  request from a `--trusted-proxy` address or with `X-Forwarded-For`, `X-Real-IP` or `Forwarded`
  (the rule of the [photo moderation](#community-photos)). With a tunnel agent on the server's
  own machine, open the panel from another machine on the LAN.
- Only from a private or loopback address (10/8, 172.16/12, 192.168/16, 127/8, link-local,
  IPv6 ULA and loopback), so a server whose port is forwarded to the internet without a tunnel
  still keeps it to the home network.
- Every request of the page needs the right token in its header. The token is never put in a
  URL and no cookie is set, so a page on another site cannot act with it. Requests with an
  `Origin` that is not the panel's own, or a `Sec-Fetch-Site` naming another site, are refused,
  and a change must be `application/json`.
- 10 wrong tokens from one address in 10 minutes (100 from everyone) answer 429 to everything,
  the right token too, until the 10 minutes have passed.

The page is plain HTML, JavaScript and CSS served from the package (`forza_live/panel/static/`),
with a strict Content-Security-Policy and no framing. Everything it shows is set as text, since
gamertags, photo titles and server names are other people's text. It holds no secrets: a relay's
new secret goes to its file in the data folder, never to the page.

The tabs:

- **Status**: players online, Horizon Worlds (and `/world/event` to start an event in one), the
  relays, score tokens, signed requests, and a preview of the Discord status message.
- **Messages**: a broadcast to every player (type, text, days), the live broadcasts (delete one
  to take it back from every Message Center), and a message to one player (`/notify`).
- **Gifts**: credits or a stock car for one player, with a reason the player sees. A credits gift
  is capped at `FORZA_PANEL_GIFT_MAX` (1,000,000 by default). The recent gifts are listed.
- **Players**: search by gamertag or XUID. A player's page shows their key (signed, a friends key
  or none), signed sign-ins, last seen, build, friends, club, scores (with unrank, rank and
  delete), photos and designs, challenge progress, gifts and bans, with ban, sign-in ban, mute and
  gift forms; a banned player's page says "Banned" at the top. With `FORZA_ACCOUNTS_ADMIN_URL`
  set, it links to the player in the website accounts service's admin (accounts are managed
  there: suspend, unlink, void likes).
- **Bans and chat**: the [bans from the server](#banning-a-player) (`banned_xuids.json`, with the
  reason given in the panel), timed sign-in bans and chat mutes (`moderation.json`), and the
  recent chat.
- **Scores**: `/admin/scores` with its filters, shortcuts for held ghosts, failed start checks,
  unverified rows and rows before the tags, and the score token counts per board.
- **Photos** and **Designs**: the moderation queues and every state, with the image or preview
  on demand, and release, hold, hide, unhide, delete and ban.
- **Servers**: `server_list.json`: this server's entry and the community servers (add, edit,
  remove, each checked with `server_list.py`'s rules), the entries left out and why, the list's
  public key, and a `/status` check of each address.
- **Relays**: `relay_list.json`, with each relay's probes and load; add (or give a new secret)
  and revoke.
- **Challenges**: today's and this week's objectives, how many players finished each, and the
  weekly gifts sent.
- **Events**: the community events of [server events](server_events.md): enroll, create,
  start, close, attendance, award and revoke.
- **Audit log**: every change made from the panel, newest first.

The audit log is `operator_audit.jsonl` in the data folder: one JSON line per change, with who
(`operator`), the address it came from, the action, its target, the answer's status, the time and
what was sent (cut to a few hundred characters a value, never the token). Past 4 MiB it becomes
`operator_audit.jsonl.1`. Reads are not logged there.

### What the panel covers

Every operator endpoint and command, and where it is in the panel:

| Endpoint or command | What it does | In the panel |
|---|---|---|
| `GET /status` | the status page | Status |
| `GET /broadcast?type=..&Message=..&days=..` | a message for every player | Messages: Broadcast |
| `GET /admin/broadcasts`, `POST /admin/broadcasts/<id>/delete` | the live broadcasts; take one back | Messages: Live broadcasts |
| `GET /notify?to=..&type=..` (`type=sample` too) | a message for one player | Messages: Message one player |
| `GET /gift?to=..&credits=..` or `&car=..` | a gift from the Message Center | Gifts; a player's page |
| `GET /world/event?session=..&event=..` | start an event in a Horizon World | Status: Horizon Worlds |
| `GET /admin/chat` | the recent chat, mutes and bans | Bans and chat |
| `GET /admin/mute?who=..&minutes=..` | mute a player's chat (0 lifts it) | Bans and chat; a player's page |
| `GET /admin/kick?who=..&minutes=..` | drop a player and refuse their sign-in (0 lifts it) | Bans and chat; a player's page |
| `GET /admin/scores?..`, `POST /admin/scores/unrank\|rank\|delete?..` | leaderboard rows by source | Scores; a player's page |
| `GET /admin/photos[?status=..]`, `/queue`, `/img/<id>` | the community photos | Photos |
| `POST /admin/photos/<id>/hide\|unhide\|hold\|release\|delete\|ban` | moderate a photo | Photos; a player's page |
| `POST /admin/photos/ban/<XUID>`, `unban/<XUID>` | [ban a player from the server](#banning-a-player), or lift it | Bans and chat; a player's page |
| `GET /admin/designs[?state=..]`, `/queue`, `/img/<id>.png` | the community designs | Designs |
| `POST /admin/designs/<id>/release\|hold\|hide\|unhide\|delete\|ban` | moderate a design | Designs; a player's page |
| `GET/POST /admin/events...` and `tools/event_admin.py` | community events | Events |
| `python3 -m forza_live.core.server_list` | check `server_list.json`, print the public key | Servers (the panel does not make the key: the first `/servers.json` request does) |
| editing `server_list.json` by hand | the launcher's server list | Servers |
| `python3 -m forza_live.core.relays add\|remove\|list` | the volunteer relays | Relays |
| `tools/server_probe.py` | check a server from outside (`/status` and a UDP HELLO) | Servers: the `/status` check (not the UDP one) |

Not in the panel: the `/owner/` routes (the website accounts service's, with their own token),
the screening worker (it uses the photo and design routes above), `tools/xtsw_decode.py` (a
developer's tool), `--rebind-legacy` or clearing a key in `friends.json` (by hand, with the
server stopped), backups, and the start options.

The panel's own routes are under `/operator/api/`: `GET` for what it shows, `POST` with a JSON
body for a change. They call the same code as the endpoints above, with the panel's rules
(`forza_live/panel/routes.py` lists them).

## Friends, chat and clubs

Friends are kept per player (XUID) in `friends.json` in the data folder: each
player's gamertag, when it was last seen, the public key of its game (or, until the player
upgrades, a SHA-256 of the friends key of a game before signed requests), and its friends,
requests both ways and blocks (written at most every 2 s after a change and on shutdown;
keep the data folder). The first game that uses an XUID's friends binds them to its key
([signed requests](configuration.md#signed-requests)); another game claiming that XUID gets no list. A record with a key is never forgotten; one without (a name another player blocked or asked) that holds no friends, requests or blocks is forgotten after 180 days, and such records never count against the room for new players (`FORZA_MAX_PLAYERS`).
Limits: 200 friends, 50 requests waiting each way, 200 blocks per player; 10 requests a
minute (bursts of 5), 60 other changes a minute, 30 searches a minute; searches answer at
most 20 players. A request to a player who blocked the sender looks sent but is dropped,
and so are invites from a blocked player. The status page shows how many players,
friendships and open requests there are, and how many of the players have a key (`with_key`:
the number `FORZA_MAX_PLAYERS` caps). Games up to v0.1.14 still see everyone online as a
friend.

Text chat is relayed, not stored: a message goes to the players online in the
sender's session, Horizon World, to one friend, or to the other members of the sender's car
club who are online (the Club channel: wherever they are, in any room or world, from
the club in `clubs.json`; both sides need a proven XUID, as for all chat, and a game that
asks for the club tag, so games before the Club channel neither show it nor get its
messages), and the server keeps only the last 200 messages in memory (time, sender, channel,
the club's tag for the Club channel, recipients, text) for moderation; they are gone
when the server stops. Limits: 200 characters per message, 5 messages per 10 s per player
(the sender is told to slow down), direct messages only between friends; a player's mutes
(kept in `friends.json`) and blocks hide the other's messages. Games up to v0.1.14 have no
chat.

Car clubs in the Horizon World : Find Game > Horizon World searches are answered with
the open worlds the fullest first; for a player in a club, a world with one of its online
clubmates in it (the one with the most clubmates first) comes before that. Only worlds with
room are offered, so a full world of clubmates changes nothing, and a player without a club,
or without a clubmate in an open world, is placed as before. The chat commands `/club` (in any
channel: the clubmates online and where they are, by gamertag) and `/club join [gamertag]`
(puts that clubmate's world first for 10 minutes) are answered only to the player who types
them.

In World chat, the world's host can type `/event random` to start a random race in the
Horizon World or the Co-op Festival. The server picks once for the whole session from that
mode's eligible events. The Horizon World needs the game's database (`--game-db`); the Co-op
Festival keeps its usual event restrictions and rewards. Other players cannot change the
host's selection.

### Moderating players

Chat moderation uses operator endpoints, with the same rules as `/gift`:

- `/admin/chat`: the last messages, and the mutes and bans with their seconds left;
- `/admin/mute?who=<gamertag or XUID>&minutes=N`: the player's messages are refused (they
  are told for how long); `minutes=0` lifts it;
- `/admin/kick?who=...&minutes=N`: drops the player and refuses its sign-in for N minutes
  (a ban; the game shows "server error 4: removed by the server admin"); `minutes=0` lifts
  it.

Mutes and bans are kept in `moderation.json` in the data folder until they run out. Example:
`curl -H "X-Admin-Token: <token>" "http://host:21201/admin/mute?who=Name&minutes=30"`.

### Banning a player

A ban (`banned_xuids.json` in the data folder, a JSON list of hex XUIDs) bans the player from the
whole server until it is lifted. Ban from the panel (Bans and chat, or the player's page, which
then says "Banned"), from the Photos and Designs tabs (ban the owner), with
`POST /admin/photos/ban/<XUID>` (`unban/<XUID>` lifts it; [below](#community-photos)), or by
editing the file. The list is read again when it changes: no restart. The panel writes each ban
and unban, with its reason, to the audit log.

What a banned XUID gets:

- **No sign-in.** Its HELLO is refused with error 4 (the game shows "server error 4: banned from
  this server"), and its title server sign-in (`Logon2`) fails, so the game shows the Horizon
  server as unavailable. A player who is online when banned is dropped at once (within 5 s when
  the file was edited by hand): out of the player list and the status page, its sessions and
  lobbies gone from matchmaking and Horizon World placement, out of chat, and its game's next
  HELLO is refused. It is not restored from `live_state.json` after a restart.
- **No writes.** Every title service call with its XUID fails, whatever the method and whether
  it is signed (scores, ghosts, score token starts, the 1000 Club, race stats, the online
  profile, auctions, bids, car gifts, clubs, the Message Center, photos, designs). Every UDP
  message of a game signed in as it is dropped (stats, friends requests, chat, invites,
  challenges, community events, matchmaking). The signed HTTP routes take it for an unknown
  player (shader list uploads, `/coop/results`, `/challenges/progress`), and the website cannot
  delete its photos or designs (`/owner/`). Behind those checks the stores refuse it as well:
  scoreboards, stats (its own and a host's writes for it), daily and weekly challenge progress
  and their gifts, and the co-op ledger (no place in a round, no results handed out).
- **Nothing listed.** Its leaderboard rows (the game's boards, Rivals, rollups, online XP, the
  1000 Club and the website's `/leaderboards`), stats ranks, challenge completions (the counts on
  `/challenges`, the launcher and the Discord status), auctions, friend requests waiting for
  others and friends searches, community photos and storefront designs (the game's, the gallery
  and the website's) are left out of every answer.

Nothing is deleted: rows, progress, photos, designs and co-op results stay in their files, and
the operator still sees them (the panel's player page, `/admin/scores`). Lifting the ban shows
them again. Not covered: what the player already sent to others (gifts, Message Center messages,
friendships, an auction's top bid) stays, and a game already in a session with others keeps its
direct connections to them until that session ends (the server no longer relays or lists it).
A timed sign-in ban (`/admin/kick`) is separate: it only refuses the sign-in, for N minutes.

## Community photos, videos and designs

Photos, videos and designs players share can appear on a website ([what is published](website-data.md#community-photos)). Moderating them has stricter rules than the other operator endpoints, and the [screening worker](#automatic-screening) can do the first pass.

### Community photos

Moderation is for the operator only, from the LAN or an ssh tunnel: the routes refuse anything
that came through the tunnel or a reverse proxy, even with the token, because the token would
cross the internet in plain HTTP. That is every request from a `--trusted-proxy` address and
every request with `X-Forwarded-For`, `X-Real-IP` or `Forwarded`. On a server set up with a tunnel as in [configuration](configuration.md#behind-a-tunnel-playitgg) (127.0.0.1 and the host's LAN IP are trusted proxies) that includes the host itself and `docker
exec`: moderate from another machine on the LAN, or through an ssh tunnel that ends at an address
that is not a trusted proxy. The routes take `FORZA_ADMIN_TOKEN` only in the `X-Admin-Token`
header (never `?token=`; this machine's loopback, when it is not a trusted proxy, needs none, as
for the other operator endpoints), and refuse any request with an `Origin` header (a web page
cannot drive them). With `T='X-Admin-Token: <token>'` and `S=http://192.168.1.x:21201`:

```
curl -H "$T" $S/admin/photos                    # every photo: id, owner XUID, gamertag, title,
                                                # status, banned, public, sizes, sha256
curl -H "$T" "$S/admin/photos?status=hidden"     # live, held or hidden only
curl -H "$T" $S/admin/photos/img/<id>.png -o p.png   # look at one, whatever its status
curl -H "$T" -X POST $S/admin/photos/<id>/hide   # out of the feed and the image route
curl -H "$T" -X POST $S/admin/photos/<id>/unhide
curl -H "$T" -X POST $S/admin/photos/<id>/delete # gone (the same picture is not published again)
curl -H "$T" -X POST $S/admin/photos/<id>/ban    # bans the photo's owner
curl -H "$T" -X POST $S/admin/photos/ban/<XUID>  # or by XUID; unban/<XUID> lifts it
```

A ban goes into `banned_xuids.json` in the data folder (a JSON list of hex XUIDs that can also be
edited by hand) and bans the player from the whole server ([banning a player](#banning-a-player)):
among the rest, their uploads are refused and their photos and designs are served to nobody until
the ban is lifted. For a screening worker
(a separate container with the token, at an address that is not a trusted proxy):
`GET /admin/photos/queue` lists the held photos, oldest first; `POST /admin/photos/<id>/hold`
takes a live photo back to the queue, `POST .../release` publishes a held one (a photo the
operator hid cannot be released, 409), and both take an optional `{"note": "..."}` kept for the
operator.

### Community videos

The game's movie uploads (replay clips, WMV) become videos only with the
[video converter](../video/README.md) running beside the server (`COMPOSE_PROFILES=videos` in
`.env` with the repository's `compose.yaml`). Without it a clip waits a day and is dropped, and
nothing is published. A converted video is **always held** first, whatever
`FORZA_COMMUNITY_HOLD` says: the [screening worker](#automatic-screening) scores one frame in
each 2 s of it (at random instants that only the server and the converter know; the poster is one
of them) and releases or hides it, and without the worker you decide each one yourself. Videos
have a disk budget of their own, `FORZA_VIDEOS_CAP_MB` (2048), apart from the quota of the other
uploads (`FORZA_DISK_CAP_MB`): they can never stop the photos, ghosts or designs. The uploaded
clip is not kept: it is deleted once the converter has made its copy, or when it is refused. The routes have the photos' rules (the LAN or an ssh tunnel, the token in its header
only, no proxy, no `Origin`). With `T` and `S` as above:

```
curl -H "$T" $S/admin/videos                     # every video: id, owner XUID, gamertag, title,
                                                 # status, length, sizes, the clip's codecs
curl -H "$T" "$S/admin/videos?status=converting" # converting, held, live or hidden only
curl -H "$T" $S/admin/videos/queue               # the held ones, oldest first, with their frames
curl -H "$T" $S/admin/videos/media/<id>.mp4 -o v.mp4  # watch one, whatever its status (.jpg: poster)
curl -H "$T" $S/admin/videos/frame/<id>-1.jpg -o f.jpg   # a frame the screening scored
curl -H "$T" -X POST $S/admin/videos/<id>/release  # publish a held one
curl -H "$T" -X POST $S/admin/videos/<id>/hold     # a live one back to held
curl -H "$T" -X POST $S/admin/videos/<id>/hide     # out of the feed and its routes; unhide undoes it
curl -H "$T" -X POST $S/admin/videos/<id>/delete   # gone (the same clip is not published again)
curl -H "$T" -X POST $S/admin/videos/<id>/ban      # bans the video's owner
```

The operator panel has a Videos tab with the same actions, the poster, the frames and a player.
A video that is still converting can only be deleted. A hidden one cannot be released (409):
unhide it. The server's log names each clip it refused and why (`videos: ... not published: ...`),
including clips whose picture is blank: the TU4 game, as recompiled in November 2026, records
black frames with the sound, and such a clip is not published.

### Who may fetch the photos and videos

The public files (`/community/img/*` and `/community/video/*`) have a budget for each address as
well as the one for the whole server, and a reader that takes nothing is dropped after 60 s
([configuration](configuration.md#the-photo-and-video-files) has the numbers). What that asks of
the operator:

- nothing, when the website's web server and builder reach the server from this machine or the
  home network: private and loopback addresses have no budget of their own. (An address that is
  a `--trusted-proxy` is the tunnel's, not the website's: with `--proxy-protocol-tcp` its
  connections must start with the agent's header.)
- a builder or web server of yours at a public address: put the address in
  `FORZA_WEBSITE_ADDRESSES` (comma separated; or `--website-address`) and restart. The start
  logs `website addresses (no media budget of their own): ...`. Without it, it has what any
  address has, which is sized for a whole build that starts with an empty cache, and an item
  answered 429 is fetched at the next build: a builder whose address changes with every run (a
  hosted one) cannot be listed and does not need to be;
- an address over its own budget is answered 429 with `Retry-After` and
  `too many requests from this address`; the server-wide limit's 429 says
  `too many requests, try again in a minute`. Both show as `-> 429` in the log's `http` lines.

### Automatic screening

[`screening/`](../screening/README.md) is that worker. Run it beside a server that has
`FORZA_COMMUNITY_HOLD=1`. Every 30 s it reads the queue and scores each held photo locally, on
the CPU, with no hosted service (and the [designs](#community-designs) waiting for the website,
the same way):

- an NSFW classifier (Freepik's EVA02, MIT);
- SigLIP2 zero-shot prompts for hate symbols, gore and nudity, over the whole frame and 6 tiles
  (Apache-2.0);
- OCR (RapidOCR, Apache-2.0) and word lists (LDNOOBW, CC-BY-4.0, and its own list of slurs,
  slogans and codes).

It then releases the photo, hides it with a note naming the rule (`screening: hide (nsfw-high:
...)`), or leaves it held for you, logging why. A [video](#community-videos) is scored frame by
frame, the same way: one frame's hide hides it, and it is released only when every frame is
clear. A doubtful frame hides the video too, so no video waits for you; you can unhide one in
the panel (`[videos] doubtful = "hold"` in `screening.toml` leaves such a video held instead). Only a confident hit hides a photo. A race number
such as 14, 18 or 88, or a badge such as SS, never hides one. The decision table, the measured
cost (about 1.3 s and 1.4 GB at peak on 4 threads) and the known limits are in its README.

Deployment, on the machine of the server (the commands are in the README):

- Build it with `docker build -t forza-screening screening/`. The build downloads the models,
  pinned by revision and SHA-256. The running container downloads nothing.
- Give it the token in an env file (`--env-file`).
- Run it on the default **bridge** network, not `host`, with
  `-e FORZA_SERVER=http://172.17.0.1:21201` (the bridge's gateway).
- Use `--memory 3g --read-only --cap-drop ALL`.

Why the bridge network:

- With `--network host` the worker would come from 127.0.0.1, a trusted proxy here, and every
  moderation request would be refused.
- From the bridge it comes from its own 172.17.0.x address. That address is no trusted proxy and
  sends no proxy header, so the token admits it.
- So the server must listen on all addresses (`--host 0.0.0.0`), and `--trusted-proxy` must not
  cover 172.17.0.0/16.

The worker's image sets `ORT_DISABLE_TELEMETRY=1`: onnxruntime's Linux packages would otherwise
send usage telemetry to Microsoft. To stop the container from reaching anything but the server,
the README shows how to give it a network of its own with an iptables rule.

To review what it left held, run `docker logs forza-screening | grep '"decision":"hold"'`, then
use the routes above.

### Community designs

Moderation, with the same rules as the photos' (the LAN or an ssh tunnel, the token in its header
only, no proxy, no `Origin`). With `T` and `S` as above:

```
curl -H "$T" $S/admin/designs/queue              # pending designs, oldest first (no XUID)
curl -H "$T" "$S/admin/designs?state=held"       # every design (pending, clear, held, hidden)
                                                 # with its owner XUID, note, reports, car
curl -H "$T" $S/admin/designs/img/<id>.png -o d.png   # its preview, whatever its state
curl -H "$T" -X POST $S/admin/designs/<id>/release   # on the website (pending or held)
curl -H "$T" -X POST $S/admin/designs/<id>/hold      # off the website, still in the game
curl -H "$T" -X POST $S/admin/designs/<id>/hide      # off the website and out of the game
curl -H "$T" -X POST $S/admin/designs/<id>/unhide    # back (also after three reports)
curl -H "$T" -X POST $S/admin/designs/<id>/delete    # gone, from the game too
curl -H "$T" -X POST $S/admin/designs/<id>/ban       # bans the design's owner
```

`<id>` is the design's public id. A hidden design cannot be released (409): unhide it. The
actions take an optional `{"note": "...", "expect": "pending"}`; with `expect` the action applies
only to a design in that state (409 otherwise), which is how the worker never overrides you: it
releases, holds or hides only designs that are still pending.

## Converted worlds

The launcher can play Forza Horizon 2's and Fast & Furious's maps converted for FH1 (a "world")
online. A world has a room of its own on the server: the player's room plus `+fh2` or `+anthem`
(and `#TU4` after it for a Title Update 4 game), so its players meet only each other. The log's
`join` line shows it: `join #N 'tag' xuid X room '+anthem#TU4'`.

A world is another map with other roads, so the server takes nothing of Colorado's from a game in
one: no leaderboard rows (events, Rivals, speed traps, speed zones, also unranked ones), ghosts,
rival messages, 1000 Club medals, race stats, online profile (online XP), daily and weekly
challenge progress (so no weekly gift from it), `firstScore`, co-op campaign results or Xbox LIVE
stats. The game is told each call went through. Signing in, friends, chat, clubs, matchmaking in
the room, the session's own play, photos, designs and videos are as in Colorado.
[Title services](title_services.md#converted-worlds-rooms-forza_livecoreroomspy) has the calls,
the answers and how the server finds a call's room.

Nothing to set up. Only a room that ends in `+anthem` or `+fh2` is a world's; a player who names
their own room that way by hand is in it too (the server cannot tell the two apart). The game
keeps 32 bytes of the room: a long room with letters such as `é` or `ク` that the launcher cuts in
letters loses its ending there, and its world's results count as Colorado's (the `join` line
shows the room the game sent). The log says
once per session and kind what it left out:
`world: <XUID> in room '+fh2#TU4' (Forza Horizon 2's world): not kept, as Colorado's: race stats
(first: User.WriteRaceStatsEx; told once per session)`. A row that is on a board already is
not touched: `/admin/scores` (below) finds and moves rows as before.

## Score admin

For a public server: start it with `--server-id main` (or `FORZA_SERVER_ID=main`), before the
first run with this code. The id is the name on rows written from then on. Nothing else needs a
migration: the existing rows have no tags, show as before and are found with `build=none`.

`/admin/scores` (operator only, like `/gift`) finds and moves rows by source:

- `GET /admin/scores?<filters>`: the matching rows (at most 500) and how many there are;
- `POST /admin/scores/unrank?<filters>`: ranked rows off the boards, kept for review with
  their ghosts;
- `POST /admin/scores/rank?<filters>`: unranked rows back on their boards, where they are
  better than the player's row there (a kept ghost too, when it passes the checks);
- `POST /admin/scores/delete?<filters>`: rows and their ghosts removed.

Filters: `server`, `build` (`none`: the rows from before the tags), `verified` (0 or 1),
`player` (an XUID in hex), `board` (`3:199:0` or `event-199`), `reason` (part of an unranked
row's reason), and `state` (`ranked`, `unranked` or `all`, the default). A change needs a
filter besides `state`, and a browser's cross-origin request (an `Origin` header) is refused.
An unranked row's reason is `unverified`, `operator`, `ghost: ...` (a Rivals ghost that failed
its checks) or `start: ...` (a result faster than the time since its event started, or with no
start: [which results count](website-data.md#which-results-count)); `reason=start` finds the
last kind.
To take a bad build off the boards:
`curl -X POST -H "X-Admin-Token: <token>" "http://host:21201/admin/scores/unrank?build=0.2.1/tu4"`.

### Score tokens

A result needs the time it claims to have passed since its event started
([which results count](website-data.md#which-results-count)). `--score-tokens` (or
`FORZA_SCORE_TOKENS`) says what happens when a result fails that check:

- `log` (the default): the check runs, but the result stays ranked. The server logs one line per
  failed check, with the board, the reason and the player's public leaderboard id (never a
  gamertag or an XUID): `score token: board 3:199:0, player 1a2b3c4d5e6f, score 60.000: too
  fast, 5.0 s after its start, it needs 60.0 s; ranked (--score-tokens log)`.
- `enforce`: the result is kept unranked with the reason `start: ...`. The game is told success.
- `off`: no tokens and no check.

The status page (`/status`) counts the checks since the server started: `score_tokens` has the
mode, the open tokens, how many results were checked and failed (`no_start`, `too_fast`), and
the failures per board (`3:199:0` is type 3, the event with Id 199), at most 500 boards.

The main server runs `log` first. After a week of public play, the counts and log lines show
whether honest players' events (street races, showcases, speed zones) ever arrive with no start
or too fast. Switch to `enforce` once they don't, or after fixing those that do.

## Listing community servers

The launcher's Servers page lists the servers players can pick. It takes the list only from the
main server, as `GET /servers.json`, and only when the list is signed with the key whose public
half the launcher ships. Owners do not add themselves: they apply (address, region, contact and
rules; on the main server, with the [List my community server](hosting.md#getting-listed) form),
you check the server, and you add it to `server_list.json` in the data folder:

```json
{
  "self": {"name": "Forza Live", "address": "your.host:21200", "region": "Europe",
           "contact": "operator@example.org"},
  "servers": [
    {"name": "Example community", "address": "192.0.2.10:21200", "region": "North America",
     "contact": "owner@example.org", "verified": true, "id": "example-1"}
  ]
}
```

- `self` is your own server. It is listed first and marked verified, with this server's id
  (`--server-id`, [score admin](#score-admin)).
- `address` is what a player would type in the launcher: `host[:port]`, where the port is the UDP
  port and the HTTP port is the next one (21200 and 21201 by default). The launcher pings
  `http://host:<port + 1>/status` for each entry, so check that it answers before you list it.
- `name` (at most 48 characters) is required. `region` (32) and `contact` (120) are optional.
  `verified` is `false` unless you set it, and `id` is the server id that server tags its
  leaderboard rows with.
- Community servers keep their own leaderboards. Only rows with the main server's id are the
  official ones ([which results count](website-data.md#which-results-count)).

The file is read again when it changes, and the list is signed again then and once a day. A
signed list expires after 14 days, so an old copy cannot be served in place of the current one for
long. An entry with a mistake is left out and named in the log. To check the file and print the
public key without starting the server, run:

    python3 -m forza_live.core.server_list --data-dir /path/to/data

or, in Docker, `docker exec forza-live python3 -m forza_live.core.server_list --data-dir /data`.

The signing key is `server_list_key.txt` in the data folder. It is made at the first request for
the list (the log names its public key) or by the command above. Keep it with your backups, and
keep it private: anyone who has it can sign a list the launcher accepts. If you make a new key,
launchers accept lists signed with it only after a launcher release ships its public key.

To take a server off the list, remove its entry. A launcher that still has a server saved keeps
it, and shows it as down while it does not answer.

## Volunteer relays

Players far from the main server lag when they cannot connect directly and their traffic goes
through it. A volunteer relay is this same program, run with `--relay-only` on a machine in
another region: it links out to the main server, holds no database and no admin token, and only
forwards game traffic between players the main server sends to it. Players, friends and
leaderboards stay on the main server.

Nothing changes until you list a relay. Then:

- Direct connections still come first. Two players get a relay only when they have relayed
  through the main server for `--relay-after` seconds (8 by default, longer than a game's hole
  punching), and only when both games support relays; older games keep the main server's relay.
- The main server picks the relay with the lowest worse ping of the two (the games ping every
  relay that is up when they sign in), and keeps the players of one session on one relay.
- Each player gets a ticket the main server signs with that relay's secret: its session, its
  XUID and id, the relay, an expiry (10 minutes, renewed while it plays) and a rate cap (the
  `--rate-limit`). The relay forwards only with a valid ticket, only between players of the same
  session, within the cap, and never to private, loopback, link-local or multicast addresses.
  Slots not used for 60 seconds expire.
- The relay sends a player nothing until the player proved its address: it answers a bind from
  a new address with a challenge (a cookie only that address receives), and binds the slot when
  the game sends the cookie back from there, as TURN servers check permissions. So a ticket
  holder cannot forge its bind's source address and aim the session's traffic at someone else.
  The proof is asked again every minute or so while the player plays.
- A relay is up while its link was heard in the last 30 seconds and it passes the main server's
  probes. When it goes down, its players go back to the main server's relay.

The main server's own relay has no such proof: older games cannot answer a challenge, and
someone forging a HELLO would simply pose as an older game. What limits it is the per-player rate
cap (`--rate-limit`), at most 16 players per address, and a room in common. The volunteer relays
are used only by games that answer the challenge.

### Probes and probation

Every 3 minutes (every minute while a relay fails or is not listed) the main server probes each
relay: its link must be up, and it must echo a ping of a fresh nonce sent to the address the
players get (the one in `relay_list.json`, so a port that is not open fails even with the link
up). The status page's `relays` block shows each relay's `linked`, `listed`, `up`, `probe_ms`,
`probe_fails`, `probation`, `sessions`, `sessions_relayed`, `players` and `capacity`; the Discord
status message shows each relay's region, up or down, and the sessions it relays.

- A new relay is used and published in `/relays.json` once its first probe passes.
- A relay that fails 3 probes in a row drops out of `/relays.json` and gets no players (its
  sessions go back to the main server's relay); it comes back after it passes 2 in a row. This
  is kept in `relay_health.json` in the data folder across restarts.
- For its first hour in the list (`--relay-probation`, in minutes; 0 for none) a relay is on
  probation: it gets only a tenth of the pairs it would be picked for, and at most 4 sessions at
  once.
- The relay reports what it can take: its slots, and the upload its operator offers
  (`--relay-bandwidth`, Mbit/s). The main server sends it no more players than that carries at
  the per-player rate cap.

### Adding a relay

Check the volunteer first (an operator agreement: a relay sees the players' IP addresses). Then,
in the data folder of the main server:

    python3 -m forza_live.core.relays --data-dir /path/to/data add eu-1 \
        --address your.host:21200 --name "Example relay" --region Europe --contact owner@example.org

or, in Docker, `docker exec forza-live python3 -m forza_live.core.relays --data-dir /data add ...`.

- The id (`eu-1`) is 1 to 32 of `a-z`, `0-9` and `-`.
- `--address` is where players reach the relay: `host:port`, its UDP port.
- The command writes the entry to `relay_list.json` and a new secret to
  `relay_secrets/eu-1.txt` (readable by the owner only). Send that file to the relay's operator
  privately; it is their `--relay-secret-file`.

The server reads the list again within a few seconds. `python3 -m forza_live.core.relays --data-dir
/path/to/data list` shows the listed relays; the status page's `relays` block shows which are up.
`GET /relays.json` publishes the list, signed with the [server list](#listing-community-servers)'s
key.

To revoke a relay, remove it:

    python3 -m forza_live.core.relays --data-dir /path/to/data remove eu-1

Its secret file is deleted, its link is refused from then on, and the relay drops every slot it
holds. Running `add` again for a listed id gives it a new secret and revokes the old one.

### Running a relay

The relay's operator runs, with the secret file from the main server's operator:

    python3 forza_live_server.py --relay-only --port 21200 \
        --relay-of your.host:21200 --relay-id eu-1 --relay-secret-file /path/to/eu-1.txt

or with Docker (the image's UDP port 21200; a relay opens no HTTP port): `docker run -d --name
forza-relay --restart unless-stopped --network host -v /path/to/secret:/secret:ro forza-live
--relay-only --relay-of your.host:21200 --relay-id eu-1 --relay-secret-file /secret/eu-1.txt`.

- Open (or forward) its UDP port; it needs no TCP port and opens nothing on the main server.
- It needs no data folder and writes nothing. Its log names its link and, every 5 minutes, its
  counters (slots, sessions, forwarded bytes, refused tickets, challenges).
- `--relay-bandwidth MBIT` tells the main server how much upload the relay may use (say `50`
  for 50 Mbit/s); without it, only its slots limit it.
- Its clock must be right within 2 minutes (tickets and links carry the time).

## Signed requests

Players prove their XUID with a key pair, and the operator can let a new installation in or set a cutoff for old games; the options are in [configuration](configuration.md#signed-requests). Watch the `auth` block of the status page and the `auth: raw friends keys ... since start` log line before setting `--raw-key-cutoff`.

The server checks at most 500 signatures a second (bursts of 1000). Under more load a title
service call whose signature waits for a check is not taken for a forged one: the server checks
it again for up to a second, and only then answers that it is busy. `refused busy` and `busy after
retries` in the `auth` block count those.

## Why a player cannot sign in

The game logs every refusal as `FORZA_LIVE: server error N: <text>`, and the server logs each
refused sign-in (`refused HELLO from <address> xuid <XUID> '<gamertag>': <reason> (error N)`) at
most once per address and reason every 5 minutes:

| Error | Meaning |
|---|---|
| 1 `not registered` | a packet from an address with no sign-in (the server restarted, or the game spoke before its HELLO): the game signs in again at once. Harmless when `signed in to` follows. |
| 2 | another protocol version |
| 3 `server full` | the player cap, or 16 players from one address |
| 4 | kicked or banned. For 10 minutes the game's other packets get error 4 with the same text too, not error 1. |
| 5 | the server requires event-unlock support (`--event-unlocks`) and the game has none |
| 6 `too many sign-ins from this address` | over 2 new sign-ins a second from one address: the game's next HELLO may get in |

A HELLO that cannot be read is logged as `malformed`.

## What holds the event loop

The server is one event loop: the game's datagrams, its title-service calls, the website's
routes and the operator's all take turns on it. Whatever runs long there pauses every player
meanwhile (the scoreboards and the notifications were written whole on it until #69 and #73). The
server times every piece of work on the loop and logs, as a warning, the ones that held it longer
than `--stall-limit` (50 ms; `0` turns all of this off):

    loop: datagram PRESENCE from #12 'Gamertag' held the event loop 61 ms (limit 50 ms)
    loop: title Scoreboard.SubmitScore held the event loop 132 ms (limit 50 ms)
    loop: http GET /community/gallery.json held the event loop 75 ms (limit 50 ms)
    loop: operator POST /admin/scores/* held the event loop 210 ms (limit 50 ms)
    loop: task expire held the event loop 58 ms (limit 50 ms)
    loop: the event loop was 180 ms late, held by work that no entry point times or by many short pieces of work (limit 50 ms)

The word after `loop:` is the kind of work, then what it was:

| Kind | What it names |
|---|---|
| `datagram` | the packet type (`HELLO`, `RELAY`, `MATCH_SEARCH`, ...) and, for a signed-in player, `#id 'gamertag'` as the other datagram lines do; never an address |
| `title` | the title service and method the game called (`Hopper.GetGameOptions`); no player |
| `http` | any other route, the status page and the website's included: its method and the route the server knows, without the query, ids or file names (`/community/img/*`); `other` for a path the server has no route for, so that no word of a requester's own is shown |
| `operator` | an operator route (`/admin/...`, `/operator/...`, `/gift`, `/notify`, `/broadcast`, `/world/event`), the same way |
| `task` | a periodic task (`keep_written`, `keep_counted`, ...) or a step of the main loop's once-a-second tick (`expire`, `maybe_save_state`, `friends.maybe_save`, `world_tick`, `events.flush`, ...) |
| `stall` | the loop was late and nothing timed explains it (below) |

What is timed: each datagram from the moment the server gets it to the end of its handler; each
stretch a connection's task runs between two waits, which for the game's title calls and the web
and operator routes is the whole answer from the request read to the answer written (an answer
that has to wait for its reader, or a signature check that waits for the budget, is two stretches,
each timed on its own and named after its request); every other task's stretches the same way; and
the tick's steps one at a time. A heartbeat wakes every 25 ms and notices when it woke late: what
is late beyond the units already logged is a `stall` line. That catches the rest, the work no
entry point times: the main loop between its timed steps, the transports' own work, a callback
of the loop, or many short pieces of work that together held it (a line each would be one under
the limit). The volunteer relay mode (`--relay-only`) has no watchdog.

The worst ones also say where they were. The work is over when it is measured, so a watcher thread
looks at the loop while it is held: once the loop has been held `--stall-stack` (250 ms; `0`: never),
it logs the loop's stack, innermost first, once per stall, while the work still runs. The line of
the work that held it then ends with the same number. An example (the clubs' file is far from that
size today; the line numbers are those of the running version):

    loop: stall 3: the event loop has been held 262 ms so far, at write_json (core/durable.py:85) < save (social/clubs.py:60) < accept_clan_invites (social/clubs.py:183) < service_call (title_services/lsp.py:974) < handle (title_services/lsp.py:1216) < lsp_answer (server.py:1866) < ...
    loop: title Xls.User.AcceptClanInvites held the event loop 498 ms (limit 50 ms) (stall 3)

A stall that never ends (the loop stuck for good) still has its stack line.

At most 10 lines a minute of each kind (the stack lines are a kind too, `stack`) are logged; the rest are counted, and once the minute is
over one line says how many were left out and which of them was the worst:

    loop: 37 more datagram line(s) were left out in that minute; the worst of them held the loop 812 ms: datagram MATCH_SEARCH from #12 'Gamertag'

The status page has the numbers without the log, and nothing about who it was (for an operator
route, not even which: only the kind, `"kind": "operator"`, with no `what`):

    "event_loop": {"limit_ms": 50, "over_limit_last_hour": 4, "over_limit_since_start": 19,
                   "worst_last_hour": {"kind": "title", "what": "Scoreboard.SubmitScore", "ms": 132, "seconds_ago": 840}}

(`{"limit_ms": 0}` when it is off.) Timing costs about 0.15 microseconds a datagram and 0.25 a
task's stretch. A machine that is busy with other work makes the loop late too: a `stall` line
with nothing else around it, now and then, can be the machine rather than the server.

## Website accounts

The project website's accounts (sign-in with Discord, GitHub or an emailed code) are a separate
service in [`accounts/`](../accounts/README.md), with its own image, database and settings. It
runs behind the website's nginx at `/api/` and never on the game server's ports or tunnel. Its
README lists every setting, what to register with Discord, GitHub and the mail relay, an nginx
example, the daily upkeep and backups. To link accounts to players' games, count likes and list
players' own photos and designs, it reads this server's data folder (`friends.json`, `players/`,
`banned_xuids.json`, `community.json`, `designs.json`) through a read-only mount, and never writes
to it.

Players delete their own photos and designs from the account page: the service asks this server's
[owner routes](title_services.md#a-players-own-items-forza_livecontentownerspy), which need
`FORZA_OWNER_TOKEN`. Give both the same random value (`openssl rand -hex 32`), never the admin
token, and let the accounts container reach the HTTP port directly, from an address that is not a
`--trusted-proxy` (the routes refuse the tunnel and proxies, as moderation does). Without the token
the routes are off and the account page cannot delete.

## Stopping the server

`docker compose stop` (SIGTERM) and Ctrl+C in a terminal (SIGINT) stop the server the same way.
The signal only asks for the stop: the server ends what it is doing at that moment (a datagram, a
request, a tick of its timers) and then stops, in this order:

1. `stop: SIGTERM: the server stops` in the log (`SIGINT` for Ctrl+C).
2. The HTTP port takes no new connection from here. Those already open end once the saves are
   done.
3. The [event loop's watchdog](#what-holds-the-event-loop) is turned off: the saves hold the loop,
   on purpose.
4. The saves: the registrations (`live_state.json`), the co-op ledger and friends, then
   `notifications.json` and the boards' `scoreboards.json` last, each with its own line
   ([scores on disk](#scores-on-disk), [notifications on disk](#notifications-on-disk)).
5. The Discord status says the server is offline (when it is set up), the UDP port closes, and
   the process ends with exit status 0.

So a stop ends with the notifications' line and then the boards' (after a second signal, one
more line, below):

    stop: SIGTERM: the server stops
    notifications: notifications.json written whole at the stop in 0.14 s: it has every change: 29310 notifications for 3998 players, 5 broadcasts
    scoreboards: scoreboards.json written whole at the stop in 0.32 s: it has every change: 412 boards, 50123 rows, 812 unranked rows, 1500 players in the 1000 Club

A stop signal that comes while the server starts (while it reads its files, or while Python is
still loading its code) is noted: the start reads every file whole, and the server then stops as above, the first line being
`stop: SIGTERM during the start: the server stops now that it is done`. A start that refuses to
run (a file it cannot read) still ends with exit status 1 and its own line. A volunteer relay
(`--relay-only`) has nothing to save: it logs the first line and ends with exit status 0.

**A second signal changes nothing.** Once the stop has begun, every further SIGTERM or SIGINT (a
second `docker compose stop`, Ctrl+C pressed again) is noted and ignored until the process ends:
no save is cut short, and the log says so once for each part of the stop it came in, the first
line before the notifications' line, the second after the boards' line (the stop's last line
then):

    stop: SIGTERM again while the registrations, co-op ledger and friends were written: ignored until they were
    stop: SIGTERM again while notifications.json and scoreboards.json were written: ignored until they were

The exit status stays 0, even for a signal that comes as the process ends.

The stop takes a fraction of a second, most of it those last two writes; the compose file gives it
20 s (`stop_grace_period`). Only a kill ends it earlier: Docker's at the end of that time,
`docker compose kill`, or SIGKILL. A kill costs the boards and the notifications nothing (their
journals have every change) and the other stores what changed since their last save; and it
leaves `scoreboards.json` and `notifications.json` complete only together with their journals,
which matters before a rollback ([scores on disk](#scores-on-disk)).

Versions before this one stopped through an exception raised wherever the server was when the
signal came; inside code whose errors Python only prints (`Exception ignored in ...` in the log),
the stop was lost: the server ran on until Docker killed it 20 s later, without the stop's saves.

## Backups and rollback

Everything the server keeps (players, friends, clubs, scores, photos, designs, moderation lists) is in the data folder. Stop the server for a moment so every file is complete, copy the folder, and start it again (the commands are in the [hosting guide](hosting.md#keeping-it-running)). To restore a backup, stop the server, replace the data folder with the backup's, and start it.

To roll back a bad update, check out the previous version of this repository, rebuild with `docker compose up -d --build`, and restore the data folder from before the update if the new version changed what it keeps. Before going back to a version from before the scores' journal, or from before the notifications' journal, put each journal in its file first ([scores on disk](#scores-on-disk) and [notifications on disk](#notifications-on-disk) have the commands). Before going back to a server without PROXY protocol support, turn the tunnel's PROXY setting off first ([configuration](configuration.md#proxy-protocol-v2-hole-punching-through-the-tunnel)).

### The previous copies (`.bak`)

Most files of the data folder (`friends.json`, `live_state.json`, `scoreboards.json`, the players'
records, the indexes of photos, videos and designs, and the others) are saved whole beside the
one in place and renamed over it, and the file they replace is kept as `<name>.bak`. The `.bak` is
itself replaced in one step (a second link made as `<name>.bak.tmp` and renamed over it): a kill or
a power cut at any moment leaves the file in place and a `.bak`, the one before or the new one. A
`<name>.bak.tmp` left by a kill is removed at the next save. On a file system without hard links
there is no `.bak`: the saves go on without one.

When a file is empty, damaged, or cannot be read at all (a read error of the disk, permissions
that refuse the server), the server reads its `.bak` instead, with a warning in the log:
`... scoreboards.json cannot be read ([Errno 5] Input/output error); using its previous copy
scoreboards.json.bak` (`is damaged` for damaged content). The next save of that file then keeps
that `.bak` as it is rather than make the file that failed the new `.bak`; the save after it
rotates them as usual again. A file that is not there is not looked for in its `.bak`. When
neither can be used, each store does what it did before: most start without what the file held
and log it, and the boards refuse to start when the file cannot be read, or is damaged with a
`.bak` that is there but cannot be read (next section).

### Scores on disk

The boards are more than one file in the data folder:

- `scoreboards.json`: every board, the unranked rows and the 1000 Club, as plain JSON with a
  row to a line. It grows with the boards (26 MB on the public server), so the server does not
  write it at every score, which stopped everything else for as long as the write took. A
  thread writes it whole once a minute while there is something new (`scoreboards:
  scoreboards.json written whole in 0.41 s, off the event loop` in the log), and the server
  writes it one last time as it stops.
- `scoreboards.json.bak`: the file before that one.
- `scoreboards.journal`: the latest changes, a line each (a new best, a ghost's id, a 1000 Club
  medal, an operator's unrank or delete), flushed to disk before the game is answered. It has
  every change `scoreboards.json` lacks, and those its `.bak` lacks. Its first line names the
  files it continues, by SHA-256. It holds a few minutes of changes at most: each whole write
  drops what both files already have (with no `.bak`, what `scoreboards.json` has). While a
  `.bak` is there but cannot be read, it keeps every change instead, and the log says so once
  (`scoreboards.json.bak cannot be read: ... scoreboards.journal keeps every change until it can
  be`): make the `.bak` readable again, or remove it.
- `ghosts/`: the Rivals ghosts, a file each, as before.

A start reads `scoreboards.json` and then the journal's lines it lacks (`scoreboards: 37 changes
from scoreboards.journal on top of scoreboards.json`). So a kill, a crash or a power cut at any
moment loses no score or ghost a player was told was kept; a line the stop cut in half was
never acknowledged and is dropped.

The stop says in the log what it did with the file, in one line; the first two end with what
the boards hold:

- `scoreboards: scoreboards.json written whole at the stop in 0.32 s: it has every change: 412
  boards, 50123 rows, 812 unranked rows, 1500 players in the 1000 Club`
- `scoreboards: nothing to write at the stop: scoreboards.json has every change: 412 boards, ...`
- `scoreboards: ... not written at the stop (...); its journal has every change`, a warning (a
  full disk, for example).
- `scoreboards: ... not written at the stop (...), and the last changes could not be added to its
  journal either (...): those are lost, the journal keeps the changes before them`, a warning:
  the disk refused the journal's last line too. What is lost is what the journal never took:
  changes whose own save had failed already, or a new name waiting for the next change.
- `scoreboards: no scoreboards.json in /data, and no journal to apply: nothing was read, nothing
  to write at the stop`: there were no boards in that folder at all (`scoreboards.json in /data
  could not be read` when the file was there but damaged, with no `.bak`).

After one of the first two, `scoreboards.json` alone is complete. After the third, or with none
of them (a kill, a crash, a stop that ran out of its 20 s), it is complete only together with
`scoreboards.journal`. This version loses nothing either way; a version from before the journal
does (rolling back, below).

The stores that have no journal (the registrations in `live_state.json`, the co-op ledger and
friends) are written first, each whatever became of the others (an error in one is logged as
`stop: the friends could not be written` with that save's own traceback, and the others and the
boards are still written). The notifications' last write comes between those stores and the
boards. A second stop signal (a second `docker compose stop`, Ctrl+C) during any of them is noted
and ignored, and no write is cut short ([stopping the server](#stopping-the-server)).

**A `scoreboards.json` that cannot be read.** When the file is there but cannot be read (a read
error, permissions) the boards come from its `.bak` and the journal, as above. When the `.bak`
cannot be used either, the server does not start rather than run on empty boards, whose next
write would replace both copies. It stops at once, before it opens a port, with exit code 1 and
this line, and with `restart: unless-stopped` Docker starts it again and it stops again with
the same line until the file is put right:

    /data/scoreboards.json cannot be read ([Errno 13] Permission denied: '/data/scoreboards.json'), and there is no previous copy scoreboards.json.bak that can be: not started, rather than run without what it holds. Make it readable again, or move it out of the folder to start without it

The same holds when the file is damaged (empty, zeros after a power cut, cut short) and its
`.bak` is there but cannot be read (its owner or mode, a read error): the `.bak` may have every
board, and a start on empty boards would make the damaged file the next `.bak`. The line is then

    /data/scoreboards.json is damaged (Expecting value: line 1 column 1 (char 0)), and its previous copy scoreboards.json.bak is there but cannot be read ([Errno 13] Permission denied: '/data/scoreboards.json.bak'): not started, rather than run without what they hold. Make the .bak readable again, or move both out of the folder to start without them

Nothing in the folder is changed, renamed or put aside meanwhile. Fix what the error names (the
file's owner or mode, the disk) and start again: with the `.bak` readable, the boards come from it
and the journal. To start without those boards on purpose, stop the container and move
`scoreboards.json` (and `scoreboards.json.bak`, if there is one) out of the data folder: the next
start finds no file and starts empty, and a journal that continued the file is kept as
`scoreboards.journal.unused.<n>` (below). A file whose content is damaged, with no `.bak` (or a
`.bak` damaged too), is not refused: the boards start with what the journal has on top of
nothing, as before, and the log says `scoreboards.json cannot be read (...): starting without what
it held`.

What that means for you:

- **Stopping**: `docker compose stop`, and let it end. The boards are written last, after
  everything else the server saves. A second `docker compose stop` (or Ctrl+C) while it stops is
  harmless: it is logged and ignored, and the stop goes on as for one. What ends a stop early is a
  kill (`docker compose kill`, or the 20 s running out), which leaves `scoreboards.json` complete
  only with its journal: the stop's last line says which it was.
- **Backups**: the whole data folder, with the server stopped. A copy of `scoreboards.json`
  taken while the server runs is not complete without `scoreboards.journal` beside it: by
  itself it lacks up to a minute of scores. If you must copy from a running server, copy
  `scoreboards.json` first and `scoreboards.journal` right after it, and keep the two together.
- **Restoring or editing `scoreboards.json`**: with the server stopped. A journal that
  continues another file than the one it finds (a file restored without its journal, or edited)
  is not applied: the file is taken as it is, and the journal is renamed
  `scoreboards.journal.unused.1` (the next one `.2`, and so on: none is written over; delete
  them once you have looked). The log says so, with what the server knows of the journal's
  changes; whether the file it found has them, it cannot know.
- **Going back to an older `scoreboards.json`** (its `.bak`, or the file of a backup, to undo
  something): remove `scoreboards.journal` as well. The journal continues the `.bak` too, so
  left in place it is applied again on top of the copy you put back, and what you wanted to
  undo is done again.
- **Rolling back to a version from before the journal**: that version reads `scoreboards.json`
  alone. So before any rollback, with the server stopped and the image still this version's,
  put the journal's changes in the file. It takes a second and needs no ports:

      docker compose stop
      docker compose run --rm --entrypoint python3 forza-live -c "import logging; logging.basicConfig(level=logging.INFO); from forza_live.competition import scores; scores.configure('/data'); scores.STORE.load(); scores.STORE.stop()"

  It must end with one of the first two lines above, which say how many boards and rows it
  read; after a clean stop it finds nothing to write, and that is how you know. A last line that
  says `no scoreboards.json in ...: nothing was read` means the folder is not the data folder
  (or has no boards): do not roll back before it reads yours. Then check out the previous
  version and run
  `docker compose up -d --build`. Without Docker, run the same `python3 -c "..."` from the
  repository's folder with your data folder in place of `/data`.

  A rollback without that step, after a stop that was not clean, costs what only the journal
  had:
  - the scores, ghosts and 1000 Club medals of the last minute;
  - where a best was improved in that minute, the row before it comes back, naming a ghost file
    the server already deleted: rivals get no ghost for that row until its player sets a new
    best;
  - rows an operator unranked or deleted in that minute are on the boards again;
  - the ghost id counter goes back, so the previous version gives its next ghosts ids that were
    already used.

  And it is for good once the previous version has written `scoreboards.json`, which it does at
  its first new score: at the next update the journal no longer continues the file, so it is
  put aside and not applied. As long as the previous version has not written the file, starting
  this version once more still applies the journal.
- **Starting the boards from nothing**: delete `scoreboards.json`, `scoreboards.json.bak` and
  `scoreboards.journal` with the server stopped.

`friends.json` is one file as before, written whole at most every 2 s after a change: by a
thread too, from a copy the server takes in a few milliseconds.

### Notifications on disk

The Message Center's notifications (a rival who beat a player's time, gifts, club news, event
rewards, the operator's messages and broadcasts) are kept the way the boards are:

- `notifications.json`: every notification a player has not cleared and that has not expired,
  each player's read and cleared broadcasts, and the event rewards' receipts, as plain JSON
  with a notification to a line. It is 11 MB on the public server: it was written whole at every
  notification sent, read or cleared, which stopped everything else for a quarter of a second
  each time, and a club's news (to each member) as many times over. A thread writes it whole once
  a minute while there is something new (`notifications: notifications.json written whole in
  0.15 s, off the event loop`), and the server writes it once more as it stops, before the
  boards. The thread pauses after each millisecond of its work, so a notification sent during
  that write takes about what it takes at any other time, 2 ms, most of it the disk's flush (the
  boards' writes pause the same way).
- `notifications.json.bak`: the file before that one.
- `notifications.journal`: the latest changes, a line each (notifications sent, read or
  cleared, a broadcast, a receipt), flushed to disk before the game or the caller is answered.
  It works as `scoreboards.journal` does, and a start reads the file and then the lines it lacks
  (`notifications: 12 changes from notifications.journal on top of notifications.json`). A kill,
  a crash or a power cut loses no notification that was sent, and no read or clear the game was
  answered for: an event's reward is marked delivered only once its notification is on disk,
  and a gift's notification is how its player collects it.

**What is dropped, and when.** A notification expires 30 days after it was sent (the
`expirationDate` the game is given); from then on the game is never shown it again. Expired
notifications are left out of memory at the start (`notifications: 40 expired ones left out
(the game is shown none of them)`) and out of the file at its next write. Nothing else is
dropped: a notification that was delivered and read stays until the player clears it or it
expires, since the Message Center lists what each poll returns, and a gift is still collected
from a message that was read. The receipts stay too (a few dozen bytes each): they keep an
event's reward from being sent twice.

The stop's line is one of:

- `notifications: notifications.json written whole at the stop in 0.14 s: it has every change:
  29310 notifications for 3998 players, 5 broadcasts`
- `notifications: nothing to write at the stop: notifications.json has every change: ...`
- `notifications: ... not written at the stop (...); its journal has every change`, a warning.
- `notifications: no notifications.json in /data, and no journal to apply: nothing was read,
  nothing to write at the stop`

As for the boards, a `notifications.json` that is there but cannot be read, with no `.bak` that
can be, stops the start with exit code 1 and `/data/notifications.json cannot be read (...),
and there is no previous copy notifications.json.bak that can be: not started, ...`; the
previous version started without it and wrote over it at the next notification. Backups, a
restored file and the `.unused.<n>` journals go as in [scores on disk](#scores-on-disk).

**Rolling back to a version from before this journal**: that version reads
`notifications.json` alone. After a clean stop the file has everything. Otherwise, with the
server stopped and the image still this version's, put the journal in the file:

    docker compose stop
    docker compose run --rm --entrypoint python3 forza-live -c "import logging; logging.basicConfig(level=logging.INFO); from forza_live.social import notifications; notifications.configure('/data'); notifications.STORE.load(); notifications.STORE.stop()"

It ends with one of the first two lines above. Without that step a rollback after a stop that
was not clean loses the last minute's notifications, and reads and clears of that minute come
back unread or uncleared. Those notifications are lost for good, and their ids are given out
again: the file's next id is the one from its last write, and the previous version counts on from
it. The previous version writes the file at its first notification, after which the journal no
longer continues it and is put aside at the next update.

### The other stores

`clubs.json`, `objectives.json` (the daily and weekly challenges), `stats.json` and the
market's `auctions.json` and `gifts.json` are still written whole at the change that needs them,
before the game is answered, but as compact JSON in one step of the encoder: half a megabyte took
20 ms of the event loop indented, 6 to 8 ms now, most of it the disk's flush. Every version reads
them the same.

A player's record (`players/<xuid>.json`, a few KB) is written whole on the event loop as
before, at each sign-in, race result and profile update: 4 ms, most of it the disk's flush. A
new player's first record also counts the folder against the players' limit, which takes 14 ms
with 20,000 players and their `.bak` copies in it.
