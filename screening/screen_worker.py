#!/usr/bin/env python3
"""Forza Live community photos and designs: the automatic screening worker (screening/README.md).

Polls the server's queue of held photos and its queue of pending designs (the design
storefront's paint jobs and vinyl groups, before the website shows them), scores each one (NSFW
classifier, SigLIP2 zero-shot over the frame and, for photos, tiles; OCR and word lists over the
picture's text, the title and a design's description) and releases it, hides it with a note
naming the rule, or leaves it held for the maintainer (a design is moved to held, with the note).
One JSON line per photo or design on stdout: id, model versions, scores, OCR text, the rule, the
decision, latency. No XUID (the photo queue's owner field is dropped; the design queue has none),
never the token.

  screen_worker.py [--config screening.toml] [--once] [--dry-run]
  screen_worker.py --score [--design] FILE...   score local pictures (--design: as design
                                               previews), print the decisions, no server

The token: env FORZA_ADMIN_TOKEN, or the file named by FORZA_ADMIN_TOKEN_FILE.
"""

import argparse
import json
import logging
import os
import re
import resource
import signal
import sys
import threading
import time
import tomllib

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)  # the modules beside this one, also under python -I
os.environ["ORT_DISABLE_TELEMETRY"] = "1"  # before onnxruntime loads (screen_models.py)

import screen_image  # noqa: E402
import screen_policy  # noqa: E402
import screen_text  # noqa: E402
log = logging.getLogger("screening")


def load_config(path):
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    cfg["_dir"] = os.path.dirname(os.path.abspath(path))
    return cfg


def rel(cfg, path):
    return path if os.path.isabs(path) else os.path.join(cfg["_dir"], path)


def emit(record):
    print(json.dumps(record, separators=(",", ":"), ensure_ascii=False, default=str), flush=True)


def short(v):
    return round(float(v), 4)


def prompt(text):
    return text.removeprefix("this is a photo of ").rstrip(".")


