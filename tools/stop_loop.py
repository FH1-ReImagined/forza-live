#!/usr/bin/env python3
"""Stop a busy server over and over, and count the stops that were lost (#78).

Usage: stop_loop.py RUNS [--repo DIR] [--python EXE] [--signals SIGTERM[,SIGINT...]] [--gap S]
                         [--gc-threshold N] [--slow-discord S] [--held SHARE] [--diag]
                         [--out FILE.jsonl]

Each run starts the server (forza_live_server.py of --repo, this repository by default) on
127.0.0.1 with free ports and a temporary data folder, waits for /status, and puts it under load:
HTTP connections opened and closed as fast as one thread can (a third answered, a third dropped
after half a request, a third dropped at once) and junk UDP datagrams, so tasks die and their weak
references are cleaned up all the time. --gc-threshold N runs the server with
gc.set_threshold(N), the garbage collector at work far more often. --slow-discord S turns on a
Discord status (nothing sent) whose update takes S seconds, with the server's ticks 100 times
faster (an update every 0.3 s instead of 30 s): most signals then come while the tick waits for
it, and the stop's saves must not wait for it. --held SHARE: in that share of the runs the
signal comes while a task holds the loop with Python code, 2-4 s (the loop takes the signal
once free) or 8-40 s (the stop breaks into it after its 5 s limit). At a random moment the run
sends the signal(s), --gap seconds apart, and waits 20 s (the public server's stop_grace_period)
for the process to end.

A stop is LOST when the process still runs after those 20 s; it is killed then (with --diag it is
asked for the stacks of all its threads first: faulthandler, in its log). Each run also checks
the stop's saves: every data folder starts with a notification that only notifications.journal
has, so a stop that saves writes notifications.json whole (its line in the log) and
live_state.json, and logs the boards' line, after the notifications' one and with nothing but
an "again" line after it. Logs with a traceback, an "Exception ignored" or a "Task was
destroyed" are counted too. The time of a stop is from the first signal to the process's end.

Prints each run that went wrong and a summary; --out adds a JSON line per run to FILE.jsonl.
Use nice and keep it to batches: each run is about 2 cores for a second or two.
"""
import argparse
import json
import os
import random
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request

STOP_TIMEOUT = 20.0
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# the server run as forza_live_server.py runs it, with what --gc-threshold, --slow-discord,
# --held and --diag add
WRAPPER = ("import gc, faulthandler, runpy, signal, sys\n"
           "if {gc}: gc.set_threshold({gc})\n"
           "if {slow}:\n"
           "    import time\n"
           "    from forza_live import server\n"
           "    from forza_live.social import discord_status\n"
           "    discord_status.enabled = lambda: True\n"
           "    discord_status.update = lambda *a, **k: time.sleep({slow})\n"
           "    discord_status.offline = lambda data_dir: None\n"
           "    real_wait = server.StopRequest.wait\n"
           "    async def wait(self, seconds):\n"
           "        return await real_wait(self, seconds / 100)\n"
           "    server.StopRequest.wait = wait\n"
           "if {held}:\n"
           "    import asyncio, os, time\n"
           "    from forza_live import server\n"
           "    real_main = server.main\n"
           "    async def hold():\n"
           "        while not os.path.exists({trigger!r}):\n"
           "            await asyncio.sleep(0.01)\n"
           "        server.log.info('test: the loop is held')\n"
           "        end = time.monotonic() + {held}\n"
           "        while time.monotonic() < end:\n"
           "            pass\n"
           "        server.log.info('test: the loop is free')\n"
           "    async def main(stop):\n"
           "        asyncio.get_running_loop().create_task(hold())\n"
           "        await real_main(stop)\n"
           "    server.main = main\n"
           "if {diag}: faulthandler.register(signal.SIGUSR1, all_threads=True)\n"
           "sys.argv = sys.argv[1:]\n"
           "runpy.run_path(sys.argv[0], run_name='__main__')\n")
SEED = ("import sys; sys.path.insert(0, sys.argv[1]); from forza_live.social import "
        "notifications as n; n.configure(sys.argv[2]); n.STORE.load(); "
        "n.send(0x0009000000000001, n.NEWS, {'Message': 'before the stop'})")


def free(kind):
    s = socket.socket(socket.AF_INET, kind)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def load(http, udp, stop):
    junk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    n = 0
    while not stop.is_set():
        n += 1
        try:
            c = socket.create_connection(("127.0.0.1", http), timeout=2)
            if n % 3 == 0:
                c.sendall(b"GET /status HTTP/1.0\r\nHost: x\r\n\r\n")
                c.recv(65536)
            elif n % 3 == 1:
                c.sendall(b"GET /sta")
            c.close()
        except OSError:
            time.sleep(0.005)
        if n % 4 == 0:
            try:
                junk.sendto(os.urandom(random.randint(1, 64)), ("127.0.0.1", udp))
            except OSError:
                pass
    junk.close()


