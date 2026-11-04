# Forza Live data for a website

What the server publishes as public, read-only JSON and images on its HTTP port, for a community website to show: leaderboards, the daily and weekly challenges, the map, community photos and designs, shader lists, and the list of servers. Running the server is in the [hosting guide](hosting.md); moderation of photos and designs is in [operating](operating.md#community-photos-and-designs).

## Leaderboards

`forza_live/competition/leaderboards.py` serves the scoreboards as public, read-only JSON on the HTTP port (the
same one as the status page, 21201 by default):

- `GET /leaderboards`: the boards that have scores (`id`, `name`, `unit`, `better`, `players`,
  the class ids on it).
- `GET /leaderboards/<board>?class=<id|letter>&limit=<1-100>`: the top rows (default 50). Board
  ids are `speed-trap-230`, `speed-zone-101`, `event-199` (events and Rivals) and
  `time-trial-7` (monthly Rivals). A row has `rank`, `player` (an opaque id), `gamertag` (the
  ASCII name the game shows), `score` (km/h for speeds, seconds for times), `unit`, `display`,
  `carId`, `car`, `classId`, `class` (D to R1) and `date`.
- `GET /leaderboards/snapshot`: every board with the top 50 of each class, in one document.

Only ranked rows are in the output ([below](#which-results-count)). No XUIDs are in the output. The player id is a
hash with a secret salt kept in the data folder
(`leaderboards_salt.txt`; keep it so ids stay the same across restarts). Answers are cached for
60 s and the server builds at most 120 answers a minute (429 beyond). Replies carry
`Access-Control-Allow-Origin: *`, like the rest of the HTTP port.

The server ships no board or car names: without them boards show as "Speed trap 230" and cars
as "Car 1398". To name them, write `leaderboard_names.json` from your own copy of the game and
put it in the data folder (read again when it changes):

```
python3 tools/extract_map_points.py --game extracted/game --update extracted/tu4 \
    --dlc ~/.local/share/forza --out /tmp/map_points.json \
    --names forza_live_data/leaderboard_names.json
```

The file is `{"boards": {"speed-trap-230": "Rt. 29 Speed Trap", "event-199": "..."},
"cars": {"1398": "2012 Lamborghini Aventador LP700-4"}}` (the in-game names of the speed traps,
speed zones and events, and the cars). `--dlc` is a folder with the DLC packages, installed
(`tools/install_dlc.py`, the game's user data folder) or extracted (a Xenia `content` folder):
the car packs' cars are in their database patches and their names in their string tables. The
cars of the disc 2 car packs ([`disc.md`](https://github.com/FH1-ReImagined/ForzaHorizon1-ReImagined/blob/main/docs/disc.md)) are named only when their packages are there. The website has the same board names in
its `data/map_points.json`, so the page shows them either way; only the boards without a place
on the map (monthly Rivals, events off the Colorado map) and the cars need the server's file.

The website (https) cannot fetch a plain-http tunnel (mixed content), so the `pages` workflow
(the website repository's `.github/workflows/pages.yml`) copies `/leaderboards/snapshot` from
`http://your.host:21201` into the site as `data/leaderboards.json` once a
day (06:23 UTC; run the workflow by hand for a fresh copy sooner), and the website's `leaderboards.html` reads that file. A scheduled run that cannot reach the
server fails and leaves the published site as it was. Nothing else is needed at deploy but the
tunnel publishing the HTTP port. To try the page against a local server:
`leaderboards.html?api=http://127.0.0.1:21201`.

### Which results count

The boards (the game's and this JSON, the rollups and the Rivals) take results only from players
signed in to this server: a title service call that carries its player's own key
(`forza_live.title_services.lsp.caller_trusted`: a signature, or until `--raw-key-cutoff` the friends key of a
game before signed requests). A result from any other caller (a game before the friends key, or
an XUID nobody signed in with here) is kept **unranked**: off the boards, for review, the
player's best one per board. A caller that is refused, because its XUID has a key and the call
does not carry it, leaves nothing at all. So is a
Rivals time whose ghost fails the checks (`forza_live/competition/ghost.py`: sizes, chunks, the version, one
car and the row's, the event's track when the server has the game's database, a lap time and
records that match the score; the game gets a failure and carries on, and the player's previous
row stays). A ghost that passes is kept with its driver's gamertag cleared; the refused ones are
kept in `ghosts/review/` with their unranked rows.

A result also needs the time it claims to have passed since its event started
(`forza_live/competition/score_token.py`, a score token as in osu!). The start is a call the game
already makes, signed by the player: `GetBestScores` as an event's race loads (seen for a
festival race and Rivals), and a speed zone's `GetRows` (view 5) as the car enters the zone. Each result on the board spends its seconds from that start: a time its own, a speed zone
the distance between its gates over the average speed. The results since one start must fit in
the time since it, give or take 2 s and 2%. RESTART on the results screen races again without a
new start, and its lap is paid for by the time that has passed. With `--score-tokens enforce`, a
result faster than that, or with no start, is kept unranked (`start: ...`), and the game is told
nothing went wrong. With `log`, the default, such a result stays ranked and is only logged and
counted; with `off` nothing is checked ([score tokens](operating.md#score-tokens)). Speed traps
have no start (a speed at a single point). The starts are kept in memory: for 30 minutes after
the server starts, a result without one still counts.

Each new row carries the server's id (`--server-id NAME` or `FORZA_SERVER_ID`; without it an id
is made at the first start and kept in `server_id.txt`) and the build the game names on its calls
(`X-Forza-Build: 0.2.0/tu4`, the launcher's version or `git-<commit>`, and the title update;
empty for games before it), and whether the caller was verified. Rows from before the tags stay
as they were: they keep showing, and a rank-again or a new best of their player replaces them.
The 1000 Club counts medals from signed-in players only, and keeps the same tags on each
player's row.

What the game is told about its own best (`GetBestScores`, the personal best of
`GetRivalAndPersonalBest`: it submits a time only when it beats this, FH1 #569) follows from
that. A signed-in player is told its ranked best, so a time that ranks is always submitted, also
after earlier unranked ones. An unverified caller is told the better of its ranked row and its
unranked one, to itself alone, so the game does not send its slower times again; with no other
time on the board, that best is also its own Rivals target. Nothing unranked is ever in a board,
another player's rival, a rollup, the public JSON or the answer to another player, and an
unverified caller's `NotifyRival` tells nobody. A player without a time is told the game's "no
time yet" (FLT_MAX for a time, 0 for a speed): the game submits a time only when it beats the
answer (FH1 #591).

To tag and move rows by source, see the [score admin](operating.md#score-admin).

## Daily and weekly challenges

`forza_live/competition/objectives.py` sets three objectives a day (00:00 to 00:00 UTC) and three a
week (Monday 00:00 UTC to the next Monday), the same for every player of the server. They are off
by default: a server runs them with `--daily-challenges` (`FORZA_DAILY_CHALLENGES=1`). Any server,
a community one too, can turn them on for its own players; the launcher shows only the main
server's, and only results the main server took count there.

Each set is chosen from the module's pool by a secret seed and the period, with sha256, so a
restart gets the same set. The seed is `--daily-challenges-seed`, or without it a random one
made at the first start and kept in the data folder (`daily_challenges_seed.txt`; keep it with
the rest of the data folder, or the next sets change). It is never served, so the coming sets
cannot be worked out from the code. A set is kept once made, so a server update during a day
keeps that day's objectives. Players progress from what the game already sends, with the
leaderboards' rule ([below](#which-results-count)): only calls that carry their player's own key
count.

| Objective | From |
|-----------|------|
| races: won, on the podium, finished, in a class, against other players | `User.WriteRaceStats(Ex)` at every race's end (the place, the grid, the humans, the car's class), at most one race every 20 s a player |
| speed traps and speed zones: a speed at so many different ones, in a class | every pass (`SubmitScore` types 1 and 2), only below the trap's record and 470 km/h (`scores.speed_over_limit`), whether or not `--trap-limits` is on |
| 1000 Club medals (Title Update 4) | `Challenge.ChallengeCompleted`: each (car, challenge) once a period |
| big shots | a photo mode Big Shot published as a [community photo](#community-photos) |

- `GET /challenges`: `{"version": 1, "server", "now", "sets": [{"kind": "daily"|"weekly", "id":
  "daily-2026-11-04"|"weekly-2026-W45", "starts", "ends", "objectives": [{"id", "type", "title",
  "target", "completed"}]}]}`. `completed` is how many players finished the objective; no names or
  ids. `Cache-Control: public, max-age=60`.
- `GET /challenges/progress`: the same with the caller's `progress` and `done` (unix time or null)
  on each objective, and `"player": {"completed": {"daily": n, "weekly": n}}` (the objectives it
  finished so far). Needs a [signed request](configuration.md#signed-requests), like the co-op
  results: 403 otherwise. The launcher's Play page shows it.

**Weekly gift** (`--daily-challenges-gifts`, `FORZA_DAILY_CHALLENGES_GIFTS=1`, off by default; for
the main server): 10,000 CR (`objectives.GIFT_CREDITS`) once a week, sent through the Message
Center as a gift from Forza Live (the game's NEW GIFT message; DOWNLOAD adds the credits to the
save), to a player who has finished 3 objectives (`GIFT_AFTER`) in that ISO week, daily or weekly.
An objective counts toward it only when all of its steps came from a release launcher's build
(`X-Forza-Build` 0.2.0 or later: not a source checkout's `0.1.0`, a `git-<commit>` build or a game
before the header), besides the rule above. The gift is recorded before it is sent, so a week is
never paid twice. With gifts on, `/challenges` has `"gift": {"credits", "after"}` and
`/challenges/progress` the player's `"gift": {"week", "finished", "sent"}`.

Data: `objectives.json` in the data folder: the current and the previous period of each kind
with each player's progress (by XUID, with the builds that made it, no gamertag), each player's
count of finished objectives, and the last 8 weeks' gift records (objectives finished, when the
gift was sent and its id). Results taken by another server never reach this one: a
community server's challenges are its own, and the launcher shows the main server's.

## The map

The page's map of Colorado is our own drawing, not the game's map image:

- the website's `images/colorado-map.svg`: every road of Colorado (the interstates, main and secondary
  roads, dirt roads, the towns and the festival), drawn by `tools/build_map_roads.py` from the
  road layout in a copy of the game: the sat-nav road graph
  (`media/tracks/colorado/colorado.nav`). Its lanes (a freeway's carriageways, one-way pairs)
  are joined into one centreline per road and simplified; roads are drawn by class (freeway,
  main, secondary, dirt). The terrain regions, the reservoir and the landmark icons are drawn
  from `tools/map_terrain.json`. Its colours are the page's CSS tokens (`--map-*`), for the
  light and dark themes.
- the website's `data/map_points.json`: the place and in-game name of every speed trap, speed zone,
  event (Rivals) and landmark, written by `tools/extract_map_points.py` from a copy of the game
  (`--game extracted/game --update extracted/tu4 --out ../website/data/map_points.json`). Only
  coordinates and names.

To redraw the map from your own copy of the game (in the game's repository, with the website
repository checked out beside it as `../website`):

```
python3 tools/build_map_roads.py --game extracted/game \
    --points ../website/data/map_points.json --out ../website/images/colorado-map.svg
```

`--ghosts COPY_OF_DATA_DIR/ghosts` (the server's Rivals ghosts, copied) and `--carlog drive.log`
(`tools/carlog.py`) add players' driven lines where the network has no road.

## Shader lists from players

The launcher's *Prebuild shaders* step builds a list of pipelines before play. Launchers upload
pipeline *descriptions* (shader hashes and decoded render state, plus how to rebuild a shader from
the player's own files; never shader code or game data) and the server publishes what several
players saw ([shader prebuild](https://github.com/FH1-ReImagined/ForzaHorizon1-ReImagined/blob/main/docs/shader_prebuild.md#phase-3-lists-from-players-276)). Both endpoints
are on `--http-port` (`forza_live/content/shader_lists.py`):

- `POST /shader-lists/upload`: JSON `{"version", "pipelines", "shaders"}` in the list format
  (`tools/pipeline_list.py check`). Needs a signature ([signed requests](configuration.md#signed-requests);
  launchers before them send `X-Xuid` and the friends key itself, `X-Friends-Key`): the XUID must have
  joined the server and be bound to that key (a 403 otherwise). Every field is checked against the list format and its enum ranges; a bad
  entry rejects the request (400). Over the size caps: 413, 400; over the daily cap: 429.
- `GET /shader-lists?version=fbo:20260228/20261018`: the merged list of one storage version, for
  anybody. It holds the pipelines reported by at least `MIN_PLAYERS` (3) different players (a
  player counts once per entry, whatever it re-sends; players are kept as a hash of the XUID) and
  the recipes of their shaders, plus an optional seed list.

Data: `shader_lists.json` in the data folder (atomic writes, at most a few MB; keep it with the
rest of the data folder). To seed the published list, put a list for one storage version in
`shader_lists_seed.json` beside it (the launcher's `data/pipelines-4D5309C9.json` fits; entries
are checked on load). Nothing else is needed at deploy: the module ships with the other
`*.py` files (the Dockerfile copies them), and a tunnel must publish the HTTP port as for
the other title services (the launcher reaches it at the server's UDP port plus one).

## Community photos

Photo mode's BIG SHOT ("upload it to ForzaMotorsport.net") becomes a community photo on the
website when the game signs its requests ([signed requests](configuration.md#signed-requests)): the server
publishes a 1920x1080 PNG it makes from the 3840x2160 BMP the game sends, credited by gamertag
and the owner's player id (the leaderboards' opaque id), with no XUID anywhere in the public
output. Details, limits and the feed format:
[title services](title_services.md#community-photos-forza_livecontentcommunitypy). Nothing to set up: the
routes are on the HTTP port with the rest.

- Public: `GET /community/gallery.json` (live photos, newest first, at most 500) and
  `GET /community/img/<id>.png`. The website's `pages` workflow (its `tools/build_gallery.py`,
  `GALLERY_FEED_URL=http://your.host:21201`) copies them, re-encoded, into the
  site; a photo hidden or deleted here leaves the site at its next build.
- Published at once, unless `FORZA_COMMUNITY_HOLD=1` (new photos wait as `held` for the
  [screening worker](operating.md#automatic-screening) or the operator to release them).
  `FORZA_COMMUNITY_MAX_WIDTH` (default 1920) is the width a big shot's copy is halved down to.
  Caps: 10 photos a day and 200 in all per player, 60 an hour on the server, 60 big shot uploads
  started an hour; copies count in `FORZA_DISK_CAP_MB`. Converting a big shot takes the server
  about 0.2 s.
- Big shots are not kept as private files (only the published copy), and the game sends no
  removal: a player who wants a photo gone deletes it from the website's account page (with an
  account linked to their game, through the [owner routes](title_services.md#a-players-own-items-forza_livecontentownerspy))
  or asks the operator.
- Games built from game main up to 3896de8 capture the big shot's tiles wrongly with the default
  `--readback_resolve=some` (nine copies of the loading screen); the server sees that and
  publishes nothing. `--readback_resolve=full` in the game gives the real photo.

Moderating photos: [operating](operating.md#community-photos).

## Community designs

The paint jobs and vinyl groups players share in the game's design storefront
([title services](title_services.md#designs-on-the-website-forza_livecontentdesign_feedpy)) are listed in
the game at once. The website's designs page shows one only after the
[screening worker](operating.md#automatic-screening) has passed its preview: until then it is
`pending`. Every design that was on the server before this version starts `pending` too, at the
first start. A confident screening hit (or the operator) hides the design in the game as well,
as three player reports do. Nothing to set up: the routes are on the HTTP port with the rest, and
the worker screens designs as well as photos.

- Public: `GET /community/designs.json` (designs listed in the game and screened clear, newest
  first, at most 500: public id, type, share code, title, description, gamertag, the creator's
  player id, car name, layers,
  downloads, rating, upload time and the preview's URL, size and SHA-256; no XUID, no payload) and
  `GET /community/design-img/<id>.png` (the preview, 288x160 or 256x128, made by the server from
  the game's texture). Cached and rate limited like the photos' routes.
- Car names: a paint job's car is named only when `leaderboard_names.json` names it (the file of
  [the leaderboards](#leaderboards)); otherwise `car` is null.

Moderating designs: [operating](operating.md#community-designs).

## Servers

`GET /servers.json` is the signed list of servers the launcher's Servers page shows, when the
operator keeps one ([listing community servers](operating.md#listing-community-servers)). The list
itself is the `list` field, JSON as text: `{"issued", "expires", "servers": [...]}`, each server
with `name`, `address`, `region`, `contact`, `verified`, and `id` when known; the server that
publishes it is first, with `"main": true`. A website can show it as it is; the signature is
for the launcher.

`GET /relays.json` is the signed list of [volunteer relays](operating.md#volunteer-relays), in
the same envelope and signed with the same key (format `forza-live-relays-1`, the signature over
`FORZA-RELAYS-V1\n` and the list text): `{"issued", "expires", "relays": [...]}`, each relay with
`id`, `name`, `address` and `region`. The status page's `relays` block says which are up and how
many sessions and players each relays (no addresses).