class Screener:
    """The models and word lists; score(img, photo) -> (scores for screen_policy, log fields)."""

    def __init__(self, cfg, models=None):
        import screen_models
        rt = cfg.get("runtime", {})
        self.cfg = cfg
        self.models_dir = rt.get("models_dir", "/models")
        threads = int(rt.get("threads", 4))
        self.grids = [tuple(g) for g in cfg.get("zeroshot", {}).get("grids", [[3, 2]])]
        text = cfg.get("text", {})
        self.min_conf = float(text.get("min_confidence", 0.5))
        self.check_gamertag = bool(text.get("check_gamertag", True))
        self.terms = screen_text.Terms(
            rel(cfg, text.get("terms", "wordlists/terms.txt")),
            text.get("ldnoobw", os.path.join(self.models_dir, "wordlists", "ldnoobw")),
            rel(cfg, text.get("ldnoobw_skip", "wordlists/ldnoobw_skip.txt")))
        m = models or {}
        self.nsfw = m.get("nsfw") or screen_models.Nsfw(self.models_dir, threads)
        self.siglip = m.get("siglip") or screen_models.Siglip(self.models_dir, threads)
        self.ocr = m.get("ocr") or screen_models.Ocr(threads, self.min_conf)
        probe = cfg.get("probe", {}).get("path", "")
        self.probe = screen_models.Probe(rel(cfg, probe)) if probe else None
        self.versions = self._versions()
        self.hidden = {}          # photo id -> pHash of the hidden photos (pHash rule)
        self.hidden_designs = {}  # design id -> pHash of the hidden designs
        state = cfg.get("state", {})
        self.embeddings_dir = None
        if state.get("save_embeddings") and state.get("dir"):
            self.embeddings_dir = os.path.join(state["dir"], "embeddings")
            os.makedirs(self.embeddings_dir, exist_ok=True)

    def _versions(self):
        v = {"ocr": getattr(self.ocr, "version", "?"), "terms": self.terms.sources[:1]
             + ["ldnoobw (%d lists)" % (len(self.terms.sources) - 1)]}
        try:
            with open(os.path.join(self.models_dir, "manifest.json")) as f:
                man = json.load(f)
            for src in man.get("sources", []):
                key = src["name"].split("/")[0]
                if key in ("nsfw", "siglip2") and key not in v:
                    v[key] = "%s@%s" % (src["source"].split(" ")[0], src["revision"][:10])
            v["prompts"] = man.get("prompts_sha256", "")[:12]
        except (OSError, ValueError, KeyError):
            v["manifest"] = "missing"
        if self.probe:
            v["probe"] = os.path.basename(self.probe.path)
        return v

    def score(self, img, photo=None, grids=None, hidden=None):
        """grids: the tiles for the zero-shot model (default: [zeroshot] grids; [] the whole
        frame only); hidden: the pHashes to compare with (default: the hidden photos')."""
        photo = photo or {}
        grids = self.grids if grids is None else grids
        hidden = self.hidden if hidden is None else hidden
        s = {"errors": {}, "hits": []}
        fields, ms = {}, {}

        def stage(name, fn):
            t = time.perf_counter()
            try:
                return fn()
            except Exception as e:  # one stage failing leaves the photo held, never released
                s["errors"][name] = "%s: %s" % (type(e).__name__, str(e)[:120])
                log.warning("%s: %s failed: %s", photo.get("id", "-"), name, s["errors"][name])
                return None
            finally:
                ms[name] = round((time.perf_counter() - t) * 1000)

        s["nsfw"] = stage("nsfw", lambda: self.nsfw(img))
        if s["nsfw"]:
            fields["nsfw"] = {k: short(v) for k, v in s["nsfw"].items()}
        emb = stage("siglip", lambda: self.siglip.embed(img, grids))
        if emb:
            names, vectors = emb
            s["zeroshot"] = self.siglip.score(names, vectors)
            fields["zeroshot"] = {c: {"p": short(v["sigmoid"]), "at": v["region"],
                                      "prompt": prompt(v["prompt"])}
                                  for c, v in s["zeroshot"].items()}
            if self.probe:
                s["probe"] = stage("probe", lambda: self.probe(names, vectors)) or {}
                fields["probe"] = {k: short(v["score"]) for k, v in s["probe"].items()}
            if self.embeddings_dir and photo.get("id"):
                stage("save", lambda: self._save(photo, names, vectors))
        lines = stage("ocr", lambda: self.ocr(img)) or []
        fields["ocr"] = [[t, short(c)] for t, c in lines]
        for t, c in lines:
            if c >= self.min_conf:
                s["hits"] += self.terms.check(t, "ocr", c)
        for field in ("title", "description"):
            if photo.get(field):
                s["hits"] += self.terms.check(photo[field], field)
        if self.check_gamertag and photo.get("gamertag"):
            s["hits"] += self.terms.check(photo["gamertag"], "gamertag")
        fields["hits"] = [{k: h[k] for k in ("term", "level", "match", "source", "confidence")}
                          for h in s["hits"]]
        ph = stage("phash", lambda: screen_image.phash(img))
        if ph:
            fields["phash"] = ph
            near = [(screen_image.distance(ph, h), pid) for pid, h in hidden.items()
                    if pid != photo.get("id")]
            if near:
                d, pid = min(near)
                s["phash"] = {"id": pid, "distance": d}
        fields["ms"] = ms
        return s, fields

    def _save(self, photo, names, vectors):
        import numpy as np
        name = ("design-" if photo.get("kind") == "design" else "") + photo["id"]
        np.savez_compressed(os.path.join(self.embeddings_dir, name + ".npz"),
                            regions=np.array(names), embeddings=vectors,
                            sha256=np.array(photo.get("sha256", "")))

    def selftest(self):
        """The models on drawn pictures: a red star is found, a word is read, a plain scene is
        clear. RuntimeError when not."""
        import synth
        star = synth.selftest_star()
        names, emb = self.siglip.embed(star, self.grids)
        p = self.siglip.score(names, emb, ["selftest"])["selftest"]["sigmoid"]
        word = "".join(t for t, _ in self.ocr(synth.text(synth.scene(), "SELFTEST 2468")))
        scene = synth.scene()
        n = self.nsfw(scene)
        scores, _ = self.score(scene)
        verdict = screen_policy.decide(scores, self.cfg)["decision"]
        ok = p >= 0.5 and "SELFTEST" in word.upper() and n["high"] < 0.5 and verdict == "release"
        result = {"event": "selftest", "ok": ok, "star": short(p), "ocr": word,
                  "scene_nsfw_high": short(n["high"]), "scene": verdict}
        emit(result)
        if not ok:
            raise RuntimeError("selftest failed: %s" % result)


