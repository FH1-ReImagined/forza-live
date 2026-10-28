"""Signed requests (forza_auth.py, forza_ed25519.py, #515): games and launchers sign with an
Ed25519 key and the server keeps only public keys. RFC 8032's vectors; the canonical string and a
vector shared with the game and the launcher; the clock window, replays, server names; the UDP
challenge and binding on first use; the upgrade from a raw friends key and the refusal of raw keys
after it; a conflicting upgrade; what a stolen stored value can do (nothing); raw keys until
--raw-key-cutoff. Drives the server's handlers directly."""

import hashlib
import json
import os
import struct
import tempfile
import time
import unittest
from types import SimpleNamespace

import forza_auth as auth
import forza_ed25519 as ed25519
import forza_friends as friends
import forza_live_lsp as lsp
import forza_live_server as srv
import forza_shader_lists as shader_lists
import forza_xtsw as x

SEED = bytes(range(32))   # DriverA's signing seed (live_identity.txt signing_seed)
KEY = bytes(range(16))    # DriverA's friends key (games before signed requests sent it)
XUID = 0x0009000000000001
OTHER = 0x0009000000000002
MAIN = "forza-reimagined.playit.plus"
TITLE = "/Services.Horizon/o.xtsw"
ALL = srv.FEATURE_INVITE_PICK | srv.FEATURE_FRIENDS | srv.FEATURE_CHAT