def stamp(line):
    """The time of a log line ("2026-11-15 13:13:44,695 ..."), or None."""
    try:
        return time.mktime(time.strptime(line[:19], "%Y-%m-%d %H:%M:%S")) + int(line[20:23]) / 1000
    except (ValueError, IndexError):
        return None


def one(args, signals, template):
    with tempfile.TemporaryDirectory() as top:
        data = os.path.join(top, "data")
        shutil.copytree(template, data)
        http, udp = free(socket.SOCK_STREAM), free(socket.SOCK_DGRAM)
        log_path = os.path.join(top, "server.log")
        trigger = os.path.join(top, "hold")
        held = 0.0
        if random.random() < args.held:  # under the stop's limit, or far past it
            held = random.uniform(2, 4) if random.random() < 0.5 else random.uniform(8, 40)
        wrapper = WRAPPER.format(gc=args.gc_threshold, diag=args.diag, slow=args.slow_discord,
                                 held=held, trigger=trigger)
        with open(log_path, "w") as log:
            proc = subprocess.Popen(
                [args.python, "-c", wrapper, os.path.join(args.repo, "forza_live_server.py"),
                 "--host", "127.0.0.1", "--port", str(udp), "--http-port", str(http),
                 "--data-dir", data],
                cwd=args.repo, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)
        deadline = time.monotonic() + 30
        started = False
        while time.monotonic() < deadline and proc.poll() is None:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/status" % http, timeout=2).read()
                started = True
                break
            except OSError:
                time.sleep(0.02)
        stop = threading.Event()
        loader = threading.Thread(target=load, args=(http, udp, stop), daemon=True)
        loader.start()
        time.sleep(random.uniform(0.2, 1.2))
        if held and started:
            open(trigger, "w").close()
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                with open(log_path, errors="replace") as f:
                    if "test: the loop is held" in f.read():
                        break
                time.sleep(0.01)
            time.sleep(random.uniform(0.2, 1.0))
        sent, sent_at = time.monotonic(), time.time()
        for i, name in enumerate(signals):
            if i:
                time.sleep(args.gap)
            try:
                proc.send_signal(getattr(signal, name))
            except ProcessLookupError:
                pass
        # a blocking wait, timed to the moment the process ends (wait(timeout) polls, with
        # sleeps of up to 50 ms)
        waiter = threading.Thread(target=proc.wait, daemon=True)
        waiter.start()
        waiter.join(STOP_TIMEOUT)
        lost = waiter.is_alive()
        if lost:
            if args.diag:
                proc.send_signal(signal.SIGUSR1)
                time.sleep(1.0)
            proc.kill()
            waiter.join()
        took, ended_at = time.monotonic() - sent, time.time()
        stop.set()
        loader.join()
        with open(log_path, errors="replace") as f:
            text = f.read()
        state = os.path.exists(os.path.join(data, "live_state.json"))
        whole = os.path.exists(os.path.join(data, "notifications.json"))
    lines = text.splitlines()
    saved = (state and whole
             and any("notifications: notifications.json written whole at the stop" in line
                     for line in lines)
             and any("scoreboards:" in line and "at the stop" in line for line in lines))
    # the log's clock: from the signal to the stop's first line (a version without one: its first
    # save line), then to its last save line, then to the process's end (the loop's close, the
    # connections still open, the exit)
    phases = None
    stamps = [stamp(line) for line in lines if " stop: " in line or "at the stop" in line]
    if stamps and None not in stamps:
        phases = [round(stamps[0] - sent_at, 4), round(stamps[-1] - stamps[0], 4),
                  round(ended_at - stamps[-1], 4)]
    # the saves' lines: one notifications line, then one boards line, nothing after them but
    # an "again" line
    said = [line for line in lines if " notifications: " in line and "at the stop" in line
            or " scoreboards: " in line and "at the stop" in line]
    order = (len(said) == 2 and " notifications: " in said[0] and " scoreboards: " in said[1]
             and all(" again while " in line for line in lines[lines.index(said[1]) + 1:]))
    broke = "the stop breaks into it" in text
    bad = (lost or not saved or proc.returncode != 0 or "Traceback" in text
           or "Exception ignored" in text or "Task was destroyed" in text or not order
           or (held and broke != (held > 5)))
    return {"started": started, "lost": lost, "rc": proc.returncode, "took": round(took, 4),
            "saved": saved, "traceback": "Traceback" in text,
            "ignored": "Exception ignored" in text, "destroyed": "Task was destroyed" in text,
            "order": order, "held": round(held, 2), "broke": broke,
            "last": lines[-2:], "phases": phases,
            "again": sum(" again while " in line for line in lines),
            "tail": text[-4000:] if bad else ""}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("runs", type=int)
    parser.add_argument("--repo", default=ROOT, help="the server's folder (default: this one)")
    parser.add_argument("--python", default=sys.executable, help="the Python that runs it")
    parser.add_argument("--signals", default="SIGTERM", help="sent in this order (default SIGTERM)")
    parser.add_argument("--gap", type=float, default=0.0, help="seconds between the signals")
    parser.add_argument("--gc-threshold", type=int, default=0, metavar="N",
                        help="gc.set_threshold(N) in the server (default: Python's)")
    parser.add_argument("--slow-discord", type=float, default=0.0, metavar="S",
                        help="a Discord status update that takes S seconds, every 0.3 s")
    parser.add_argument("--held", type=float, default=0.0, metavar="SHARE",
                        help="in this share of the runs (0-1) the signal comes while the "
                             "server's loop is held by Python code: 2-4 s (it takes the "
                             "signal once free) or 8-40 s (past the stop's 5 s limit: broken "
                             "into)")
    parser.add_argument("--diag", action="store_true",
                        help="the stacks of a lost stop's threads in its log, before the kill")
    parser.add_argument("--out", help="a JSON line per run, added to this file")
    args = parser.parse_args()
    signals = args.signals.split(",")
    version = subprocess.run([args.python, "-c", "import platform; print(platform.python_version())"],
                             capture_output=True, text=True).stdout.strip()
    template = tempfile.mkdtemp()
    out = open(args.out, "a") if args.out else None
    results = []
    try:
        subprocess.run([args.python, "-c", SEED, args.repo, template], check=True,
                       capture_output=True)
        for i in range(args.runs):
            r = one(args, signals, template)
            results.append(r)
            if out:
                out.write(json.dumps(dict(r, python=version, signals=args.signals,
                                          gc=args.gc_threshold,
                                          slow_discord=args.slow_discord)) + "\n")
                out.flush()
            if r["tail"]:
                print("run %d: lost=%s rc=%s saved=%s took=%.2f s\n%s" % (
                    i, r["lost"], r["rc"], r["saved"], r["took"], r["tail"]), flush=True)
    finally:
        shutil.rmtree(template, ignore_errors=True)
        if out:
            out.close()
    took = sorted(r["took"] for r in results if not r["lost"])
    print("Python %s, %d runs, %s%s: lost %d, without the stop's saves %d, exit status not 0 %d, "
          "a traceback %d, 'Exception ignored' %d, not started %d" % (
              version, len(results), args.signals,
              (", gc threshold %d" % args.gc_threshold if args.gc_threshold else "")
              + (", Discord update %g s" % args.slow_discord if args.slow_discord else ""),
              sum(r["lost"] for r in results), sum(not r["saved"] for r in results),
              sum(r["rc"] != 0 for r in results), sum(r["traceback"] for r in results),
              sum(r["ignored"] for r in results), sum(not r["started"] for r in results)))
    print("'Task was destroyed' %d, the saves' lines out of order %d" % (
        sum(r["destroyed"] for r in results), sum(not r["order"] for r in results)))
    held = [r for r in results if r["held"]]
    if held:
        print("held loops: %d (%d under the limit, %d past it), broken into %d, wrong way %d, "
              "slowest of them %.2f s" % (
                  len(held), sum(r["held"] <= 5 for r in held), sum(r["held"] > 5 for r in held),
                  sum(r["broke"] for r in held),
                  sum(r["broke"] != (r["held"] > 5) for r in held),
                  max(r["took"] for r in held)))
    if len(signals) > 1:
        print("the log said a signal was ignored during the saves in %d runs (%d lines)" % (
            sum(r["again"] > 0 for r in results), sum(r["again"] for r in results)))
    phases = [r["phases"] for r in results if r["phases"]]
    if phases:
        print("of which (the log's millisecond clock), median / max: signal to the stop's first "
              "line %s, its saves %s, the end after them %s" % tuple(
                  "%.3f / %.3f s" % (statistics.median(p[k] for p in phases),
                                     max(p[k] for p in phases)) for k in range(3)))
    if took:
        print("stop time, first signal to exit: median %.3f s, 99th percentile %.3f s, "
              "max %.3f s" % (statistics.median(took),
                              took[min(len(took) - 1, int(len(took) * 0.99))], took[-1]))
    return 1 if any(r["lost"] or not r["saved"] or r["rc"] != 0 or not r["order"]
                    for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
