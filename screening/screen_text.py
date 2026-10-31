"""Word lists over the text found in a photo (OCR), its title and its credit (screening/README.md).

Text and terms are normalised the same way: NFKC, case folded, the swastika characters read as
the [ambiguous] word "manjisign" (a word with no listed term inside it) and the SS-rune
look-alikes as "s". A term written without accents is matched against the text with its accents
dropped (so "nïgger" is caught); a term with accents only against the text as written (French
"péter" is not the name Peter). A text yields tokens (runs of
letters and digits), and per token: itself, Cyrillic and Greek look-alike letters read as Latin
ones, leetspeak undone (n1gg3r, sh!t, a$$; digits only inside a word, so 88 stays 88), runs of
three or more of the same letter shortened to two and to one, and runs of single letters joined
(n i g g e r, f.u.c.k). Hits:

- a word term: a token equal to it, or to it with a plural s, es or z;
- a phrase: its tokens in a row;
- a term in a script written without spaces (Chinese, Japanese, Thai) of two characters or more:
  anywhere in the text;
- "inside": a [hide] or [hate] term of 5 letters or more inside a longer token. Such a hit only
  holds (never hides): "snigger" and its kind are in terms.txt's [allow].

LDNOOBW entries of the non-English lists that are ordinary English words (ldnoobw_skip.txt) are
left out: OCR reads sponsor logos and badges, and "con", "del" or "pinto" must not hold a photo.

Terms that are only digits count only from terms.txt (LDNOOBW's "13." is not a race number's
business), and one-character terms are skipped.
"""

import os
import re
import unicodedata

LEVELS = ("hide", "hate", "ambiguous", "hold")
# The swastika characters (a Buddhist and Hindu sign as often as anything else) become the
# [ambiguous] token "manjisign"; SS-rune look-alikes an "s".
SPECIAL = {"\u5350": " manjisign ", "\u534d": " manjisign ", "\u03df": "s",
           "\u16cb": "s", "\u16ca": "s"}