# RFC 8032 section 7.1: seed, public key, message, signature.
RFC8032 = [
    ("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60",
     "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("4ccd089b28ff96da9db6c346ec114e0f5b8a319f35aba624da8cf6ed4fb8a6fb",
     "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da085ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("c5aa8df43f9f837bedb7442f31dcb7b166d38535076f094b85ce3a2e0b4458f7",
     "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025", "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac18ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("833fe62409237b9d62ec77587520911e9a759cec1d19755b7da901b96dca3d42",
     "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b58909351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]

# The same vector is checked by forza-recomp's tools/test_live_signing.cpp and the launcher's
# test_live_auth.py: change all three together.
VECTOR = dict(when=1700000000, nonce="00112233445566778899aabbccddeeff",
              headers={"x-classname": "Forza.WebServices.User, Forza.WebServices",
                       "x-method": "Logon2"}, body=b"\x00\x01\x02")
VECTOR_PUB = "03a107bff3ce10be1d70dd18e74bc09967e4d6309ba50d5f1ddc8664125531b8"
VECTOR_SIGS = {
    MAIN: "f24472663146a0d6964c5ceb1d631bb15cf0cdb9f126aad2ba43ac700c1202e70c60dd512228f2bf4140d904bb755ba331cbf24446622161fbeb9fa84d33c60f",
    "my.server.example": "a89b4d608e6287a5c3c43d237b06e3c2ac922591cbda795b79f7011eca8f03f1608d170debc4a5a023c099c1746314708b278945b3589a83225ee3f80a9b6e03"}


def reset_auth():
    auth.SERVER_NAMES.clear()
    auth.RAW_KEY_CUTOFF = None
    auth.REBIND_LEGACY = False
    auth.COUNTS.clear()
    auth._nonces.clear()
    auth._raw_players.clear()
    auth._logged["refused"].clear()
    auth._budget[:] = [float(auth.VERIFY_RATE[1]), time.monotonic()]


class SchemeTest(unittest.TestCase):
    def test_rfc8032_vectors(self):
        for seed, pub, msg, sig in RFC8032:
            seed, pub, msg, sig = map(bytes.fromhex, (seed, pub, msg, sig))
            self.assertEqual(ed25519.public_key(seed), pub)
            self.assertEqual(ed25519.sign(seed, msg), sig)
            self.assertTrue(ed25519.verify(pub, msg, sig))
            self.assertFalse(ed25519.verify(pub, msg + b"!", sig))
            bad = bytearray(sig)
            bad[63] ^= 0x10
            self.assertFalse(ed25519.verify(pub, msg, bytes(bad)))
        # S >= L and a key that is not a point are refused
        seed, pub, msg, sig = map(bytes.fromhex, RFC8032[0])
        s = int.from_bytes(sig[32:], "little") + ed25519.L
        self.assertFalse(ed25519.verify(pub, msg, sig[:32] + s.to_bytes(32, "little")))
        self.assertFalse(ed25519.verify(b"\xff" * 32, msg, sig))

    def test_canonical_string(self):
        v = VECTOR
        text = auth.request_canonical("POST", TITLE, MAIN, XUID, v["when"], v["nonce"], v["headers"],
                                      v["body"])
        self.assertEqual(text, b"FORZA-ED25519-V1\nPOST\n/Services.Horizon/o.xtsw\n"
                               b"forza-reimagined.playit.plus\n0009000000000001\n1700000000\n"
                               b"00112233445566778899aabbccddeeff\n"
                               b"Forza.WebServices.User, Forza.WebServices\nLogon2\n"
                               + hashlib.sha256(b"\x00\x01\x02").hexdigest().encode())

    def test_vector(self):
        v = VECTOR
        self.assertEqual(ed25519.public_key(SEED).hex(), VECTOR_PUB)
        for name, sig in VECTOR_SIGS.items():
            header = auth.auth_header(SEED, name, XUID, "POST", TITLE, v["body"], v["headers"],
                                      v["when"], v["nonce"])
            self.assertEqual(header, "FORZA-ED25519-V1 Server=%s, Xuid=0009000000000001, "
                                     "Time=1700000000, Nonce=%s, Signature=%s" % (name, v["nonce"], sig))
            parsed = auth.parse_header(header)
            self.assertEqual((parsed["server"], parsed["xuid"], parsed["time"], parsed["signature"].hex()),
                             (name, XUID, 1700000000, sig))
        # what a game sends a server without signed requests (never for a main name)
        self.assertEqual(auth.old_server_key(KEY, "my.server.example").hex(), "d809345837c85fbdd0453daf311b4ef6")

    def test_server_names(self):
        for host, name in (("Forza-Reimagined.Playit.Plus", MAIN), ("forza.dyotson.dev:21200", "forza.dyotson.dev"),
                           ("[::1]:21200", "::1"), ("1.2.3.4", "1.2.3.4"), ("host.example.", "host.example")):
            self.assertEqual(auth.server_name(host), name)

    def test_parse_refuses_junk(self):
        for value in ("", "Basic abc", "FORZA-HMAC-SHA256-V1 Server=a, Xuid=1, Time=1, Nonce=%s, Signature=%s" % ("0" * 32, "0" * 128),
                      "FORZA-ED25519-V1 Server=a, Xuid=1, Time=1, Nonce=00, Signature=00",
                      "FORZA-ED25519-V1 Server=a, Xuid=zz, Time=1, Nonce=%s, Signature=%s" % ("0" * 32, "0" * 128)):
            self.assertIsNone(auth.parse_header(value))


class Base(unittest.TestCase):
    def setUp(self):
        reset_auth()
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = lsp.DATA_DIR
        lsp.DATA_DIR = self.tmp.name
        lsp._unverified_logged.clear()
        self.sent = []
        self.server = srv.ForzaLive("test", 64)
        self.server.transport = SimpleNamespace(sendto=lambda data, addr: self.sent.append((addr, data)))
        self.store = self.server.friends.store

    def tearDown(self):
        lsp.DATA_DIR = self.old_dir
        self.tmp.cleanup()
        reset_auth()

    @staticmethod
    def hello_body(xuid=XUID, token=1, name=b"DriverA"):
        body = struct.pack(">HQQIH", srv.VERSION, token, xuid, 0, 0)
        return body + bytes([4]) + b"#TU4" + bytes([len(name)]) + name + bytes([ALL])

    def hello(self, slot, addr=("127.0.0.1", 20001), **kw):
        body = self.hello_body(**kw)
        self.sent.clear()
        self.server.datagram_received(srv.MAGIC + bytes([srv.HELLO]) + body + bytes([len(slot)]) + slot, addr)
        return self.server.by_addr.get(addr), self.welcome(addr)

    def welcome(self, addr):
        """(flags, server time, challenge) of the WELCOME just sent, or None without its auth part."""
        for a, d in self.sent:
            if a == addr and d[4] == srv.WELCOME:
                at = 5 + 14 + struct.unpack_from(">H", d, 5 + 12)[0] + 2
                tail = d[at:]
                if not tail:
                    return None
                self.assertEqual(tail[0], auth.BLOCK)
                return tail[1], struct.unpack_from(">Q", tail, 2)[0], tail[10:26]
        return None

    def notices(self):
        return b"".join(d for a, d in self.sent if d[4] == friends.KIND)

    def sign_in(self, name=MAIN, seed=SEED, addr=("127.0.0.1", 20001), upgrade=None, **kw):
        """A signed game's sign-in: an empty slot, then the answer to the WELCOME's challenge
        (with an upgrade when the WELCOME says the XUID is bound to a raw friends key)."""
        _, (flags, _, nonce) = self.hello(b"", addr, **kw)
        if flags & auth.W_LEGACY and upgrade is None:
            upgrade = KEY
        block = auth.hello_block(seed, name, kw.get("xuid", XUID), nonce, self.hello_body(**kw),
                                 upgrade=upgrade if flags & auth.W_LEGACY else None)
        return self.hello(block, addr, **kw)

    def call(self, cls, method, params_type, params, name=MAIN, seed=SEED, xuid=XUID, signer=None,
             when=None, nonce=None, raw=None, tamper=None):
        headers = {"x-classname": "Forza.WebServices.%s, Forza.WebServices" % cls,
                   "x-method": method, "x-userid": "0x%x" % xuid}
        body = bytes(x.encode(params_type, params))
        if seed is not None:
            headers[auth.HEADER] = auth.auth_header(seed, name, signer or xuid, "POST", TITLE, body,
                                                    headers, when, nonce)
        if raw is not None:
            headers["x-friends-key"] = raw
        if tamper:
            body = tamper(headers, body)
        return lsp.service_call(headers, body, self.server, "POST", TITLE)

    def race_stats(self, place, **kw):
        stats = {"finished": True, "userFinishPosition": place, "maxPlaces": 8, "hopperId": 3}
        return self.call("User", "WriteRaceStatsEx", x.UserWriteRaceStats_Head,
                         {"id": 1, "raceStats": stats}, **kw)

    def read(self, **kw):
        return self.call("Hopper", "GetList", x.HopperGetList_Params,
                         {"maxCategoryResults": 2, "maxHopperResults": 32}, **kw)

    def last_race(self, xuid=XUID):
        path = os.path.join(self.tmp.name, "players", "%016X.json" % xuid)
        if not os.path.exists(path):
            return None
        with open(path) as f:
            return json.load(f).get("lastRace")

    @staticmethod
    def status(reply):
        return struct.unpack_from(">II", reply)


class SignedCallTest(Base):
    def setUp(self):
        super().setUp()
        peer, (flags, _, _) = self.sign_in()   # registers the public key
        self.assertTrue(peer.friends_ok)
        self.assertEqual(flags, auth.W_BOUND | auth.W_PROVEN | auth.W_CHECKED)

    def test_a_signed_call_is_the_player_s(self):
        self.assertEqual(self.status(self.race_stats(3)), (0, 0))
        self.assertEqual((self.last_race()["userFinishPosition"], self.last_race()["verified"]), (3, True))
        self.assertEqual(auth.COUNTS["signed"], 1)

    def test_only_the_public_key_is_kept(self):
        record = self.store.get(XUID)
        self.assertEqual((record["pub"], record["key"]), (ed25519.public_key(SEED).hex(), ""))
        self.assertNotIn(SEED.hex(), json.dumps(self.store.players))

    def test_the_clock_window(self):
        now = time.time()
        self.assertEqual(self.status(self.race_stats(4, when=now - 100)), (0, 0))
        self.assertEqual(self.status(self.race_stats(1, when=now - 200)), (2, 5))
        self.assertEqual(self.status(self.race_stats(1, when=now + 200)), (2, 5))
        self.assertEqual(self.last_race()["userFinishPosition"], 4)
        self.assertEqual(auth.COUNTS["refused clock"], 2)

    def test_a_replay_is_refused(self):
        nonce = os.urandom(16).hex()
        self.assertEqual(self.status(self.race_stats(3, nonce=nonce)), (0, 0))
        self.assertEqual(self.status(self.race_stats(1, nonce=nonce)), (2, 5))
        self.assertEqual(self.last_race()["userFinishPosition"], 3)
        self.assertEqual(auth.COUNTS["refused replay"], 1)

    def test_the_nonce_cache_is_bounded(self):
        old = auth.MAX_NONCES
        auth.MAX_NONCES = 5
        try:
            for _ in range(20):
                self.assertFalse(auth.seen_nonce(os.urandom(16).hex()))
            self.assertLessEqual(len(auth._nonces), 5)
        finally:
            auth.MAX_NONCES = old

    def test_a_changed_request_is_refused(self):
        def body(headers, b):
            return b[:-1] + bytes([b[-1] ^ 1])

        def method(headers, b):
            headers["x-method"] = "WriteRaceStats"
            return b
        for tamper in (body, method):
            self.assertEqual(self.status(self.race_stats(1, tamper=tamper)), (2, 5))
        self.assertIsNone(self.last_race())
        self.assertEqual(auth.COUNTS["refused signature"], 2)

    def test_another_player_s_signature_is_forged(self):
        other_seed = bytes([7] * 32)
        self.sign_in(seed=other_seed, xuid=OTHER, addr=("127.0.0.1", 20002), token=2)
        # signed by DriverA for a call naming OTHER, and by another key for DriverA
        self.assertEqual(self.status(self.race_stats(1, xuid=OTHER, signer=XUID)), (2, 5))
        self.assertEqual(self.status(self.race_stats(1, seed=other_seed)), (2, 5))
        self.assertIsNone(self.last_race(OTHER))
        self.assertIsNone(self.last_race())

    def test_stolen_stored_values_cannot_sign(self):
        """Whoever reads friends.json gets public keys (and the SHA-256 of friends keys not yet
        upgraded): neither signs, nor stands in for the friends key."""
        record = self.store.get(XUID)
        for fake_seed in (bytes.fromhex(record["pub"]), hashlib.sha256(KEY).digest()):
            self.assertEqual(self.status(self.race_stats(1, seed=fake_seed)), (2, 5))
        self.assertEqual(self.status(self.race_stats(1, seed=None, raw=record["pub"][:32])), (2, 5))
        self.assertIsNone(self.last_race())

    def test_server_names(self):
        self.assertEqual(self.status(self.race_stats(5, name="some.other.name")), (0, 0))  # any name
        auth.SERVER_NAMES.update({MAIN, "forza.dyotson.dev"})
        self.assertEqual(self.status(self.race_stats(2, name="forza.dyotson.dev")), (0, 0))
        self.assertEqual(self.status(self.race_stats(1, name="some.other.name")), (2, 5))
        self.assertEqual(self.last_race()["userFinishPosition"], 2)
        self.assertEqual(auth.COUNTS["refused name"], 1)

    def test_reads_are_answered_as_today(self):
        for kw in ({}, {"seed": bytes([9] * 32)}, {"when": 0}, {"seed": None}):
            self.assertEqual(self.status(self.read(**kw)), (0, 0))

    def test_unbound_xuids_are_unverified(self):
        stranger = 0x0009000000000099
        self.assertEqual(self.status(self.race_stats(4, xuid=stranger, seed=bytes(32))), (0, 0))
        self.assertEqual(self.last_race(stranger)["verified"], False)

    def test_the_reply_tells_the_server_time(self):
        head = srv.response(200, b"x").split(b"\r\n\r\n")[0].decode()
        stamp = [line for line in head.split("\r\n") if line.startswith("X-Forza-Time: ")]
        self.assertEqual(len(stamp), 1)
        self.assertLessEqual(abs(int(stamp[0].split(": ")[1]) - time.time()), 2)

    def test_checks_have_a_budget(self):
        auth._budget[:] = [2.0, time.monotonic()]
        old = auth.VERIFY_RATE
        auth.VERIFY_RATE = (0.001, 2)
        try:
            results = [self.status(self.race_stats(i + 1)) for i in range(4)]
        finally:
            auth.VERIFY_RATE = old
        self.assertEqual(results[:2], [(0, 0), (0, 0)])
        self.assertEqual(results[2:], [(2, 5), (2, 5)])
        self.assertEqual(auth.COUNTS["refused busy"], 2)


class UdpSignInTest(Base):
    def test_the_welcome_has_the_time_and_a_challenge(self):
        peer, (flags, when, nonce) = self.hello(b"")
        self.assertFalse(peer.friends_ok)
        self.assertEqual(flags, 0)
        self.assertLessEqual(abs(when - time.time()), 2)
        self.assertEqual(len(nonce), 16)
        self.assertEqual(self.hello(b"")[1][2], nonce)  # the same for the address

    def test_binding_on_first_use_and_proof(self):
        peer, (flags, _, _) = self.sign_in("my.server.example")
        self.assertTrue(peer.friends_ok)
        self.assertEqual(self.store.get(XUID)["pub"], VECTOR_PUB)
        self.assertEqual(auth.COUNTS["registered"], 1)
        nonce = self.welcome(("127.0.0.1", 20001))[2]
        peer, (flags, _, _) = self.hello(auth.hello_block(SEED, "my.server.example", XUID, nonce, self.hello_body()))
        self.assertTrue(peer.friends_ok)
        self.assertEqual(flags, auth.W_BOUND | auth.W_PROVEN | auth.W_CHECKED)
        self.assertEqual(auth.COUNTS["registered"], 1)
        self.assertEqual(self.status(self.race_stats(2, name="my.server.example")), (0, 0))

    def test_a_proof_does_not_work_from_another_address(self):
        _, (_, _, nonce) = self.hello(b"")
        block = auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body())
        self.assertTrue(self.hello(block)[0].friends_ok)
        other, (flags, _, _) = self.hello(block, ("127.0.0.1", 20009), token=2)
        self.assertFalse(other.friends_ok)
        self.assertEqual(flags, auth.W_BOUND | auth.W_CHECKED)
        # nor for a changed HELLO (another gamertag) from the same address
        self.assertFalse(self.hello(block, name=b"Mallory")[0].friends_ok)

    def test_the_clock_and_name_of_a_hello(self):
        auth.SERVER_NAMES.add(MAIN)
        _, (_, _, nonce) = self.hello(b"")
        late = auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body(), when=time.time() - 300)
        self.assertFalse(self.hello(late)[0].friends_ok)
        stray = auth.hello_block(SEED, "elsewhere.example", XUID, nonce, self.hello_body())
        self.assertFalse(self.hello(stray)[0].friends_ok)
        self.assertFalse(auth.bound(self.store, XUID))

    def test_another_installation_is_told(self):
        self.sign_in()
        peer, (flags, _, _) = self.sign_in(seed=bytes([5] * 32), addr=("127.0.0.1", 20002), token=2)
        self.assertFalse(peer.friends_ok)
        self.assertEqual(flags, auth.W_BOUND | auth.W_CHECKED)
        self.assertEqual(self.store.get(XUID)["pub"], VECTOR_PUB)
        self.assertIn(b"linked to another installation", self.notices())
        self.server.chat.say(peer, 1, 0, "hi")
        self.assertIn(b"Chat is off: this account is linked to another installation",
                      b"".join(d for a, d in self.sent))

    def test_no_notice_before_the_answer(self):
        """FRIEND messages sent between the first HELLO and the answer to its challenge are
        dropped quietly; a game whose signature fails is told."""
        peer, (_, _, nonce) = self.hello(b"")
        self.sent.clear()
        self.server.friends.on_message(peer, struct.pack(">BI", friends.LIST, 0))
        self.assertEqual(self.notices(), b"")
        bad = bytearray(auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body()))
        bad[40] ^= 1
        peer, _ = self.hello(bytes(bad))
        self.sent.clear()
        self.server.friends.on_message(peer, struct.pack(">BI", friends.LIST, 0))
        self.assertIn(b"Friends are off", self.notices())

    def test_unchanged_keep_alives_are_not_checked_again(self):
        _, (_, _, nonce) = self.hello(b"")
        for _ in range(5):
            block = auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body())
            self.assertTrue(self.hello(block)[0].friends_ok)
        self.assertEqual((auth.COUNTS["registered"] + auth.COUNTS["signed hello"], auth.COUNTS["hello unchanged"]), (2, 3))
        # a changed HELLO is checked: a bad signature with another gamertag is refused
        bad = bytearray(auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body(name=b"Mallory")))
        bad[40] ^= 1
        self.assertFalse(self.hello(bytes(bad), name=b"Mallory")[0].friends_ok)

    def test_a_player_s_checks_have_a_budget(self):
        _, (_, _, nonce) = self.hello(b"")
        for i in range(30):
            junk = bytearray(auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body()))
            junk[20] ^= 1 + i % 7
            self.hello(bytes(junk))
        self.assertLessEqual(auth.COUNTS["refused signature"], auth.HELLO_CHECKS[1] + 1)
        self.assertGreater(auth.COUNTS["hello checks skipped"], 20)


