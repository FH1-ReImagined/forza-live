"""The screening decision (screening/README.md, "Decision table"): release, hide or leave held, from
the scores of one photo and the thresholds of screening.toml. A pure function: no model, no I/O.

A photo is hidden only on a confident hit; anything doubtful is left held for the maintainer;
a photo some stage could not score is never released. Rule names go into the log and, for a
hidden photo, into the note the server keeps.
"""

RELEASE, HIDE, HOLD = "release", "hide", "hold"

DEFAULTS = {
    "nsfw": {"hold_medium_high": 0.30, "hide_high": 0.90},
    "zeroshot": {"hold": {"hate": 0.30, "gore": 0.30, "nudity": 0.30},
                 "hide_hate_with_text": 0.90, "ambiguous_with_hate": 0.20},
    "probe": {"hold": {}, "hide": {}},
    "text": {"hide_min_confidence": 0.80},
    "phash": {"hold_distance": 6},
}


def merged(cfg):
    """The thresholds: DEFAULTS with the config's values over them (one level of tables)."""
    out = {}
    for k, v in DEFAULTS.items():
        out[k] = dict(v)
        for kk, vv in (cfg or {}).get(k, {}).items():
            out[k][kk] = dict(out[k].get(kk, {}), **vv) if isinstance(vv, dict) else vv
    return out


def decide(s, cfg=None):
    """s: {"nsfw": {neutral, low, medium, high} | None, "zeroshot": {category: {"sigmoid", ...}}
    | None, "probe": {label: {"score"}}, "hits": [screen_text hits, with "source" and
    "confidence"], "phash": {"id", "distance"} | None, "errors": {stage: message}}.
    Returns {"decision", "rule", "rules": [(decision, rule, detail)]}: the first hide rule, else the
    first hold rule, else release."""
    t = merged(cfg)
    fired = []

    def rule(decision, name, detail):
        fired.append((decision, name, detail))

    nsfw = s.get("nsfw")
    if nsfw:
        high, mh = nsfw.get("high", 0.0), nsfw.get("medium", 0.0) + nsfw.get("high", 0.0)
        if high >= t["nsfw"]["hide_high"]:
            rule(HIDE, "nsfw-high", "P(high) %.2f" % high)
        elif mh >= t["nsfw"]["hold_medium_high"]:
            rule(HOLD, "nsfw-suspect", "P(medium+high) %.2f" % mh)

    hits = s.get("hits") or []
    min_conf = t["text"]["hide_min_confidence"]
    for h in hits:
        where = h.get("source", "ocr")
        sure = where == "title" or (where == "ocr" and h.get("confidence", 0.0) >= min_conf)
        what = "'%s' in the %s" % (h["term"], "text of the photo" if where == "ocr" else where)
        if h["level"] == "hide" and where != "gamertag":
            rule(HIDE if sure else HOLD, "text-hide" if sure else "text-unsure", what)
        elif h["level"] in ("hide", "hate", "hold"):
            rule(HOLD, "gamertag-word" if where == "gamertag" else
                 "text-hate" if h["level"] == "hate" else "text-word", what)

    zs = s.get("zeroshot") or {}
    hate = (zs.get("hate") or {}).get("sigmoid", 0.0)
    in_photo = [h for h in hits if h.get("source") != "gamertag"]
    hate_words = [h for h in in_photo if h["level"] in ("hate", "hide")]
    codes = [h for h in in_photo if h["level"] == "ambiguous"]
    if hate >= t["zeroshot"]["hide_hate_with_text"] and hate_words:
        rule(HIDE, "hate-symbol+text", "hate %.2f (%s) and '%s'" % (
            hate, zs["hate"].get("prompt", ""), hate_words[0]["term"]))
    for cat, threshold in sorted(t["zeroshot"]["hold"].items()):
        v = (zs.get(cat) or {}).get("sigmoid", 0.0)
        if v >= threshold:
            rule(HOLD, cat + "-suspect", "%s %.2f at %s (%s)" % (
                cat, v, zs[cat].get("region", "?"), zs[cat].get("prompt", "")))
    if codes and hate >= t["zeroshot"]["ambiguous_with_hate"]:
        rule(HOLD, "code+symbol", "'%s' with hate %.2f" % (codes[0]["term"], hate))

    probe = s.get("probe") or {}
    for label, v in sorted(probe.items()):
        score = v.get("score", 0.0)
        if label in t["probe"]["hide"] and score >= t["probe"]["hide"][label]:
            rule(HIDE, "probe-" + label, "%.2f" % score)
        elif label in t["probe"]["hold"] and score >= t["probe"]["hold"][label]:
            rule(HOLD, "probe-" + label, "%.2f" % score)

    ph = s.get("phash")
    if ph and ph.get("distance", 99) <= t["phash"]["hold_distance"]:
        rule(HOLD, "like-hidden", "pHash %d bits from hidden photo %s" % (
            ph["distance"], ph.get("id")))

    for stage, err in sorted((s.get("errors") or {}).items()):
        rule(HOLD, "error-" + stage, str(err)[:120])

    for decision in (HIDE, HOLD):
        first = next((r for r in fired if r[0] == decision), None)
        if first:
            return {"decision": decision, "rule": first[1], "detail": first[2], "rules": fired}
    return {"decision": RELEASE, "rule": "clear", "detail": "", "rules": fired}
