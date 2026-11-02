#!/usr/bin/env python3
"""Forza Live: the server's command line. The server is the forza_live package (README.md, Layout).

    python3 forza_live_server.py [--port 21200] [--http-port 21201]

`python3 forza_live_server.py --help` lists every option.
"""

from forza_live.server import run

if __name__ == "__main__":
    run()