class UpgradeTest(Base):
    """An XUID bound to its raw friends key by a game before signed requests."""

    def setUp(self):
        super().setUp()
        peer, (flags, _, _) = self.hello(KEY)   # the old game
        self.assertTrue(peer.friends_ok)
        self.assertEqual(flags, auth.W_LEGACY | auth.W_PROVEN)
        self.addr = ("127.0.0.1", 20003)

    def upgrade(self, seed=SEED, raw=KEY, addr=None, token=3):
        addr = addr or self.addr
        _, (flags, _, nonce) = self.hello(b"", addr, token=token)
        block = auth.hello_block(seed, MAIN, XUID, nonce, self.hello_body(token=token), upgrade=raw)
        return self.hello(block, addr, token=token)

    def test_the_upgrade(self):
        with self.assertLogs("forza-live", "INFO") as logs:
            peer, (flags, _, _) = self.upgrade()
        self.assertTrue(peer.friends_ok)
        self.assertEqual(flags, auth.W_BOUND | auth.W_PROVEN | auth.W_CHECKED)
        record = self.store.get(XUID)
        self.assertEqual((record["pub"], record["key"]), (VECTOR_PUB, ""))
        self.assertTrue(any("upgraded from its friends key" in m for m in logs.output))
        self.assertEqual(auth.COUNTS["upgraded"], 1)
        self.assertEqual(self.status(self.race_stats(2)), (0, 0))

    def test_raw_keys_are_refused_after_its_upgrade(self):
        self.assertEqual(self.status(self.race_stats(3, seed=None, raw=KEY.hex())), (0, 0))
        self.upgrade()
        # no cutoff set: the XUID's own upgrade ends its raw key everywhere
        self.assertEqual(self.status(self.race_stats(1, seed=None, raw=KEY.hex())), (2, 5))
        peer, (flags, _, _) = self.hello(KEY, ("127.0.0.1", 20004), token=4)
        self.assertFalse(peer.friends_ok)
        self.assertEqual(flags, auth.W_BOUND)
        self.sent.clear()
        self.server.friends.on_message(peer, struct.pack(">BI", friends.LIST, 0))
        self.assertIn(b"too old for this server", self.notices())
        raw = {"x-xuid": "%016X" % XUID, "x-friends-key": KEY.hex()}
        self.assertIsNone(shader_lists.authenticate(self.store, raw))
        self.assertFalse(self.store.claim(XUID, KEY))
        self.assertEqual(self.last_race()["userFinishPosition"], 3)
        self.assertEqual(self.status(self.read(seed=None, raw=KEY.hex())), (0, 0))

    def test_an_upgrade_needs_the_friends_key(self):
        peer, (flags, _, _) = self.upgrade(raw=bytes([9] * 16))
        self.assertFalse(peer.friends_ok)
        self.assertEqual(flags, auth.W_LEGACY | auth.W_CHECKED)
        self.assertEqual(self.store.get(XUID)["key"], friends.key_hash(KEY))
        # nor is a signed sign-in without the upgrade enough
        _, (_, _, nonce) = self.hello(b"", self.addr, token=3)
        plain = auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body(token=3))
        self.assertFalse(self.hello(plain, self.addr, token=3)[0].friends_ok)
        self.assertFalse(auth.bound_pub(self.store, XUID))

    def test_a_conflicting_upgrade(self):
        """Someone who copied the friends key upgrades first: the player's own upgrade is refused
        and its game says why; the copied key works no more either."""
        thief = bytes([0xEE] * 32)
        self.assertTrue(self.upgrade(seed=thief, addr=("127.0.0.1", 20005), token=5)[0].friends_ok)
        peer, (flags, _, _) = self.upgrade()
        self.assertFalse(peer.friends_ok)
        self.assertEqual(flags, auth.W_BOUND | auth.W_CHECKED)
        self.assertIn(b"linked to another installation", self.notices())
        self.assertEqual(self.store.get(XUID)["pub"], ed25519.public_key(thief).hex())
        self.assertEqual(auth.COUNTS["refused another installation"], 1)

    def test_rebind_legacy(self):
        auth.REBIND_LEGACY = True
        _, (_, _, nonce) = self.hello(b"", self.addr, token=3)
        plain = auth.hello_block(SEED, "127.0.0.1", XUID, nonce, self.hello_body(token=3))
        self.assertTrue(self.hello(plain, self.addr, token=3)[0].friends_ok)
        self.assertEqual(self.store.get(XUID)["pub"], VECTOR_PUB)
        self.assertEqual(auth.COUNTS["rebound"], 1)

    def test_community_event_enrollment_follows(self):
        old = self.server.by_addr[("127.0.0.1", 20001)]
        self.server.events.enroll(old)
        self.assertEqual(self.server.events.db.execute("SELECT credential FROM identities").fetchone()[0],
                         friends.key_hash(KEY))
        peer, _ = self.upgrade()
        self.assertEqual(self.server.events.db.execute("SELECT credential FROM identities").fetchone()[0],
                         "pub:" + VECTOR_PUB)
        self.assertTrue(self.server.events.authenticated(peer))