def note_for(verdict):
    """The note the server keeps with a released or hidden photo (ASCII, at most 200 chars)."""
    text = "screening: %s (%s%s)" % (verdict["decision"], verdict["rule"],
                                     ": " + verdict["detail"] if verdict["detail"] else "")
    return re.sub(r"[^\x20-\x7e]", "?", text)[:200]


class Worker:
    def __init__(self, cfg, client, screener, dry_run=False):
        srv = cfg.get("server", {})
        self.cfg, self.client, self.screener, self.dry_run = cfg, client, screener, dry_run
        self.poll = float(srv.get("poll_seconds", 30))
        self.max_per_poll = int(srv.get("max_per_poll", 20))
        self.attempts = int(srv.get("fetch_attempts", 5))
        self.hidden_refresh = float(srv.get("hidden_refresh_seconds", 3600))
        d = cfg.get("designs", {})
        self.designs_on = bool(d.get("enabled", True))
        self.design_grids = [tuple(g) for g in d.get("grids", [])]
        self.left = {}      # (kind, id, sha256) -> the decision that left it held: not screened again
        self.failures = {}  # (kind, id) -> image fetches that failed
        self.stop = threading.Event()
        self._hidden_at = 0.0
        self._no_designs_until = 0.0  # a server without designs on the website: asked again then

    def _refresh(self, listing, known, what):
        try:
            hidden = listing()
        except Exception as e:
            log.warning("hidden %ss: %s", what, e)
            return
        for p in hidden:
            if p["id"] in known:
                continue
            try:
                data = self.client.image(p)
                img = (screen_image.decode(data, screen_image.DESIGN_MIN, screen_image.DESIGN_MAX)
                       if what == "design" else screen_image.decode(data))
                known[p["id"]] = screen_image.phash(img)
            except Exception as e:
                log.warning("hidden %s %s: %s", what, p["id"], e)
        for pid in set(known) - {p["id"] for p in hidden}:
            del known[pid]

    def refresh_hidden(self):
        """The pHashes of the hidden photos and designs (for the like-hidden rule), read again
        hourly."""
        if time.monotonic() - self._hidden_at < self.hidden_refresh and self._hidden_at:
            return
        self._hidden_at = time.monotonic()
        self._refresh(lambda: self.client.photos("hidden"), self.screener.hidden, "photo")
        if self.designs_on and time.monotonic() >= self._no_designs_until:
            self._refresh(lambda: self.client.designs("hidden"), self.screener.hidden_designs,
                          "design")

    def design_queue(self):
        """(pending designs, how many are pending in all); none when off or when the server has
        no designs on the website (an older forza-live: asked again an hour later). A failure
        here never stops the photos."""
        if not self.designs_on or time.monotonic() < self._no_designs_until:
            return [], 0
        try:
            return self.client.design_queue()
        except Exception as e:
            if getattr(e, "status", None) == 404:
                log.warning("the server has no designs queue (a forza-live before the designs "
                            "feed): designs skipped for an hour")
                self._no_designs_until = time.monotonic() + 3600
            else:
                log.warning("designs' queue: %s", e)
            return [], 0

    @staticmethod
    def key(p):
        return (p.get("kind", "photo"), p["id"], p["sha256"])

    def run_once(self):
        """One poll: (photos and designs screened, photos and designs waiting)."""
        self.refresh_hidden()
        queue = self.client.queue()
        designs, pending = self.design_queue()
        items = queue + designs
        waiting = {self.key(p) for p in items}
        for key in set(self.left) - waiting:  # decided by the operator since
            del self.left[key]
        todo = [p for p in items if self.key(p) not in self.left
                and self.failures.get(self.key(p)[:2], 0) < self.attempts]
        done = 0
        for p in todo[:self.max_per_poll]:
            if self.stop.is_set():
                break
            self.screen(p)
            done += 1
        return done, len(todo) - done + max(0, pending - len(designs))

    def screen(self, p):
        """Scores one photo or design (p["kind"]) and acts on the decision."""
        t0 = time.perf_counter()
        design = p.get("kind") == "design"
        rec = {"event": "design" if design else "photo", "id": p["id"]}
        if design:
            rec["type"] = p.get("type", "?")
        rec.update(sha256=p["sha256"][:16], models=self.screener.versions)
        fkey = self.key(p)[:2]
        try:
            data = self.client.image(p)
        except Exception as e:
            n = self.failures[fkey] = self.failures.get(fkey, 0) + 1
            rec.update(decision="retry" if n < self.attempts else "hold",
                       rule="fetch-failed", detail=str(e)[:200])
            if getattr(e, "status", None) == 404:
                rec.update(decision="gone", rule="gone")
                self.failures[fkey] = self.attempts
            elif design and n >= self.attempts and not self.dry_run:
                self._act(p, "hold", "screening: hold (fetch-failed: %s)" % str(e)[:120], rec)
            emit(rec)
            return
        self.failures.pop(fkey, None)
        try:
            if design:  # a small preview: the whole frame only, compared with hidden designs
                img = screen_image.decode(data, screen_image.DESIGN_MIN, screen_image.DESIGN_MAX)
                rec["size"] = "%dx%d" % img.size
                scores, fields = self.screener.score(img, p, grids=self.design_grids,
                                                     hidden=self.screener.hidden_designs)
            else:
                img = screen_image.decode(data)
                rec["size"] = "%dx%d" % img.size
                scores, fields = self.screener.score(img, p)
        except screen_image.BadPicture as e:
            scores, fields = {"errors": {"decode": str(e)}}, {}
        except Exception as e:  # never crash on a photo: it stays held
            log.exception("%s %s", rec["event"], p["id"])
            scores, fields = {"errors": {"screen": "%s: %s" % (type(e).__name__, e)}}, {}
        verdict = screen_policy.decide(scores, self.cfg)
        rec.update(fields)
        rec.update(decision=verdict["decision"], rule=verdict["rule"], detail=verdict["detail"],
                   rules=[r[1] for r in verdict["rules"]])
        rec["ms"] = dict(rec.get("ms", {}), total=round((time.perf_counter() - t0) * 1000))
        # a held photo is left as it is; a pending design is moved to held, with the note
        acts = ("release", "hide", "hold") if design else ("release", "hide")
        action = verdict["decision"] if verdict["decision"] in acts else None
        if action and not self.dry_run:
            self._act(p, action, note_for(verdict), rec)
        elif action:
            rec["done"] = "dry-run"
        if verdict["decision"] == "hold" and rec.get("done") in (None, "dry-run"):
            self.left[self.key(p)] = verdict["rule"]
        if verdict["decision"] == "hide" and rec.get("done") == "hide" and fields.get("phash"):
            (self.screener.hidden_designs if design else self.screener.hidden)[p["id"]] = fields["phash"]
        rec["rss_mb"] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        emit(rec)

    def _act(self, p, action, note, rec):
        try:
            if p.get("kind") == "design":
                self.client.design_act(p["id"], action, note)
            else:
                self.client.act(p["id"], action, note)
            rec["done"] = action
        except Exception as e:
            status = getattr(e, "status", None)
            rec["done"] = ("operator-decided-first" if status == 409 else
                           "gone" if status == 404 else "failed: %s" % str(e)[:160])
            if status not in (404, 409):
                rec["decision"] = "retry"

    def run(self, once=False):
        delay = self.poll
        while not self.stop.is_set():
            try:
                done, waiting = self.run_once()
                delay = 0.5 if waiting and not once else self.poll
            except Exception as e:  # the server down, a bad answer: wait and try again
                log.warning("poll failed: %s", e)
                delay = min(max(delay * 2, self.poll), 300)
                if once:
                    raise
            if once:
                return
            self.stop.wait(delay)


