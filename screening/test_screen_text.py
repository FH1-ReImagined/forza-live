"""The word lists (screen_text) with made-up test words; the shipped lists on benign racing text."""

import os
import tempfile
import unittest

import screen_text as st

HERE = os.path.dirname(os.path.abspath(__file__))
MODELS = os.environ.get("SCREENING_MODELS", "/models")
LDNOOBW = os.path.join(MODELS, "wordlists", "ldnoobw")

FIXTURE = """# test terms
[hide]
zqxslur
zqx slogan
[hate]
zqxhate
[ambiguous]
88
ss
[hold]
zqxpéter
测试词
x
[allow]
zqxslur free zone
"""


def terms_file(text=FIXTURE):
    f = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return f.name


class Matching(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.path = terms_file()
        cls.t = st.Terms(cls.path)

    @classmethod
    def tearDownClass(cls):
        os.remove(cls.path)

    def found(self, text):
        return {(h["term"], h["level"]) for h in self.t.check(text)}

    def test_evasions_of_a_word(self):
        for text in ("zqxslur", "ZQXSLUR", "Zqxslurs", "zqxslurz", "zqx5lur", "z q x s l u r",
                     "z.q.x.s.l.u.r", "zqxsluuuuur", "zqxslür", "zqхslur",  # Cyrillic х
                     "the ZQXSLUR car"):
            with self.subTest(text):
                self.assertEqual(self.found(text), {("zqxslur", "hide")})

    def test_phrases(self):
        self.assertEqual(self.found("ZQX-SLOGAN!"), {("zqx slogan", "hide")})
        self.assertEqual(self.found("zqx and slogan"), set())

    def test_inside_a_longer_word_only_holds(self):
        self.assertEqual(self.found("bigzqxslurword"), {("zqxslur", "hold")})
        self.assertEqual(self.found("bigzqxhateword"), {("zqxhate", "hold")})

    def test_allow(self):
        self.assertEqual(self.found("zqxslur free zone"), set())
        self.assertEqual(self.found("zqxslur"), {("zqxslur", "hide")})

    def test_numbers_only_whole(self):
        self.assertEqual(self.found("88"), {("88", "ambiguous")})
        for text in ("188", "880", "8.8", "88s", "b8"):
            with self.subTest(text):
                self.assertEqual(self.found(text), set())

    def test_accents_matter_only_for_accented_terms(self):
        self.assertEqual(self.found("ZQXPÉTER"), {("zqxpéter", "hold")})
        self.assertEqual(self.found("zqxpeter"), set())

    def test_scripts_without_spaces(self):
        self.assertEqual(self.found("这是测试词啊"), {("测试词", "hold")})

    def test_one_character_terms_are_skipped(self):
        self.assertEqual(self.found("x marks the spot"), set())

    def test_digits_only_entries_of_ldnoobw_are_skipped(self):
        t = st.Terms(self.path)
        t.add("13.", "hold", "ldnoobw/zh", digits=False)
        self.assertEqual(t.check("13"), [])

    def test_hit_fields(self):
        h, = self.t.check("zqx5lur", "title", 0.9)
        self.assertEqual((h["term"], h["level"], h["match"], h["source"], h["confidence"]),
                         ("zqxslur", "hide", "leet", "title", 0.9))
        self.assertEqual(self.t.check(""), [])
        self.assertEqual(self.t.check(None), [])

    def test_bad_section(self):
        path = terms_file("[nope]\nword\n")
        try:
            self.assertRaises(ValueError, st.Terms, path)
        finally:
            os.remove(path)


class ShippedLists(unittest.TestCase):
    """terms.txt (and LDNOOBW's lists when the models are here: SCREENING_MODELS) on the text
    racing photos carry: none of it may hide a photo, and only the codes are even noticed."""

    BENIGN = ["GOPRO MILLENNIA MASTERS", "Lamborghini Aventador LP700-4", "Nissan GT-R R34 845",
              "Ford Focus RS500 A 600", "Peter", "Dick Johnson Racing",
              "Dodge Challenger Scat Pack",
              "cul-de-sac", "Ford Pinto", "CON DEL FAN POT TRIO", "MOTUL CASTROL BREMBO",
              "PRESS START", "HORIZON 2012", "RACE 00:16.205", "Snigger", "Regina", "Inferno"]

    @classmethod
    def setUpClass(cls):
        cls.t = st.Terms(os.path.join(HERE, "wordlists", "terms.txt"),
                         LDNOOBW if os.path.isdir(LDNOOBW) else None,
                         os.path.join(HERE, "wordlists", "ldnoobw_skip.txt"))

    def test_benign_text(self):
        for text in self.BENIGN:
            with self.subTest(text):
                self.assertEqual(self.t.check(text), [])

    def test_codes_are_only_ambiguous(self):
        for text in ("88", "14", "18", "Camaro SS", "SS 396"):
            with self.subTest(text):
                self.assertEqual({h["level"] for h in self.t.check(text)}, {"ambiguous"})

    def test_combined_codes_and_slogans(self):
        for text in ("14/88", "1488", "HEIL HITLER", "Sieg-Heil", "K K K"):
            with self.subTest(text):
                self.assertIn("hide", {h["level"] for h in self.t.check(text)})

    def test_swastika_characters_are_ambiguous(self):
        # also a Buddhist and Hindu sign: they count only with a hate-symbol score
        for text in ("卐", "卍", "寺卍"):
            with self.subTest(text):
                self.assertEqual({(h["term"], h["level"]) for h in self.t.check(text)},
                                 {("manjisign", "ambiguous")})

    @unittest.skipUnless(os.path.isdir(LDNOOBW), "LDNOOBW lists not here (SCREENING_MODELS)")
    def test_ldnoobw_loaded(self):
        self.assertGreater(len(self.t.words[0]), 1000)
        self.assertEqual(len(self.t.sources), 28)
        self.assertEqual({h["level"] for h in self.t.check("shit")}, {"hold"})


if __name__ == "__main__":
    unittest.main()
