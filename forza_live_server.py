#!/usr/bin/env python3
"""Forza Live: the server's command line. The server is the forza_live package (README.md, Layout).

    python3 forza_live_server.py [--port 21200] [--http-port 21201]

`python3 forza_live_server.py --help` lists every option.
"""

if __name__ == "__main__":
    import signal

    # A stop signal while the server's code is still being read is noted, and stops the server
    # once it has started (forza_live.server.run, #78): as a container's first process it would
    # otherwise be ignored, and a stop right after a start lost.
    came = []
    for name in ("SIGTERM", "SIGINT"):
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), lambda number, frame: came.append(number))

    from forza_live.server import run
    run(came)
