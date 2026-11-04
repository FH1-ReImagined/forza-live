# How to make your own Forza Live Server

This guide sets up your own Forza Live server with Docker Compose, so you and your friends can
play online on it. Every option, the tunnels, the operator endpoints and what the server
publishes are in the pages listed under [Going further](#going-further).

## Pre-requisites

- A PC with at least 2 GB of RAM. The server itself uses about 100 MB with players online;
  the rest is for the operating system and Docker.
- A headless Linux operating system with docker, docker compose and git installed (Windows is
  not recommended).
- A router with the option to open UDP ports (or use [playit.gg](https://playit.gg)).

## Steps

### Preparing

1. On the PC, clone the Forza Live repository and go into it:

       git clone https://github.com/FH1-ReImagined/forza-live.git
       cd forza-live

2. Copy the example settings file and set the operator token (the password of the server's
   operator endpoints) to a long random value:

       cp .env.example .env
       sed -i "s/^FORZA_ADMIN_TOKEN=.*/FORZA_ADMIN_TOKEN=$(openssl rand -hex 32)/" .env

   Every other setting goes in `.env` too (the file has examples), so `compose.yaml` stays as
   it is and updates never conflict with your changes.

3. Optional, for the Horizon World's events and the Co-op Festival: copy the game's database
   from your own game files (`media/db/gamedb.slt` in the extracted game) into `data/`, and
   turn it on in `.env`:

       mkdir -p data
       cp /path/to/your/game/media/db/gamedb.slt data/
       echo "FORZA_GAME_DB=/data/gamedb.slt" >> .env

   Without it the server works, but the Co-op Festival playlist is not offered and the
   Horizon World has only a few default events. The file is not included here: it is part
   of the game.

### Starting the server

4. Build and start it:

       docker compose up -d --build

   The server restarts by itself after a crash or a reboot (`restart: unless-stopped`).

5. Check that it runs. The log should end with `Forza Live listening on UDP 0.0.0.0:21200`
   and `title servers and status page on http://0.0.0.0:21201/`:

       docker compose logs --tail 20

   and the status page answers with JSON (`"server": "forza-live"`, the number of players):

       curl http://127.0.0.1:21201/status

### Letting players in

6. Open the two ports on your router and forward them to this PC's LAN address:

   | Port  | Protocol | Use |
   |-------|----------|-----|
   | 21200 | UDP      | the game: sign-in, rendezvous, relay |
   | 21201 | TCP      | the title services and the status page |

   If the PC has a firewall, open them there too (for example `sudo ufw allow 21200/udp` and
   `sudo ufw allow 21201/tcp`). Players forward nothing.

   **No UDP on your connection** (some ISPs and CGNAT drop it)? Use a
   [playit.gg](https://playit.gg) tunnel instead of the router:

   1. Run the playit agent on the same PC and create two tunnels to the PC's LAN address: one
      UDP to port 21200 and one TCP to port 21201. Note the public address and port of each.
   2. Tell the server about the tunnel in `.env`: the agent's address (the PC's own LAN
      address when the tunnels point at it, plus `127.0.0.0/8` if they point at 127.0.0.1:
      the playit agent forwards from changing addresses in 127.x.x.x, not only 127.0.0.1),
      and the public port of the TCP tunnel:

          FORZA_TRUSTED_PROXIES=192.168.1.10,127.0.0.0/8
          FORZA_PROXY_HTTP_PORT=<the TCP tunnel's public port>

   3. Restart with `docker compose up -d`. Players then use the UDP tunnel's public address.

   Without `FORZA_PROXY_HTTP_PORT` a tunnelled game says "The Horizon server is currently
   unavailable". Players behind the tunnel use the server's relay; with PROXY protocol v2 they
   can connect directly to each other again: see
   [Behind a tunnel](configuration.md#behind-a-tunnel-playitgg) and
   [PROXY protocol v2](configuration.md#proxy-protocol-v2-hole-punching-through-the-tunnel).

7. Check it from outside your network (a friend's PC, or a phone hotspot), with Python 3 and a
   copy of this repository:

       python3 tools/server_probe.py your.public.address:21200

   Both lines should say PASS. (It checks the TCP port one above the UDP port: give it the
   UDP tunnel's address only when the TCP tunnel's port is that one plus 1.)

8. Players connect in the launcher: **Settings**, **Online**, **My own server**, then type
   your server's address (`host`, or `host:port` when it is not 21200). Optional: list the
   names players type in `.env` (`FORZA_SERVER_NAMES=your.domain.example`, comma separated,
   without the port), so the server refuses signatures made for other servers.

### Keeping it running

9. **Updating.** Get the new version and rebuild; the data folder stays, and the players in a
   lobby or a Horizon World stay in it across the restart:

       cd forza-live
       git pull
       docker compose up -d --build

10. **Backups.** Everything the server keeps (players, friends, clubs, scores, photos,
    designs) is in `data/`. Stop the server for a moment so every file is complete, and copy
    the folder:

        docker compose stop
        tar czf forza-live-data-$(date +%F).tar.gz data
        docker compose start

    To restore one, stop the server, replace `data/` with the backup's, and run
    `docker compose up -d`.
### Troubleshooting

- **`docker compose` is not found**: install Docker's Compose plugin (the
  `docker-compose-plugin` package on most distributions).
- **The container stops right away**: `docker compose logs` says why. A port in use means
  another server already runs on this PC (an older `docker run` container, or the launcher's
  This PC mode): stop it, then `docker compose up -d` again.
- **The game says "The Horizon server is currently unavailable"** (no playlists, no
  leaderboards): TCP 21201 is not reachable from outside, or, behind a tunnel,
  `FORZA_PROXY_HTTP_PORT` is not set to the TCP tunnel's public port.
- **On Windows or macOS with Docker Desktop**, players from other PCs show up in the log as
  `127.0.0.1` and their games say the Horizon server is unavailable. Docker Desktop runs
  containers in a hidden Linux VM, and `network_mode: host` puts the server on that VM's
  network, not the PC's. Run the server without Docker instead: install Python 3 (3.12 or
  newer, with "Add python.exe to PATH" ticked), then in this folder run
  `py forza_live_server.py --host 0.0.0.0 --http-port 21201 --data-dir data`, and allow
  Python on private networks when Windows Firewall asks. Settings from `.env` go on the
  command line there (`py forza_live_server.py --help` lists them).
- **Behind a tunnel, the game still says the Horizon server is unavailable**, and the
  game's `forza.log` shows `title server ... (HTTP port 21201)` instead of your TCP tunnel's
  port: the server didn't count the player as coming through the tunnel. Check that
  `FORZA_TRUSTED_PROXIES` covers the agent's addresses (`127.0.0.0/8` for an agent on the same
  PC) and that `FORZA_PROXY_HTTP_PORT` is set. The status page's `via_proxy` counts players
  who came through it.
- **Players sign in but cannot see or join each other**: UDP 21200 is not forwarded (check
  with `tools/server_probe.py` from outside). Players behind a tunnel go through the
  server's relay, which works but adds a little latency.
- **No Co-op Festival playlist**: the game's database is missing (step 3). The log says
  `co-op campaign: no events` at the start.
- **Anything else**: `docker compose logs -f` follows the log while a player connects;
  `curl http://127.0.0.1:21201/status` shows who is on.

---

## Getting listed

The launcher's Servers page and the Discord status message list community servers, with their
players and ping. To be listed, fill in the
[List my community server](https://github.com/FH1-ReImagined/forza-live/issues/new?template=list-my-server.yml)
form. It asks for the name, address, region, a contact and how the server runs, and for the rules:

- Your server keeps its own friends list and leaderboards. Only the main server's leaderboards
  are the official ones.
- Player data stays private, as the
  [privacy note](https://github.com/FH1-ReImagined/ForzaHorizon1-ReImagined/blob/main/PRIVACY.md)
  describes, and nothing is sold on the server.
- You keep it reasonably up to date with forza-live main.
- You say when it shuts down. A listed server that stays offline for a few days is taken off the
  list and can apply again.

We check that the status page (`http://host:<port + 1>/status`) answers from outside, that it
runs a current version and that players can reach its title services, then list it and answer
on the issue. The list is signed again within a minute, so the launcher shows it the next time
it fetches the list.

## Going further

- [Configuration](configuration.md): every option and environment variable, Docker, tunnels, limits, restarts.
- [Operating a public server](operating.md): the operator endpoints, moderating photos and designs, leaderboard admin, backups.
- [Data for a website](website-data.md): the leaderboards, the map, community photos and designs, shader lists.
- [Title services](title_services.md) and [community events](server_events.md): the developer docs.
