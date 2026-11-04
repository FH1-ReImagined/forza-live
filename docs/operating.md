# Operating a public Forza Live server

Day-to-day running of a server other people play on: the operator endpoints and the [operator panel](#operator-panel), chat, photo and design moderation, the score admin, the list of community servers, and backups. Options are in [configuration](configuration.md); the first setup is in the [hosting guide](hosting.md).

## Operator endpoints

(The moderation of community photos and designs has stricter rules: [below](#community-photos-and-designs).)

`GET /gift?to=<gamertag|XUID>&credits=<n>[&message=..]` or `&car=<id>`,
`GET /notify?to=..&type=..`, `GET /broadcast?type=0x100&Message=..[&days=1-30]` (one
Message Center notification for every player, new ones included, stored once), and `GET /world/event?session=all|<id>[&event=<id>]` are
operator-only. They are answered for requests from the server's own machine (loopback, not
through a reverse proxy), or with `FORZA_ADMIN_TOKEN` (header `X-Admin-Token: <token>` or
`?token=<token>`); everything else gets 403. From the host's terminal:

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
  gift forms. With `FORZA_ACCOUNTS_ADMIN_URL` set, it links to the player in the website accounts
  service's admin (accounts are managed there: suspend, unlink, void likes).
- **Bans and chat**: the content bans (`banned_xuids.json`, with the reason given in the panel),
  timed sign-in bans and chat mutes (`moderation.json`), and the recent chat.
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
| `POST /admin/photos/ban/<XUID>`, `unban/<XUID>` | the ban list | Bans and chat; a player's page |
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
([signed requests](configuration.md#signed-requests)); another game claiming that XUID gets no list. A player with no friends, requests or blocks is forgotten after 180 days.
Limits: 200 friends, 50 requests waiting each way, 200 blocks per player; 10 requests a
minute (bursts of 5), 60 other changes a minute, 30 searches a minute; searches answer at
most 20 players. A request to a player who blocked the sender looks sent but is dropped,
and so are invites from a blocked player. The status page shows how many players,
friendships and open requests there are. Games up to v0.1.14 still see everyone online as a
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

## Community photos and designs

Photos and designs players share can appear on a website ([what is published](website-data.md#community-photos)). Moderating them has stricter rules than the other operator endpoints, and the [screening worker](#automatic-screening) can do the first pass.

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

A ban goes into `banned_xuids.json` in the data folder (the list the design storefront uses
too, a JSON list of hex XUIDs that can also be edited by hand): the player's new uploads are not
published and their photos are served to nobody until the ban is lifted. For a screening worker
(a separate container with the token, at an address that is not a trusted proxy):
`GET /admin/photos/queue` lists the held photos, oldest first; `POST /admin/photos/<id>/hold`
takes a live photo back to the queue, `POST .../release` publishes a held one (a photo the
operator hid cannot be released, 409), and both take an optional `{"note": "..."}` kept for the
operator.

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
...)`), or leaves it held for you, logging why. Only a confident hit hides a photo. A race number
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

## Backups and rollback

Everything the server keeps (players, friends, clubs, scores, photos, designs, moderation lists) is in the data folder. Stop the server for a moment so every file is complete, copy the folder, and start it again (the commands are in the [hosting guide](hosting.md#keeping-it-running)). To restore a backup, stop the server, replace the data folder with the backup's, and start it.

To roll back a bad update, check out the previous version of this repository, rebuild with `docker compose up -d --build`, and restore the data folder from before the update if the new version changed what it keeps. Before going back to a server without PROXY protocol support, turn the tunnel's PROXY setting off first ([configuration](configuration.md#proxy-protocol-v2-hole-punching-through-the-tunnel)).