def read_token():
    path = os.environ.get("FORZA_ADMIN_TOKEN_FILE")
    if path:
        with open(path) as f:
            return f.read().strip()
    return os.environ.get("FORZA_ADMIN_TOKEN", "").strip()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--config", default=os.path.join(HERE, "screening.toml"))
    ap.add_argument("--once", action="store_true", help="screen the queue once and exit")
    ap.add_argument("--dry-run", action="store_true", help="score and log, change nothing")
    ap.add_argument("--no-selftest", action="store_true")
    ap.add_argument("--score", nargs="+", metavar="FILE", help="score local pictures only")
    ap.add_argument("--design", action="store_true",
                    help="with --score: the pictures are design previews (whole frame only)")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, stream=sys.stderr,
                        format="%(asctime)s %(levelname)s %(message)s")
    cfg = load_config(a.config)
    t = time.perf_counter()
    screener = Screener(cfg)
    emit({"event": "start", "models": screener.versions,
          "load_ms": round((time.perf_counter() - t) * 1000),
          "threads": cfg.get("runtime", {}).get("threads", 4)})
    if cfg.get("runtime", {}).get("selftest", True) and not a.no_selftest:
        screener.selftest()
    if a.score:
        for path in a.score:
            t0 = time.perf_counter()
            try:
                with open(path, "rb") as f:
                    data = f.read()
                if a.design:
                    img = screen_image.decode(data, screen_image.DESIGN_MIN, screen_image.DESIGN_MAX)
                    scores, fields = screener.score(
                        img, {"id": os.path.basename(path)},
                        grids=[tuple(g) for g in cfg.get("designs", {}).get("grids", [])],
                        hidden={})
                else:
                    img = screen_image.decode(data)
                    scores, fields = screener.score(img, {"id": os.path.basename(path)})
            except (OSError, screen_image.BadPicture) as e:
                scores, fields = {"errors": {"decode": str(e)}}, {}
            v = screen_policy.decide(scores, cfg)
            emit(dict(fields, event="score", file=os.path.basename(path), decision=v["decision"],
                      rule=v["rule"], detail=v["detail"], ms=dict(fields.get("ms", {}), total=round(
                          (time.perf_counter() - t0) * 1000)),
                      rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024))
        return 0
    from screen_client import AdminClient
    srv = cfg.get("server", {})
    token = read_token()
    if not token:
        log.warning("no FORZA_ADMIN_TOKEN: only a server on this machine's loopback admits that")
    base = os.environ.get("FORZA_SERVER") or srv.get("url", "http://172.17.0.1:21201")
    client = AdminClient(base, token, timeout=float(srv.get("timeout_seconds", 30)),
                         max_image_bytes=int(srv.get("max_image_bytes", 16 << 20)))
    del token
    worker = Worker(cfg, client, screener, dry_run=a.dry_run)
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: worker.stop.set())
    emit({"event": "polling", "server": client.base, "dry_run": a.dry_run})
    worker.run(once=a.once)
    emit({"event": "stopped"})
    return 0


if __name__ == "__main__":
    sys.exit(main())
