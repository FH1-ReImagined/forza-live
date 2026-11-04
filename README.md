# Forza Live

**[Host your own server](docs/hosting.md#how-to-make-your-own-forza-live-server)**: the
step-by-step guide (Docker Compose, the ports, playit.gg, updates and backups); then
[configuration](docs/configuration.md), [operating](docs/operating.md) and
[website data](docs/website-data.md).

Forza Live is the online server of
[Forza Horizon 1: ReImagined](https://github.com/FH1-ReImagined/ForzaHorizon1-ReImagined). It
stands in for Xbox LIVE and Turn 10's title services, so the PC port can play online: sign-in,
friends and chat, the Horizon World, the Co-op Festival, leaderboards and Rivals, car clubs,
photos and designs. One Python 3 program, standard library only.

- **UDP 21200**: sign-in, presence, rendezvous (UDP hole punching) and a relay for the players
  who cannot reach each other directly.
- **TCP 21201**: the title services the game asks Turn 10's servers for, a JSON status page at
  `/status`, and the community photos and designs the website shows.

The launcher's default is the public server; its **This PC** mode runs this server on the
player's own machine, and **My own server** connects to one you host.

## Layout

| Path | What |
|------|------|
| `forza_live_server.py` | the entry point: `python3 forza_live_server.py --help` lists every option |
| `forza_live/server.py` | the server: the UDP port, the HTTP port, the command line |
| `forza_live/core/` | signed requests (`auth`, `ed25519`), the signed list of servers (`server_list`), the volunteer relays' list, secrets and tickets (`relays`), abuse limits (`limits`), saves that survive a power cut (`durable`) |
| `forza_live/network/` | the title services' wire format (`xtsw`) and all its records (`xtsw_all`), the PROXY protocol behind a tunnel (`proxy`), a volunteer relay (`relay_node`, `--relay-only`) and the main server's side of the relays (`relay_broker`) |
| `forza_live/title_services/` | Turn 10's title services on the HTTP port (`lsp`) and the playlists' round presets (`playground_rounds.xml`) |
| `forza_live/social/` | friends, chat, car clubs, Message Center notifications, the Discord status message |
| `forza_live/competition/` | scores and leaderboards, Rivals ghosts, daily and weekly challenges (`objectives`), community events, head-to-head challenges, Xbox LIVE stats |
| `forza_live/world/` | the Horizon World, who hosts it, clubs in it, the Co-op Festival and its results |
| `forza_live/content/` | photos and community photos, designs and their previews, the Auction House and gifts, shader lists |
| `tests/` | the unit tests, one folder per `forza_live/` folder (`tests/server/` for `server.py`); shared helpers in `tests/support.py` |
| `docs/hosting.md` | the step-by-step guide to running a server |
| `docs/configuration.md` | every option and environment variable, Docker, tunnels, limits |
| `docs/operating.md` | running a public server: operator endpoints, moderation, score admin, the server list, backups |
| `docs/website-data.md` | what the server publishes for a website |
| `docs/title_services.md` | the title services as the game calls them (the protocol) |
| `docs/server_events.md` | organizer-approved community events |
| `tools/server_probe.py` | checks a server from outside (status page, UDP HELLO -> WELCOME) |
| `tools/xtsw_decode.py` | decodes the requests a server saved with `--dump-requests` |
| `tools/event_admin.py` | manages community events on a server |
| `accounts/` | the website accounts service for forza.dyotson.dev/api/ (Django, django-allauth, django-ninja): its own dependencies, image and tests ([`accounts/README.md`](accounts/README.md)) |
| `screening/` | the screening worker for community photos and designs (its own Docker image, [`screening/README.md`](screening/README.md)) |
| `Dockerfile`, `compose.yaml`, `.env.example` | the server's image and how to run it |

## Development

Run the server from a checkout (Python 3.12 or newer, nothing to install):

    python3 forza_live_server.py --http-port 21201

Run the tests:

    python3 -m unittest discover -s tests -t .

`tests/content/test_shader_lists.py` and `tests/content/test_design_feed.py` also compare the
server's code with the game repository's `tools/` (`pipeline_list.py`, `fxobj_shaders.py`,
`xdc_decode.py`): they run when this repository is the game's `server/` submodule, or with
`FORZA_REPO_TOOLS=/path/to/ForzaHorizon1-ReImagined/tools`, and are skipped otherwise. The
website accounts service has its own (`cd accounts && python manage.py test members`, with its
`requirements.txt`), and so does the screening worker (`python3 -m unittest discover -s screening -p 'test_screen_*.py'`,
with numpy and Pillow). CI (`.github/workflows/tests.yml`) runs them all on every push and pull
request, on Linux and Windows, and builds the Docker images; `server-probe.yml` checks the public
server from outside every 6 hours.

Developer docs: [title services](docs/title_services.md) (the protocol as the game calls it) and
[community events](docs/server_events.md).

The game's repository pins a commit of this one in `server/`. A change that the game or the
launcher needs lands here first; then the game's repository moves its submodule to it
(`git -C server fetch && git -C server checkout <commit> && git add server`). Keep the server
compatible with released games: players update the launcher on their own schedule. The server
lived in `server/` of the game's repository until October 2026; its history came along.

## License

Forza Live is free software under the
[GNU Affero General Public License, version 3 or later](LICENSE) (AGPL-3.0-or-later). If you run a
modified version for other people over a network, you must offer them its source (section 13).
Releases and commits before this license change were published under the MIT License and stay
available under it; see [`NOTICE.md`](NOTICE.md). The game and its launcher are under the GNU
General Public License, version 3 or later.

Forza Horizon is (c) Microsoft. This project is not affiliated with, endorsed by
or sponsored by Microsoft, Playground Games or Turn 10 Studios, and the server talks to none of
their services.
