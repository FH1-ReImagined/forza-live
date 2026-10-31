"""The decision table (screen_policy.decide) with made-up scores: no model, no server."""

import os
import tomllib
import unittest

import screen_policy as policy

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, "screening.toml"), "rb") as f:
    CFG = tomllib.load(f)

CLEAN_NSFW = {"neutral": 0.99, "low": 0.008, "medium": 0.001, "high": 0.001}
CLEAN_ZS = {"hate": {"sigmoid": 0.01, "region": "full", "prompt": "a swastika"},
            "gore": {"sigmoid": 0.001, "region": "full", "prompt": "gore"},
            "nudity": {"sigmoid": 0.002, "region": "full", "prompt": "nudity"}}


def scores(nsfw=None, hate=None, gore=None, nudity=None, hits=(), **extra):
    zs = {k: dict(v) for k, v in CLEAN_ZS.items()}
    for cat, v in (("hate", hate), ("gore", gore), ("nudity", nudity)):
        if v is not None:
            zs[cat].update(sigmoid=v, region="tile3x2:1,1")
    s = {"nsfw": dict(CLEAN_NSFW, **(nsfw or {})), "zeroshot": zs, "hits": list(hits),
         "errors": {}}
    s.update(extra)
    return s


def hit(term, level, source="ocr", confidence=0.99):
    return {"term": term, "level": level, "source": source, "confidence": confidence}


def decide(s):
    return policy.decide(s, CFG)


class DecisionTable(unittest.TestCase):
    def check(self, s, decision, rule):
        v = decide(s)
        self.assertEqual((v["decision"], v["rule"]), (decision, rule), v)
        return v

    def test_clean_photo_is_released(self):
        v = self.check(scores(), "release", "clear")
        self.assertEqual(v["rules"], [])

    def test_nsfw(self):
        self.check(scores({"medium": 0.20, "high": 0.05}), "release", "clear")
        self.check(scores({"medium": 0.25, "high": 0.06}), "hold", "nsfw-suspect")
        self.check(scores({"medium": 0.0, "high": 0.89}), "hold", "nsfw-suspect")
        self.check(scores({"high": 0.90}), "hide", "nsfw-high")
        self.check(scores({"high": 0.999}), "hide", "nsfw-high")

    def test_zero_shot_only_holds(self):
        for cat in ("hate", "gore", "nudity"):
            self.check(scores(**{cat: 0.29}), "release", "clear")
            self.check(scores(**{cat: 0.30}), "hold", cat + "-suspect")
            self.check(scores(**{cat: 0.999}), "hold", cat + "-suspect")

    def test_hate_symbol_with_hate_text_is_hidden(self):
        self.check(scores(hate=0.95, hits=[hit("nazi", "hate")]), "hide", "hate-symbol+text")
        self.check(scores(hate=0.85, hits=[hit("nazi", "hate")]), "hold", "text-hate")
        self.check(scores(hate=0.01, hits=[hit("nazi", "hate")]), "hold", "text-hate")

    def test_race_numbers_and_badges_are_not_hidden(self):
        # 14, 18, 88, SS alone: nothing; with a weak symbol score: held; never hidden
        for code in ("14", "18", "88", "ss"):
            self.check(scores(hits=[hit(code, "ambiguous")]), "release", "clear")
            self.check(scores(hate=0.19, hits=[hit(code, "ambiguous")]), "release", "clear")
            self.check(scores(hate=0.20, hits=[hit(code, "ambiguous")]), "hold", "code+symbol")
            self.check(scores(hate=0.99, hits=[hit(code, "ambiguous")]), "hold", "hate-suspect")

    def test_words(self):
        self.check(scores(hits=[hit("testslur", "hide")]), "hide", "text-hide")
        self.check(scores(hits=[hit("testslur", "hide", confidence=0.79)]), "hold", "text-unsure")
        self.check(scores(hits=[hit("testslur", "hide", source="title", confidence=1.0)]),
                   "hide", "text-hide")
        self.check(scores(hits=[hit("testword", "hold")]), "hold", "text-word")
        self.check(scores(hits=[hit("testslur", "hide", source="gamertag")]), "hold",
                   "gamertag-word")
        self.check(scores(hits=[hit("testword", "hold", source="gamertag")]), "hold",
                   "gamertag-word")

    def test_hide_wins_over_hold(self):
        v = self.check(scores({"medium": 0.5}, gore=0.5, hits=[hit("testslur", "hide")]),
                       "hide", "text-hide")
        self.assertEqual({r[1] for r in v["rules"]}, {"nsfw-suspect", "gore-suspect", "text-hide"})

    def test_errors_never_release(self):
        s = scores()
        s["errors"] = {"ocr": "RuntimeError: boom"}
        self.check(s, "hold", "error-ocr")
        s = {"errors": {"decode": "size 10x10"}}
        self.check(s, "hold", "error-decode")
        s = scores({"high": 0.95})
        s["errors"] = {"siglip": "x"}
        self.check(s, "hide", "nsfw-high")  # a confident hit still counts

    def test_missing_stages_are_not_clear(self):
        s = scores()
        s["nsfw"], s["errors"] = None, {"nsfw": "failed"}
        self.check(s, "hold", "error-nsfw")

    def test_like_a_hidden_photo(self):
        self.check(scores(phash={"id": "AAAAAAAAAAAA", "distance": 6}), "hold", "like-hidden")
        self.check(scores(phash={"id": "AAAAAAAAAAAA", "distance": 7}), "release", "clear")

    def test_probe_hook(self):
        cfg = dict(CFG, probe={"path": "x", "hold": {"hate": 0.5}, "hide": {"hate": 0.97}})
        s = scores(probe={"hate": {"score": 0.4}})
        self.assertEqual(policy.decide(s, cfg)["decision"], "release")
        s = scores(probe={"hate": {"score": 0.6}})
        self.assertEqual(policy.decide(s, cfg)["rule"], "probe-hate")
        s = scores(probe={"hate": {"score": 0.98}})
        self.assertEqual(policy.decide(s, cfg)["decision"], "hide")
        s = scores(probe={"other": {"score": 0.99}})  # no threshold: ignored
        self.assertEqual(policy.decide(s, cfg)["decision"], "release")

    def test_defaults_without_config(self):
        self.assertEqual(policy.decide(scores({"high": 0.95}))["decision"], "hide")
        self.assertEqual(policy.decide(scores(hate=0.31))["decision"], "hold")
        self.assertEqual(policy.decide(scores())["decision"], "release")

    def test_config_matches_defaults(self):
        # screening.toml and screen_policy.DEFAULTS say the same (the README's table is one)
        merged = policy.merged(CFG)
        for table, values in policy.DEFAULTS.items():
            for key, value in values.items():
                if key not in ("hold", "hide") or table != "probe":
                    self.assertEqual(merged[table][key], value, (table, key))


if __name__ == "__main__":
    unittest.main()
