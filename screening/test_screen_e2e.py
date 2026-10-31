"""One end-to-end run: the real models (built by build_models.py; SCREENING_MODELS, default
/models) screening drawn pictures held on a local forza-live server, through the worker's own
command line (screen_worker.main --once). Prints the CPU time per picture and the peak memory.
Skipped when the models are not there."""

import io
import json
import os
import re
import resource
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

import synth
from test_screen_client import TOKEN, LocalServer

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.environ.get("SCREENING_MODELS", "/models")


def have_models():
    import importlib.util
    if not all(importlib.util.find_spec(m) for m in ("onnxruntime", "rapidocr")):
        return False
    return os.path.exists(os.path.join(MODELS, "manifest.json"))


def config(tmp):
    """screening.toml with this test's paths, plus a test word in [hide]."""
    terms = os.path.join(tmp, "terms.txt")
    with open(os.path.join(HERE, "wordlists", "terms.txt"), encoding="utf-8") as f:
        text = f.read()
    with open(terms, "w", encoding="utf-8") as f:
        f.write(text + "\n[hide]\nzqxtestword\n")
    with open(os.path.join(HERE, "screening.toml"), encoding="utf-8") as f:
        cfg = f.read()
    for key, value in (("models_dir", MODELS), ("terms", terms),
                       ("ldnoobw", os.path.join(MODELS, "wordlists", "ldnoobw")),
                       ("ldnoobw_skip", os.path.join(HERE, "wordlists", "ldnoobw_skip.txt")),
                       ("threads", int(os.environ.get("SCREENING_THREADS", "4")))):
        cfg = re.sub(r"(?m)^%s = .*$" % key, "%s = %s" % (key, json.dumps(value)), cfg)
    path = os.path.join(tmp, "screening.toml")
    with open(path, "w", encoding="utf-8") as f:
        f.write(cfg)
    return path


PICTURES = {  # id: (what, picture, the decision expected)
    "AAAAAAAAAAAA": ("scene", lambda: synth.scene(), "release"),
    "BBBBBBBBBBBB": ("car 88", lambda: synth.car(synth.scene(), "88"), "release"),
    "CCCCCCCCCCCC": ("car 14 sponsors", lambda: synth.text(synth.car(synth.scene(), "14"),
                                                           "MOTUL  CASTROL  BREMBO", size=80),
                     "release"),
    "DDDDDDDDDDDD": ("test word", lambda: synth.text(synth.scene(), "ZQXTESTWORD"), "hide"),
    "EEEEEEEEEEEE": ("red star", lambda: synth.selftest_star(), "release"),
}


@unittest.skipUnless(have_models(), "models not built (set SCREENING_MODELS)")
class EndToEnd(unittest.TestCase):
    def test_real_models(self):
        server = LocalServer({pid: (synth.png(make()), "")
                              for pid, (_, make, _) in PICTURES.items()})
        tmp = tempfile.TemporaryDirectory()
        try:
            import screen_worker
            out = io.StringIO()
            env = {"FORZA_SERVER": server.base, "FORZA_ADMIN_TOKEN": TOKEN}
            t0 = time.perf_counter()
            with mock.patch.dict(os.environ, env), redirect_stdout(out):
                self.assertEqual(screen_worker.main(["--config", config(tmp.name), "--once"]), 0)
            wall = time.perf_counter() - t0
            lines = [json.loads(x) for x in out.getvalue().splitlines() if x.startswith("{")]
            statuses = server.statuses()
        finally:
            server.close()
            tmp.cleanup()
        events = {r["event"] for r in lines}
        self.assertTrue({"start", "selftest", "polling", "photo", "stopped"} <= events, events)
        self.assertTrue(next(r for r in lines if r["event"] == "selftest")["ok"])
        photos = {r["id"]: r for r in lines if r["event"] == "photo"}
        want = {"release": "live", "hide": "hidden"}
        rows = []
        for pid, (what, _, decision) in PICTURES.items():
            r = photos[pid]
            rows.append("%-16s %-7s %-10s %5d ms (nsfw %d, siglip %d, ocr %d) hate %.3f  ocr %s" % (
                what, r["decision"], r["rule"], r["ms"]["total"], r["ms"]["nsfw"],
                r["ms"]["siglip"], r["ms"]["ocr"], r["zeroshot"]["hate"]["p"],
                [t for t, _ in r["ocr"]][:3]))
            self.assertEqual(r["decision"], decision, r)
            self.assertEqual(statuses[pid][0], want[decision])
            for key in ("models", "nsfw", "zeroshot", "ocr", "hits", "rule", "decision", "ms"):
                self.assertIn(key, r)
        self.assertEqual(photos["DDDDDDDDDDDD"]["rule"], "text-hide")
        self.assertIn("zqxtestword", statuses["DDDDDDDDDDDD"][1])
        self.assertIn("88", {h["term"] for h in photos["BBBBBBBBBBBB"]["hits"]})
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024
        start = next(r for r in lines if r["event"] == "start")
        totals = sorted(r["ms"]["total"] for r in photos.values())
        print("\n  end to end, %s threads: models loaded in %d ms, %d pictures in %.1f s (with the "
              "self-test), per picture median %d ms, max %d ms; peak RSS %d MB\n  %s" % (
                  start["threads"], start["load_ms"], len(photos), wall,
                  totals[len(totals) // 2], totals[-1], peak, "\n  ".join(rows)))


if __name__ == "__main__":
    unittest.main()