HOMOGLYPHS = str.maketrans({
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p",
    "с": "c", "т": "t", "у": "y", "х": "x", "і": "i", "ј": "j", "ѕ": "s", "ԁ": "d", "ɡ": "g",
    "α": "a", "β": "b", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x"})
LEET = str.maketrans({"0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "8": "b",
                      "9": "g", "@": "a", "$": "s", "!": "i", "|": "l", "€": "e", "+": "t"})
TOKEN = re.compile(r"[^\W_]+")
NO_SPACE_SCRIPT = re.compile("[฀-๿぀-ヿ㐀-䶿一-鿿豈-﫿]")
INSIDE_MIN = 5


def fold(text, strip_accents=True):
    text = unicodedata.normalize("NFKC", text or "").casefold()
    for k, v in SPECIAL.items():
        text = text.replace(k, v)
    if strip_accents:
        text = "".join(c for c in unicodedata.normalize("NFKD", text)
                       if not unicodedata.combining(c))
    return unicodedata.normalize("NFC", text)


def collapse(token):
    """A token's runs of three or more of one letter shortened to two and to one (niiiigger)."""
    return {re.sub(r"(.)\1\1+", r"\1\1", token), re.sub(r"(.)\1\1+", r"\1", token)} - {token}


def variants(folded):
    """(plain tokens, [(token, how)]) of a folded text: the tokens, the look-alike and leetspeak
    readings, the shortened letter runs, the joined single letters."""
    plain = TOKEN.findall(folded)
    out = [(t, "word") for t in plain]
    for chunk in folded.split():
        letters = chunk.translate(HOMOGLYPHS)
        if letters != chunk:
            out += [(t, "lookalike") for t in TOKEN.findall(letters)]
        if re.search(r"[^\W\d_]", letters) and re.search(r"[\d@$!|€+]", letters):
            leet = "".join(TOKEN.findall(letters.translate(LEET)))
            if leet:
                out.append((leet, "leet"))
    for t, how in list(out):
        out += [(c, how + "+repeats") for c in sorted(collapse(t))]
    run = []
    for t in plain + [""]:
        if len(t) == 1 and not t.isdigit():
            run.append(t)
            continue
        if len(run) >= 3:
            out.append(("".join(run), "spaced"))
        run = []
    return plain, out


class Terms:
    """The terms, each with its level: hide, hate, ambiguous (terms.txt) or hold (LDNOOBW).
    Kept twice over: [0] the terms without accents (matched on the text without its accents),
    [1] the terms with accents (matched on the text as written)."""

    def __init__(self, terms_file, ldnoobw_dir=None, ldnoobw_skip=None):
        self.words = ({}, {})
        self.phrases = ([], [])
        self.substrings = ([], [])
        self.allow = []
        self.sources = []
        self._load_terms(terms_file)
        skip = set()
        if ldnoobw_skip and os.path.exists(ldnoobw_skip):
            with open(ldnoobw_skip, encoding="utf-8") as f:
                skip = {fold(x.strip(), False) for x in f if x.strip() and not x.startswith("#")}
        if ldnoobw_dir and os.path.isdir(ldnoobw_dir):
            for lang in sorted(os.listdir(ldnoobw_dir)):
                if lang == "LICENSE" or lang.startswith("."):
                    continue
                with open(os.path.join(ldnoobw_dir, lang), encoding="utf-8") as f:
                    for line in f:
                        if lang == "en" or fold(line.strip(), False) not in skip:
                            self.add(line.strip(), "hold", "ldnoobw/" + lang, digits=False)
                self.sources.append("ldnoobw/" + lang)

    def _load_terms(self, path):
        level = None
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.fullmatch(r"\[(\w+)\]", line)
                if m:
                    level = m.group(1)
                    if level not in LEVELS + ("allow",):
                        raise ValueError("%s: unknown section [%s]" % (path, level))
                    continue
                if level == "allow":
                    self.allow.append(TOKEN.findall(fold(line)))
                elif level:
                    self.add(line, level, os.path.basename(path), digits=True)
        self.sources.append(os.path.basename(path))

    def add(self, term, level, source, digits):
        kept = fold(term, strip_accents=False)
        accents = int(kept != fold(term))
        toks = TOKEN.findall(kept)
        if not toks or (not digits and all(t.isdigit() for t in toks)):
            return
        entry = (term, level, source)
        if NO_SPACE_SCRIPT.search(kept):
            compact = "".join(toks)
            if len(compact) >= 2:
                self.substrings[accents].append((compact, entry))
        elif len(toks) == 1:
            if len(toks[0]) >= 2:
                self.words[accents].setdefault(toks[0], entry)
        else:
            self.phrases[accents].append((toks, entry))

    def check(self, text, source="ocr", confidence=1.0):
        """The hits in one text: [{"term", "level", "list", "match", "token", "source",
        "confidence"}]."""
        hits, seen = [], set()
        if not text:
            return hits

        def hit(entry, how, token):
            if entry[0] not in seen:
                seen.add(entry[0])
                hits.append({"term": entry[0], "level": entry[1], "list": entry[2], "match": how,
                             "token": token, "source": source, "confidence": round(confidence, 3)})

        for accents in (0, 1):
            folded = fold(text, strip_accents=not accents)
            for phrase in self.allow:
                pattern = r"(?<![^\W_])%s(?![^\W_])" % r"[\W_]+".join(map(re.escape, phrase))
                folded = re.sub(pattern, " ", folded)
            plain, toks = variants(folded)
            words = self.words[accents]
            for tok, how in toks:
                for cand in (tok, tok[:-1] if tok.endswith(("s", "z")) else None,
                             tok[:-2] if tok.endswith("es") else None):
                    if cand and cand in words and (not cand.isdigit() or cand == tok):
                        hit(words[cand], how, tok)
            for phrase, entry in self.phrases[accents]:
                n = len(phrase)
                if any(plain[i:i + n] == phrase for i in range(len(plain) - n + 1)):
                    hit(entry, "phrase", " ".join(phrase))
            compact = "".join(plain)
            for sub, entry in self.substrings[accents]:
                if sub in compact:
                    hit(entry, "script", sub)
            for tok, how in toks:  # terms.txt's long terms inside a longer word: hold only
                if len(tok) <= INSIDE_MIN or tok.isdigit():
                    continue
                for word, entry in words.items():
                    if (len(word) >= INSIDE_MIN and word != tok and word in tok
                            and entry[1] in ("hide", "hate")):
                        hit((entry[0], "hold", entry[2]), "inside:" + how, tok)
        return hits
