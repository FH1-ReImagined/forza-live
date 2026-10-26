# Forza Live

The online server of [Forza Horizon 1: ReImagined](https://github.com/FH1-ReImagined/ForzaHorizon1-ReImagined):
a self-hostable stand-in for Xbox LIVE and Turn 10's title services, so the PC port can play
online. One Python 3 program, standard library only, nothing to install.

- **UDP 21200**: sign-in, presence, rendezvous (UDP hole punching) and a relay for the players
  who cannot reach each other directly.
- **TCP 21201**: the title services the game asks Turn 10's servers for (the Horizon sign-in,
  Find Game playlists, online XP, scoreboards, leaderboards and Rivals ghosts, car clubs,
  photos, notifications) and a JSON status page at `/`.
- Friends and text chat, the Horizon World (a shared free roam with festival events), the
  Co-op Festival campaign, head-to-head challenges, the players' shader lists.

The public server is `forza-reimagined.playit.plus:56677`, the launcher's default. The
launcher's **This PC** mode runs this server on the player's own machine; it comes with every
release of the game's launcher (the game's repository has this one as its `server/` submodule).

## Run it

    python3 forza_live_server.py --http-port 21201

or with Docker:

    docker build -t forza-live .
    docker run -d --name forza-live --restart unless-stopped --network host \
        -e FORZA_ADMIN_TOKEN=choose-a-token -v /path/to/data:/data forza-live --host 0.0.0.0

Players set the server's address in the launcher (Settings, Online, My own server). Ports,
tunnels (playit.gg), the operator endpoints, the leaderboards and the data folder are in
[`docs/hosting.md`](docs/hosting.md). `python3 forza_live_server.py --help` lists every option.

## Layout

| Path | What |
|------|------|
| `forza_live_server.py` | the UDP server, the HTTP port and the command line |
| `forza_live_lsp.py`, `forza_xtsw.py`, `forza_xtsw_all.py` | Turn 10's title services and their wire format |
| `forza_*.py` | friends, chat, stats, scores and leaderboards, clubs, photos, market, notifications, the Horizon World, the co-op campaign, challenges, shader lists, limits |
| `playground_rounds.xml` | the round presets the playlists serve |
| `test_*.py` | unit tests |
| `docs/hosting.md` | running a server |
| `docs/title_services.md` | the title services as the game calls them (the protocol) |
| `tools/server_probe.py` | checks a server from outside (status page, UDP HELLO -> WELCOME) |
| `tools/xtsw_decode.py` | decodes the requests a server saved with `--dump-requests` |

## Tests

    python3 -m unittest discover -p 'test_*.py'

`test_forza_shader_lists` also compares the server's copy of the shader list format with its
definition in the game's repository (`tools/pipeline_list.py`): it runs when this repository is
the game's `server/` submodule, or with `FORZA_REPO_TOOLS=/path/to/ForzaHorizon1-ReImagined/tools`,
and is skipped otherwise. CI (`.github/workflows/tests.yml`) runs the tests on every push and
pull request, with those files checked out, and builds the Docker image. `server-probe.yml`
checks the public server from outside every 6 hours.

## Changing the server

The game's repository pins a commit of this one in `server/`. A change that the game or the
launcher needs lands here first; then the game's repository moves its submodule to it
(`git -C server fetch && git -C server checkout <commit> && git add server`). Keep the server
compatible with released games: players update the launcher on their own schedule.

## History

The server lived in `server/` of the game's repository until October 2026; its history came
along when it moved here.

## License

[MIT](LICENSE). Forza Horizon is (c) Microsoft. This project is not affiliated with, endorsed by
or sponsored by Microsoft, Playground Games or Turn 10 Studios, and the server talks to none of
their services.
