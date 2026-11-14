# Forza Live server configuration

Every option and environment variable a host may set, with the Docker, tunnel and limit details behind them. For a first setup, follow the [hosting guide](hosting.md).

## Options at a glance

Options are command line flags of `forza_live_server.py` (`--help` lists them all); most have an environment variable, which goes in `.env` with Docker Compose.

| Option | Environment variable | What it does |
|--------|----------------------|--------------|
| `--host ADDR` | | the address to listen on (`0.0.0.0` for all) |
| `--http-port PORT` | | the TCP port of the title services and status page (default 21201) |
| | `FORZA_ADMIN_TOKEN` | the password of the [operator endpoints](operating.md): a long random value (`openssl rand -hex 32`). The examples' placeholders are not taken as a token, and the start warns about one under 16 characters |
| | `FORZA_PANEL_GIFT_MAX` | the most credits one gift from the [operator panel](operating.md#operator-panel) may carry (default 1000000) |
| | `FORZA_ACCOUNTS_ADMIN_URL` | the website accounts service's admin page, linked from the operator panel (never fetched) |
| | `FORZA_OWNER_TOKEN` | the token of the [website accounts](operating.md#website-accounts) service, for players deleting their own photos and designs (unset: off) |
| `--game-db FILE` | `FORZA_GAME_DB` | the game's `gamedb.slt`, for world events and the Co-op Festival |
| `--trusted-proxy IP/CIDR` | `FORZA_TRUSTED_PROXIES` | addresses of a [tunnel agent](#behind-a-tunnel-playitgg) (repeatable; comma separated in the variable) |
| `--proxy-http-port PORT` | `FORZA_PROXY_HTTP_PORT` | the TCP port the tunnel publishes |
| `--proxy-protocol-udp` | `FORZA_PROXY_PROTOCOL_UDP=1` | read [PROXY v2](#proxy-protocol-v2-hole-punching-through-the-tunnel) headers on UDP |
| `--proxy-protocol-tcp` | `FORZA_PROXY_PROTOCOL_TCP=1` | read PROXY v2 headers on TCP |
| `--rate-limit` | | the relay budget per player (256 KB/s), also the cap in a volunteer relay's tickets |
| `--relay-after SECONDS` | | how long two players relay through this server before they get a [volunteer relay](operating.md#volunteer-relays) (default 8) |
| `--relay-only` | | run as a [volunteer relay](operating.md#running-a-relay) of a main server, with `--relay-of HOST:PORT`, `--relay-id ID` and `--relay-secret-file FILE` |
| `--relay-allow-private` | | a relay also forwards to private and loopback addresses (a test on one LAN only) |
| | `FORZA_DISK_CAP_MB` | cap of ghosts, cars and market blobs (default 4096) |
| | `FORZA_MAX_PLAYERS` | cap of player records, and of the records with a key in `friends.json` (the players' keys, friends and blocks); as many again may have no key (a name another player blocked or asked) (default 20000) |
| `--no-invites` | `FORZA_INVITES=0` | drop every game invite |
| `--server-name HOST` | `FORZA_SERVER_NAMES` | the names players type to reach this server ([signed requests](#signed-requests)) |
| `--rebind-legacy` | `FORZA_REBIND_LEGACY=1` | let an XUID bound to a friends key move to the first signed game |
| `--raw-key-cutoff UNIX_TIME` | `FORZA_RAW_KEY_CUTOFF` | refuse friends keys sent raw after this time |
| `--upgrade-cutoff UNIX_TIME` | `FORZA_UPGRADE_CUTOFF` | no more upgrades from a raw friends key to a public key after this time (default: 30 days after `--raw-key-cutoff`) |
| `--server-id NAME` | `FORZA_SERVER_ID` | the id written on score rows ([which results count](website-data.md#which-results-count)) |
| `--daily-challenges` | `FORZA_DAILY_CHALLENGES=1` | run the [daily and weekly challenges](website-data.md#daily-and-weekly-challenges) (off by default) |
| `--daily-challenges-seed TEXT` | `FORZA_DAILY_CHALLENGES_SEED` | the secret seed they are chosen with (default: a random one made at the first start, kept in the data folder) |
| `--daily-challenges-gifts` | `FORZA_DAILY_CHALLENGES_GIFTS=1` | a weekly credits gift for finished challenges (for the main server) |
| `--score-tokens off\|log\|enforce` | `FORZA_SCORE_TOKENS` | what a result faster than the time since its event started does: `enforce` keeps it unranked, `log` (the default) only logs and counts it, `off` skips the check ([score tokens](operating.md#score-tokens)) |
| | `FORZA_COMMUNITY_HOLD=1` | new community photos wait for release ([photos](operating.md#community-photos)) |
| | `FORZA_COMMUNITY_MAX_WIDTH` | width of a published big shot (default 1920) |
| | `FORZA_DISCORD_STATUS_WEBHOOK` | a Discord webhook for the [players-online message](#players-online-in-discord) |
| | `FORZA_PUBLIC_ADDRESS` | the address that message shows |
| | `FORZA_DISCORD_STATUS_NAME` | what that message calls this server (default: the public server) |

Forza Live is a standard-library Python server: UDP rendezvous/relay plus the HTTP title
services (Turn 10's services, kept-alive connections) and a JSON status page. The server is the
`forza_live/` package of this repository, started by `forza_live_server.py` in its root.

## Ports

Paths: the server is the `forza_live/` package of this repository, started by
`forza_live_server.py` in its root (in the game's repository this repository is the `server/`
submodule). `tools/server_probe.py`, `tools/xtsw_decode.py` and `tools/event_admin.py` are here; the other
`tools/` and `docs/` paths are in [ForzaHorizon1-ReImagined](https://github.com/FH1-ReImagined/ForzaHorizon1-ReImagined), and the
website's files in [website](https://github.com/FH1-ReImagined/website).

| Port | Protocol | Use |
|------|----------|-----|
| 21200 | UDP | game, rendezvous, relay |

## Docker

    docker build -t forza-live .        # in a checkout of this repository
    docker run -d --name forza-live --restart unless-stopped --network host \
        -e FORZA_ADMIN_TOKEN=choose-a-token \
        -v /path/to/data:/data forza-live --host 0.0.0.0

Put a long random value of your own in place of `choose-a-token` (`openssl rand -hex 32` makes
one): the placeholder itself is not accepted as a token.

Use `--network host`: with a bridge network the players' public ip:port is hidden behind
Docker's UDP proxy. The optional world events and the Co-op Festival (its festival events
with CPU drivers; without the database the playlist is not offered) need the game's

Any directory works as the data folder (`-v /path/to/data:/data`).

## Behind a tunnel (playit.gg)

When the ISP drops unsolicited UDP from the internet (TCP still works), put a tunnel agent
in front. Run the agent on the same machine with `--net=host` and create two tunnels to the
host's LAN IP: UDP 21200 and TCP 21201. Every player then reaches the server from the agent's
local address, each on its own source port. Start the server with that address as a trusted
proxy:

    docker run -d --name forza-live --restart unless-stopped --network host \
        -e FORZA_ADMIN_TOKEN=choose-a-token -v /path/to/data:/data \
        forza-live --host 0.0.0.0 --http-port 21201 --proxy-http-port 50001 \
        --trusted-proxy 192.168.1.10 --trusted-proxy 127.0.0.0/8

`--proxy-http-port` is the TCP port the tunnel publishes (for example 50001 for UDP 50000): the server
tells it to players that arrive through a trusted proxy as their title servers' port (the
game takes that port from the welcome, so without it a tunnelled game connects to 21201 on
the tunnel's address, finds nothing and says "The Horizon server is currently unavailable":
no playlists, no leaderboards, no XP). Players connecting directly still get `--http-port`.
(`--trusted-proxy` is repeatable and takes IPs or CIDRs; `FORZA_TRUSTED_PROXIES` takes a
comma separated list.) Add 127.0.0.0/8 only if the agent forwards to 127.0.0.1 (playit's agent forwards from changing 127.x.x.x addresses, so 127.0.0.1 alone is not enough); use the host's
LAN IP as the tunnel target otherwise. Players then set `live_server` to the tunnel's public
address. For a trusted proxy:

- the per-address limits (16 players, 32 HTTP connections, the sign-in rate) cannot count its
  players by address, as they all share the agent's: without PROXY protocol (below) only the
  server-wide caps hold them, and anyone who opens many connections through the tunnel can use
  those up. With `--proxy-protocol-udp` and `--proxy-protocol-tcp` each player is counted under
  the address its header names, as if it connected directly. The datagram and error rates are
  per tunnelled player (per source port) either way, and every player's relay budget stays;
- its players are told no public address, so other players do not punch to the agent's
  socket and their traffic goes through the server relay, unless the tunnel sends PROXY
  protocol headers (below). Players connecting directly keep hole punching. The log says
  `via proxy` on joins and the status page has `via_proxy`;
- its address no longer counts as "this machine" for the operator endpoints (a tunnelled
  request from 127.0.0.1 would otherwise be admin): use `FORZA_ADMIN_TOKEN`.

Without the option nothing changes. Only list addresses you control: a trusted address gets
no per-address protection.

### PROXY protocol v2 (hole punching through the tunnel)

With a tunnel's Proxy Protocol set to `proxy-protocol-v2` (playit.gg: the tunnel's page,
Origin Configuration, Proxy Protocol; each tunnel has its own), the agent tells the server
each player's public address in a PROXY v2 header (`forza_live/network/proxy.py`): on UDP once per flow,
as a datagram of its own before the flow's first one (agents up to 0.15.26 put it in front
of every datagram; 0.16.0 to 0.16.4 swap its two addresses: use 0.16.5 or newer); on TCP
once at the start of the connection. The server reads headers only when told the tunnel
sends them, as the PROXY protocol requires (a player can send the same bytes itself):

- `--proxy-protocol-udp` (env `FORZA_PROXY_PROTOCOL_UDP=1`): a header from a trusted proxy is
  taken only as the first datagram of a flow: of an agent socket not heard for 2 minutes
  (the agent closes a flow after at most 106 s of silence), and not in the server's first 2
  minutes (a flow then may be older than the server). Never in the middle of a flow. The
  address belongs to the first game whose HELLO on that flow is accepted (its token and
  XUID); another game on the same socket without a new header gets none and uses the relay,
  and so does any player once its agent is no longer a `--trusted-proxy`. Repeated legacy
  inline headers only confirm the flow's first address; a changed address clears it and
  leaves traffic relayed until a fresh flow. A valid standalone header on a legacy flow
  switches it to the newer header-once format, including across server restarts, so client
  bytes after an agent upgrade cannot be interpreted as more agent headers. A player with an
  address gets it in its welcome and the others get it to punch to, as for a player
  connecting directly; the relay stays the fallback (symmetric NATs). Only global addresses
  count. A flow without a header (older than the setting or than the server) is relayed as
  before; players get their address the next time they start the game. The address is kept
  across a restart, as the agent's flow goes on without a new header, for the same
  token and XUID, while the agent is still a `--trusted-proxy` and the flow map has room.
- `--proxy-protocol-tcp` (env `FORZA_PROXY_PROTOCOL_TCP=1`): every connection from a trusted
  proxy must start with a header, or it is closed: so reach the operator endpoints from an
  address that is not a trusted proxy (the LAN, with the token). The header's address is the
  one logged and counted for the per-address connection limit; admin still needs the token.

With the headers the per-address limits are the player's own again:

- UDP: a flow's games count under the address its header gave for the 16 players per address,
  the sign-in rate, the new keys an address may bind and its signature checks
  ([signed requests](#signed-requests)). The header only repeats what the datagrams said, and
  an address is often many players', so a place under an address is held against others only by
  a game that answered the challenge the server sent there (a game with signed requests, once
  it is signed in). When an address is full, the registration there that has waited longest
  without answering gives way to a newcomer with signed requests, so sign-ins made in a
  player's name never keep that player's own game out. Flows are counted the same way: besides
  those of its games that answered, one address may have 64 (the flows of games just restarted,
  of games before signed requests); over that the one opened longest ago is refused for the
  newest (its datagrams are dropped, and it is tried again every 2 s). Flows without an address
  (older than the server or than its first 2 minutes, or opened while the flow table was full)
  cannot be told apart: together they may hold half of `--max-peers` (at least 16 players),
  never the whole server, and bind 32 new keys at once and then 2 a minute. After a restart
  longer than the saved state lasts (10 minutes), the players who sign in during the first 2
  minutes are such flows, so keep `--max-peers` at twice the players you expect at once, or more.
- TCP: connections count under the header's address (an IPv6 address under its /64) for the 32
  per address, and so do signature checks and wrong operator tokens.

Everything else (the relay budget, admin, auth) stays as above.
The status page then counts `via_proxy_address` (players with a PROXY address; per player
`proxy_address`) and `proxy_protocol` (headers taken on UDP and HTTP, headers refused, flows
known), with no IP address; the log says `via proxy, public address from PROXY` on such joins.

Order (each setting on the playit side and its option go together):

1. Deploy the server without the options: it is the server it was.
2. UDP: update the agent to 0.16.5 or newer, set the UDP tunnel to `proxy-protocol-v2`, then
   restart the server with `--proxy-protocol-udp`. In between, the server drops the header
   datagrams as before (no outage); players who join then, or in the restarted server's first
   2 minutes, stay relayed until they next start the game. Never run the option without the
   tunnel's setting: a player's first datagram could then name any address.
3. TCP (optional: log lines and limits per player): set the TCP tunnel to `proxy-protocol-v2`
   and restart the server with `--proxy-protocol-tcp` right after. In between, title-service
   connections through the tunnel fail (the server without the option cannot read them, and
   with it refuses connections without a header), so do it when few players are on.

Before going back to a server without the option (an older one, a rollback), turn the
tunnel's setting off: for TCP always (an older server closes every tunnelled connection),
and for UDP too with an agent at 0.15.26 or older (an older server drops every datagram that
starts with a header). And never leave an option on once its tunnel's setting is off.

## Restarting the server

A restart does not disturb the players. The server writes who is registered (player id,
token, XUID, gamertag, room, addresses, presence, the sessions each player hosts or is in,
the id counter and the epoch) to `live_state.json` in the data folder: after a change (at
most every 2 s), every 30 s while players are on, and on shutdown (Ctrl+C, SIGTERM,
`docker stop`). A server that starts and finds a file at most 10 minutes old loads it. The
games send HELLO with the same token every 5 s from the same socket, so they find their
registration, keep their player id and the epoch, and their lobby or Horizon World goes on
(the relay, the standby host and the world's host are all restored). Players that do not
come back are dropped after 30 s as usual. No game update is needed: v0.1.12 and later
behave the same. Keep the data folder across restarts (`-v ...:/data`); without it, or after
a crash that left no recent file, the old behaviour applies (the games sign in again under new
ids and a lobby breaks up). Not kept: a head-to-head challenge in progress, a world event
that was requested but not taken.

## Limits (all untrusted input)

- per address (an IPv6 address counts under its /64; a tunnelled player's own with [PROXY protocol](#proxy-protocol-v2-hole-punching-through-the-tunnel);
  without it the tunnel's players cannot be counted by address): 16 players, 2500 datagrams/s,
  2 new registrations/s, 32 HTTP connections (they stay open while idle: the game needs that),
  16 new keys at once and then 16 an hour, 20 signature checks a second
  ([signed requests](#signed-requests)). A sign-in over the registration rate gets
  "server error 6: too many sign-ins from this address" (the game tries again by itself), and
  so does a registered game that names another XUID faster than that;
- per player: relay budget 256 KB/s (`--rate-limit`), 30 open auctions, 50 unretrieved
  gifts, 100 shared cars, 4 game invites per 30 s (the rest are dropped and logged);
  one upload at most 2 MB (a signed game's photo mode big shot or movie: 32 MiB, in 256 KiB
  requests), community videos 5 a day and 50 in all, in a disk budget of their own,
  `FORZA_VIDEOS_CAP_MB` (2048) ([website data](website-data.md#community-videos)),
  HTTP bodies at most 8 MB;
- whole server: ghosts, cars and market blobs at most `FORZA_DISK_CAP_MB` (default 4096) MB,
  at most `FORZA_MAX_PLAYERS` (20000) player records and as many records of keys and friends,
  30 new player records an hour for callers with no key on record
  ([title services](title_services.md)); 1024 HTTP connections, of which 768 at most that are
  not kept for a player (a browser's, the website's, a game older than the keys). A new
  connection over either takes the place of the one of those that has waited longest (or, when
  all of them are being answered, of the one open longest), so connections that only sit there
  keep nobody out. A connection is kept for its player once a title call on it was signed by the
  player's own key, for as long as that player is signed in (or 15 minutes after its last signed
  call), and one key keeps the 4 it used last: a game's own is never closed for a newcomer, and
  no key holds more than a few;
- the status page lists gamertags and XUIDs but no IP addresses.

- shader lists ([website data](website-data.md#shader-lists-from-players)): 1 MiB and 500 pipelines per upload, 5000 pipelines per player and UTC day,
  8 storage versions and 200000 pipelines per version.

The two disk totals (`FORZA_DISK_CAP_MB` and the videos' `FORZA_VIDEOS_CAP_MB`) are kept in
memory. The server counts the stored files once as it starts, in a thread, and says so in the
log (`disk usage: uploads: 25001 files, 930.4 MB (counted in 4.6 s, off the event loop)`); from
then on every upload adds to the total and every delete takes from it, and the files are counted
again every 5 minutes, in a thread too, to put right what the server could not see: a file
copied into the data folder or deleted from it by hand, the video converter's results. A recount
that finds the running total 1 MB or more off logs the difference. Until the first count is done
the uploads are taken up to 64 MB in all, so a store that was already full is passed by that
much at most. Counting never happens on the server's event loop: a count of 25,000 files takes
seconds on a slow disk, and nothing else was answered meanwhile.

Players are not authenticated (the XUID is whatever the client claims), except for the friends
list, the shader lists and the title service writes, which need a signature by the key bound to
the XUID ([signed requests](#signed-requests)).

### Names are plain ASCII

A name of combining marks, foreign scripts or emoji would be drawn on every other player's
screen, so only ASCII reaches the game. `forza_live.core.limits.clean_gamertag` keeps letters, digits
and single spaces (at most 15, like an Xbox 360 gamertag) after transliterating accents
(`José` becomes `Jose`); when nothing is left the name is `Driver` plus 4 hex digits of
the XUID. It runs on the HELLO name, the sign-in (Logon2) name and every name read back
from the data files, and again when a name is written into a reply (`forza_live/network/xtsw.py`), so
stored names can never reach the game unclean. Club tags (4), names (20), descriptions (128)
and photo titles and descriptions are printable ASCII (0x20-0x7E; an empty club name
becomes `Club <id>`). Text chat drops control characters, bidi overrides, zero-width
characters and combining marks and is at most 200 characters. The launcher's Gamertag
setting and the game apply the same rule. Names the game puts inside its own peer-to-peer
session data are not seen by the server.

Game invites are delivered only between games that let their player pick whom to
invite and ask the invited player before joining (v0.1.13 and later; they say so in their
HELLO). Games up to v0.1.12 invite every player online with one press of Invite Friends and
join every invite they get at once (a player in free roam has their game restarted into the
party): their invites are dropped, and none are delivered to them. One player's invites are
capped at 4 per 30 s. `--no-invites` (env `FORZA_INVITES=0`) drops every invite; a server

The relay limit is 256 KB/s per player (bursts of 4 s): a game sends each peer one datagram a
frame, about 100 bytes per car it runs, so a player in a full Horizon World (7 peers) needs about
106 KB/s through the relay at 144 fps and 181 KB/s at 240 (measured with four games, #12;
[`online_vision.md`](https://github.com/FH1-ReImagined/ForzaHorizon1-ReImagined/blob/main/docs/online_vision.md), "Three and four players"). Players with a direct path to each other do
not use the relay.

## Signed requests

Games and launchers with signed requests have an Ed25519 key pair (`signing_seed` in the
player's `live_identity.txt`) and sign every HELLO and HTTP request with it (`forza_live/core/auth.py`,
`forza_live/core/ed25519.py`). The server keeps only the public key: nothing it receives or stores can
sign. Games up to 0.2.x sent their friends key itself in every HELLO and title service call
instead, so a tunnel, anyone on the path or another server's operator could copy it, and this
server kept its SHA-256.

- Binding: an XUID with no key on record is bound to the public key of its first signed HELLO.
  An XUID bound to a friends key moves to a public key with one upgrade: the game sends the raw
  friends key once, in a signed HELLO, and the server checks it against the SHA-256 on record.
  From then on that XUID's friends key is refused, so a copied key stops working when its owner
  upgrades. Games send the upgrade only to the official server's names; on other servers an XUID
  bound to a friends key stays off friends and chat until the operator lets the new game in
  (`--rebind-legacy`, or by clearing `key` in the player's `friends.json` record while the
  server is stopped). An upgrade or sign-in with another public key than the one on record is
  refused, and the game is told the account is linked to another installation: recovery is by
  hand.
- `--server-name HOST` (repeatable; env `FORZA_SERVER_NAMES`, comma separated): the names
  players type to reach this server, without the port. Signatures name the server they were
  made for, and the server refuses other names, so a signature made for one server is no use on
  another. Players use the same key on every server: without the option every name is taken, and
  a server whose players reach it by name should always list it (the log warns). A server reached as
  `play.example.org` runs with `--server-name play.example.org`.
- `--rebind-legacy` (env `FORZA_REBIND_LEGACY=1`): an XUID bound to a friends key goes to the
  first signed game that signs in with it, without an upgrade. Only for a server nobody else
  uses: the launcher's "This PC" server runs with it.
- `--raw-key-cutoff UNIX_TIME` (env `FORZA_RAW_KEY_CUTOFF`): from then on, games and launchers
  that still send the friends key are refused for XUIDs that have a key on record: their friends
  list and chat stay off and their title service writes fail (reads still work). A raw key binds
  no new XUID after it either. Default: taken until each player upgrades. `auth: raw friends keys
  ... since start` in the log every 10 minutes, and `auth` on the status page (`raw_http`,
  `raw_hello`, `raw_key_players`, `upgraded`, `registered`), count them, so the cutoff can wait
  until old games are rare. Set it: a raw key was sent in the clear to every server its player
  used, and until its owner upgrades, whoever kept a copy can use it here.
- `--upgrade-cutoff UNIX_TIME` (env `FORZA_UPGRADE_CUTOFF`; default: 30 days after
  `--raw-key-cutoff`, none without one): from then on an XUID still bound to a raw friends key is
  no longer moved to a public key by that key, because a copied key could do it before its owner
  does. Its player's game is told the account is linked to another installation, and the operator
  releases it by hand as for any such account (clear `key` in the player's `friends.json` record
  while the server is stopped; the next signed game that signs in with the XUID binds it). Every
  upgrade until then is a warning in the log, with the address it came from.
- New keys have a budget. An XUID with no key on record is bound to the first key that signs in
  with it, at most 16 at once from one address (then 16 an hour) and 2 a second for the whole
  server; the tunnelled games without an address share one budget, 32 at once and then 2 a
  minute; raw friends keys, which bind with one datagram whose source may be forged, 10 at once
  and one a minute, counted apart from their address's signed games. Over a budget the XUID
  stays unbound until a later HELLO (the game says one every few seconds), with a warning in the
  log, and the game is told that friends are off "for now". `friends.json` holds at most
  `FORZA_MAX_PLAYERS` records with a key, and such a record is never forgotten, however long its
  player is away. Records without a key are made only by what other players do (a name someone
  blocked, a request to a player who has not signed in with a key): they have a cap of their
  own, as large, never take a new player's place, and the oldest that hold nothing but a name go
  when there are too many (and after 180 days). A registered game may name another XUID (another
  profile) within the sign-in rate: its own once it has answered the server's challenge, else
  its address's.

Every HTTP answer carries the server's clock (`X-Forza-Time`), and the WELCOME carries it too: a
signature more than 120 s off the server's clock is refused, and a nonce is taken once. A check
costs about 1 ms of CPU; at most 500 a second are made for the whole server, and each is charged
first to who asks for it: 20 a second (100 at once) for one address and for one TCP connection,
a few for each player's HELLOs, and 100 a second for all the tunnelled clients without an address
together. An address whose signatures fail 40 times in a minute gets no checks from its budget
for a minute, with one warning in the log that names it; nothing is counted against the XUID a
request names, which anyone can write. An address is not a player, though: a household, a
carrier's NAT or a VPN exit is many, and behind a tunnel a UDP source is its sender's word. So
the address's budget and its failures decide only what the unproven get. A connection or
registration whose signature for an XUID checked out has that key's own budget for it (2 a
second, 30 at once) whenever its address's is spent or stopped, and one that has had no check
yet gets its first from a reserve of the server's (which only a signature that fails uses up):
whoever shares an address with a flood of bad signatures still proves its key and goes on. A
HELLO for an XUID bound to another key, or signed before its address was sent its challenge, is
refused without a check. So floods of bad signatures cannot take the CPU, nor one sender the
other players' checks. A reverse proxy in front of `--http-port` must pass the request line and
body unchanged.

Rollout: the server first (it takes signed requests and friends keys alike), then the game and
launcher release, then `--raw-key-cutoff` on a date announced to the players. A game with signed
requests against a server without them keeps its keys to itself: on the official server's names
friends and chat stay off; any other server gets a key made from the friends key and its name,
useless anywhere else.

## Players online in Discord

Set `FORZA_DISCORD_STATUS_WEBHOOK` to a channel webhook URL (Discord: channel settings >
Integrations > Webhooks). The server posts one message there and edits it when the number
of players or open Horizon Worlds changes (and every 10 minutes); on shutdown it says the
server is offline. The message id is kept in the data folder (`discord_status.json`), so a
restart edits the same message. `FORZA_PUBLIC_ADDRESS` sets the address it shows. With
Docker, keep the URL in an env file: `--env-file /path/discord.env`.

With [daily and weekly challenges](website-data.md#daily-and-weekly-challenges) on, the message
ends with a short Challenges block: today's and this week's objectives, each with how many
players finished it. A finished objective edits the message; nothing else is posted for them.
When the message is over Discord's 2000 characters, lines go from the end first (the challenges
before the servers), and the last line always stays.

The community servers in the [server list](operating.md#listing-community-servers) follow in
the same message, each in the same form as this server: up or offline, players, open Horizon
Worlds and online lobbies, from their own `/status` (checked every 2 minutes), and the address.

A community server can post its own message, for example in the project Discord's status channel
with a webhook its staff gives you: set `FORZA_DISCORD_STATUS_NAME` to its name (`US East`, at
most 48 characters) and `FORZA_PUBLIC_ADDRESS` to the address players type. The message then
reads "Forza Horizon 1: ReImagined — US East" and points players to the launcher's Servers page.