class RawKeyTest(Base):
    """Games and launchers before signed requests, for XUIDs still bound to their friends key."""

    def setUp(self):
        super().setUp()
        peer, _ = self.hello(KEY)
        self.assertTrue(peer.friends_ok)

    def test_raw_keys_until_the_cutoff(self):
        self.assertEqual(self.status(self.race_stats(3, seed=None, raw=KEY.hex())), (0, 0))
        self.assertEqual(self.last_race()["verified"], True)
        self.assertEqual((auth.COUNTS["raw http"], auth.COUNTS["raw hello"]), (1, 1))
        self.assertEqual(self.server.status()["auth"]["raw_key_players"], 1)
        auth.RAW_KEY_CUTOFF = int(time.time()) - 1
        self.assertEqual(self.status(self.race_stats(1, seed=None, raw=KEY.hex())), (2, 5))
        self.assertEqual(self.last_race()["userFinishPosition"], 3)
        peer, _ = self.hello(KEY)
        self.assertFalse(peer.friends_ok)
        self.sent.clear()
        self.server.friends.on_message(peer, struct.pack(">BI", friends.LIST, 0))
        self.server.chat.say(peer, 1, 0, "hi")
        told = b"".join(d for a, d in self.sent)
        self.assertIn(b"Friends are off: this version of the game is too old", told)
        self.assertIn(b"Chat is off: this version of the game is too old", told)
        self.assertEqual(self.status(self.read(seed=None, raw=KEY.hex())), (0, 0))
        # an upgrade still works after the cutoff
        self.assertTrue(self.sign_in(addr=("127.0.0.1", 20006), token=6)[0].friends_ok)
        self.assertEqual(self.status(self.race_stats(2)), (0, 0))

    def test_a_cutoff_in_the_future_changes_nothing(self):
        auth.RAW_KEY_CUTOFF = int(time.time()) + 3600
        self.assertEqual(self.status(self.race_stats(3, seed=None, raw=KEY.hex())), (0, 0))
        self.assertTrue(self.hello(KEY)[0].friends_ok)

    def test_a_stolen_hash_is_not_the_key(self):
        stolen = self.store.get(XUID)["key"]
        for raw in (stolen[:32], bytes.fromhex(stolen)[:16].hex()):
            self.assertEqual(self.status(self.race_stats(1, seed=None, raw=raw)), (2, 5))
        self.assertFalse(self.upgrade_with(bytes.fromhex(stolen)[:16]))
        self.assertIsNone(self.last_race())

    def upgrade_with(self, raw):
        addr = ("127.0.0.1", 20007)
        _, (_, _, nonce) = self.hello(b"", addr, token=7)
        block = auth.hello_block(bytes([3] * 32), MAIN, XUID, nonce, self.hello_body(token=7), upgrade=raw)
        return self.hello(block, addr, token=7)[0].friends_ok


