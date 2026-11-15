#!/usr/bin/env python3
"""Stop a busy server over and over, and count the stops that were lost (#78).

Usage: stop_loop.py RUNS [--repo DIR] [--python EXE] [--signals SIGTERM[,SIGINT...]] [--gap S]
                         [--gc-threshold N] [--diag] [--out FILE.jsonl]

Each run starts the server (forza_live_server.py of --repo, this repository by default) on
127.0.0.1 with free ports and a temporary data folder, waits for /status, and puts it under load:
HTTP connections opened and closed as fast as one thread can (a third answered, a third dropped
after half a request, a third dropped at once) and junk UDP datagrams, so tasks die and their weak
references are cleaned up all the time. --gc-threshold N runs the server with
gc.set_threshold(N), the garbage collector at work far more often. At a random moment the run
sends the signal(s), --gap seconds apart, and waits 20 s (the public server's stop_grace_period)
for the process to end.

A stop is LOST when the process still runs after those 20 s; it is killed then (with --diag it is
asked for the stacks of all its threads first: faulthandler, in its log). Each run also checks
the stop's saves: every data folder starts with a notification that only notifications.journal
has, so a stop that saves writes notifications.json whole (its line in the log) and
live_state.json, and logs the boards' line. Logs with a traceback or an "Exception ignored" are
counted too. The time of a stop is from the first signal to the process's end.

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
# the server run as forza_live_server.py runs it, with what --gc-threshold and --diag add
WRAPPER = ("import gc, faulthandler, runpy, signal, sys\n"
           "if {gc}: gc.set_threshold({gc})\n"
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


def one(args, signals, template):
    with tempfile.TemporaryDirectory() as top:
        data = os.path.join(top, "data")
        shutil.copytree(template, data)
        http, udp = free(socket.SOCK_STREAM), free(socket.SOCK_DGRAM)
        log_path = os.path.join(top, "server.log")
        wrapper = WRAPPER.format(gc=args.gc_threshold, diag=args.diag)
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
        sent = time.monotonic()
        for i, name in enumerate(signals):
            if i:
                time.sleep(args.gap)
            try:
                proc.send_signal(getattr(signal, name))
            except ProcessLookupError:
                pass
        lost = False
        try:
            proc.wait(STOP_TIMEOUT)
        except subprocess.TimeoutExpired:
            lost = True
            if args.diag:
                proc.send_signal(signal.SIGUSR1)
                time.sleep(1.0)
            proc.kill()
            proc.wait()
        took = time.monotonic() - sent
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
    bad = (lost or not saved or proc.returncode != 0 or "Traceback" in text
           or "Exception ignored" in text)
    return {"started": started, "lost": lost, "rc": proc.returncode, "took": round(took, 4),
            "saved": saved, "traceback": "Traceback" in text,
            "ignored": "Exception ignored" in text, "last": lines[-2:],
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
                                          gc=args.gc_threshold)) + "\n")
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
              ", gc threshold %d" % args.gc_threshold if args.gc_threshold else "",
              sum(r["lost"] for r in results), sum(not r["saved"] for r in results),
              sum(r["rc"] != 0 for r in results), sum(r["traceback"] for r in results),
              sum(r["ignored"] for r in results), sum(not r["started"] for r in results)))
    if took:
        print("stop time, first signal to exit: median %.3f s, 99th percentile %.3f s, "
              "max %.3f s" % (statistics.median(took),
                              took[min(len(took) - 1, int(len(took) * 0.99))], took[-1]))
    return 1 if any(r["lost"] or not r["saved"] or r["rc"] != 0 for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
