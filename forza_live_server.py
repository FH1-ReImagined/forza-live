#!/usr/bin/env python3
"""Forza Live: the server's command line. The server is the forza_live package (README.md, Layout).

    python3 forza_live_server.py [--port 21200] [--http-port 21201]

`python3 forza_live_server.py --help` lists every option.
"""

import sys

try:
    import sqlalchemy  # noqa: F401 - the server's one dependency (forza_live/core/storage.py)
except ImportError:
    sys.exit("Forza Live needs SQLAlchemy: python3 -m pip install --require-hashes -r "
             "requirements.txt (README.md, Development)")

from forza_live.server import run  # noqa: E402

if __name__ == "__main__":
    run()