class LauncherEndpointTest(unittest.TestCase):
    """The shader list upload and co-op results (forza_shader_lists.authenticate)."""

    def setUp(self):
        reset_auth()
        self.store = friends.Store()
        self.store.get(XUID, create=True)["pub"] = VECTOR_PUB

    def tearDown(self):
        reset_auth()

    def test_signed(self):
        body = b'{"version": "x"}'
        path = "/shader-lists/upload"
        signed = {auth.HEADER: auth.auth_header(SEED, MAIN, XUID, "POST", path, body), "x-xuid": "%016X" % XUID}
        self.assertEqual(shader_lists.authenticate(self.store, signed, "POST", path, body), XUID)
        self.assertIsNone(shader_lists.authenticate(self.store, signed, "POST", path, body))  # replay
        other = {auth.HEADER: auth.auth_header(SEED, MAIN, XUID, "POST", path, body + b" ")}
        self.assertIsNone(shader_lists.authenticate(self.store, other, "POST", path, body))
        self.assertIsNone(shader_lists.authenticate(self.store, {"x-xuid": "%016X" % XUID,
                                                                 "x-friends-key": KEY.hex()}, "POST", path, body))

    def test_raw_for_an_xuid_still_bound_to_it(self):
        store = friends.Store()
        store.claim(XUID, KEY)
        raw = {"x-xuid": "%016X" % XUID, "x-friends-key": KEY.hex()}
        self.assertEqual(shader_lists.authenticate(store, raw), XUID)
        auth.RAW_KEY_CUTOFF = 0
        self.assertIsNone(shader_lists.authenticate(store, raw))


if __name__ == "__main__":
    unittest.main()
