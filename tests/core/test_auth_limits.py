"""What signing in costs its sender (auth.py, friends.Store, server.on_hello): first-use bindings
have a budget per address and for the whole server, raw keys bind nothing after the cutoff, a
registered game names another XUID only at the sign-in rate, the store of bindings is capped and
never forgets one, signature checks are charged to who asks for them (an address whose signatures
keep failing gets none for a while, whoever's XUID it names), and upgrades from a raw key end.
Drives the server's handlers directly."""

import hashlib
import json
import os
import struct
import time
from unittest import mock

from forza_live.core import auth
from forza_live.core import ed25519
from forza_live.core import limits
from forza_live.social import friends
from forza_live.title_services import lsp
from forza_live import server as srv
from forza_live.network import xtsw as x
from tests.core.test_auth import ALL, KEY, MAIN, OTHER, SEED, SEED_B, THIRD, TITLE, XUID, Base

VICTIM = ("198.51.100.7", 3074)


def seed_of(n):
    return hashlib.sha256(b"seed %d" % n).digest()


class LimitsBase(Base):
    def new_player(self, n, addr, seed=None, token=None):
        """Player n (its own XUID and key) signs in from addr: (peer, WELCOME)."""
        return self.sign_in(seed=seed or seed_of(n), addr=addr, xuid=0x0009000000001000 + n,
                            token=token or 1000 + n)

    def bound(self):
        return sum(1 for p in self.store.players.values() if p.get("pub") or p.get("key"))

    def errors(self, addr):
        """The codes of the ERRORs the last datagram was answered with."""
        return [d[5] for a, d in self.sent if a == addr and d[4] == srv.ERROR]


class FirstUseTest(LimitsBase):
    def test_a_flood_of_raw_keys_binds_a_few_xuids(self):
        """One datagram with 16 random bytes bound an XUID nobody had: any number of them."""
        with mock.patch.object(ed25519, "verify", side_effect=AssertionError("checked")):
            for i in range(60):
                self.hello(os.urandom(16), ("203.0.113.%d" % (1 + i), 3074), xuid=0x0009000000002000 + i,
                           token=2000 + i)
        self.assertEqual(self.bound(), auth.FIRST_USE_RAW[1])
        self.assertEqual(len(self.store.players), auth.FIRST_USE_RAW[1])
        self.assertEqual(auth.COUNTS["first use refused"], 60 - auth.FIRST_USE_RAW[1])

    def test_an_old_game_still_binds_its_key(self):
        peer, _ = self.hello(KEY)
        self.assertTrue(peer.friends_ok)
        self.assertTrue(self.hello(KEY)[0].friends_ok)  # and proves itself with it afterwards
        self.assertEqual(auth.COUNTS["first use refused"], 0)

    def test_a_registered_game_names_another_xuid_at_the_sign_in_rate(self):
        """A HELLO with another XUID on a registered game's address and token cost nothing: one
        socket named a new XUID in every datagram."""
        addr = ("203.0.113.9", 3074)
        named, errors = set(), []
        for i in range(60):
            peer, _ = self.hello(os.urandom(16), addr, xuid=0x0009000000003000 + i, token=7)
            named.add(peer.xuid)
            errors += self.errors(addr)
        self.assertEqual(len(named), srv.HELLOS_PER_IP[1])
        self.assertEqual(set(errors), {srv.ERR_SLOW_DOWN})
        self.assertLessEqual(self.bound(), auth.FIRST_USE_RAW[1])
        self.assertEqual(len(self.server.peers), 1)
        self.assertEqual(self.server.per_ip, {"203.0.113.9": 1})

    def test_another_profile_is_still_one_hello_away(self):
        peer, _ = self.sign_in()
        self.assertTrue(peer.friends_ok)
        again, _ = self.sign_in(xuid=THIRD)
        self.assertIs(again, peer)
        self.assertTrue(again.friends_ok)
        self.assertEqual(self.store.get(THIRD)["pub"], ed25519.public_key(SEED).hex())

    def test_one_address_binds_as_many_new_players_as_it_may_have(self):
        ip = "203.0.113.20"
        peers = [self.new_player(n, (ip, 4000 + n))[0] for n in range(srv.MAX_PEERS_PER_IP)]
        self.assertTrue(all(p.friends_ok for p in peers))
        self.assertEqual(self.bound(), auth.FIRST_USE_SOURCE[1])
        # one more new XUID from there (a player's second profile): not bound for now, and told so
        late = dict(seed=seed_of(0), addr=(ip, 4000), xuid=0x0009000000001100, token=1000)
        with self.assertLogs("forza-live", "WARNING") as logs:
            peer, (flags, _, _) = self.sign_in(**late)
            self.sign_in(**late)
        self.assertFalse(peer.friends_ok)
        self.assertFalse(flags & (auth.W_BOUND | auth.W_PROVEN))
        self.assertIsNone(self.store.get(0x0009000000001100))
        self.assertEqual(len([line for line in logs.output if "more new players from 203.0.113.20" in line]), 1)
        self.sent.clear()
        self.server.friends.on_message(peer, struct.pack(">BI", friends.LIST, 0))
        self.assertIn(b"Friends are off for now", self.notices())
        # another address is not held back by it
        self.assertTrue(self.new_player(50, ("203.0.113.21", 4000))[0].friends_ok)
        # and an hour later the address has bindings again
        self.store.first_use.sources[ip][0] = 1.0
        self.assertTrue(self.sign_in(**late)[0].friends_ok)

    def test_the_whole_server_binds_so_many_at_once(self):
        with mock.patch.object(auth, "FIRST_USE_SIGNED", (0.001, 3)):
            self.store.first_use = auth.FirstUse()
            proven = [self.new_player(n, ("203.0.113.%d" % (30 + n), 3074))[0].friends_ok for n in range(6)]
        self.assertEqual(proven, [True] * 3 + [False] * 3)
        self.assertEqual(self.bound(), 3)

    def test_a_refused_first_use_costs_no_check(self):
        with mock.patch.object(auth, "FIRST_USE_SIGNED", (0.001, 0)):
            self.store.first_use = auth.FirstUse()
            with mock.patch.object(ed25519, "verify", side_effect=AssertionError("checked")):
                self.assertFalse(self.new_player(1, ("203.0.113.40", 3074))[0].friends_ok)

    def test_no_raw_key_binds_after_the_cutoff(self):
        auth.RAW_KEY_CUTOFF = int(time.time()) - 1
        peer, (flags, _, _) = self.hello(KEY)
        self.assertFalse(peer.friends_ok)
        self.assertTrue(peer.auth_outdated)  # told to update the game
        self.assertIsNone(self.store.get(XUID))
        self.assertEqual(flags, 0)
        self.assertTrue(self.sign_in()[0].friends_ok)  # a game that signs still binds it


class StoreTest(LimitsBase):
    def test_the_store_is_capped(self):
        with mock.patch.object(limits, "MAX_PLAYERS", 3):
            peers = [self.new_player(n, ("203.0.113.%d" % (50 + n), 3074))[0] for n in range(5)]
            self.assertEqual([p.friends_ok for p in peers], [True] * 3 + [False] * 2)
            self.assertFalse(self.hello(os.urandom(16), ("203.0.113.60", 3074), xuid=OTHER, token=9)[0].friends_ok)
            self.assertEqual(len(self.store.players), 3)
            with self.assertRaises(friends.Full):
                self.store.get(0x0009000000009999, create=True)
            # a player blocks XUIDs nobody has: no record for them either, and nothing breaks
            for i in range(5):
                self.server.datagram_received(
                    srv.MAGIC + bytes([friends.KIND]) + struct.pack(">BQ", friends.BLOCK, 0x000900000000A000 + i)
                    + friends.text8("Nobody%d" % i), peers[0].addr)
            self.assertEqual(len(self.store.players), 3)
            # the players who have a record are who they were
            self.assertTrue(self.new_player(0, ("203.0.113.50", 3074))[0].friends_ok)

    def write(self, players):
        path = os.path.join(self.tmp.name, friends.FILE)
        with open(path, "w") as f:
            json.dump({"version": 1, "players": players}, f)
        return path

    def test_a_binding_is_never_forgotten(self):
        """A player away for FORGET_DAYS with no friends lost its key with its record: its XUID
        then went to the first key that named it, with its player record and scores."""
        old = time.time() - (friends.FORGET_DAYS + 20) * 86400
        blank = {"name": "", "seen": old, "key": "", "friends": {}, "in": {}, "out": {}, "blocked": {}}
        pub = ed25519.public_key(SEED).hex()
        store = friends.Store(self.write({
            "%016X" % XUID: dict(blank, name="DriverA", pub=pub),
            "%016X" % OTHER: dict(blank, name="DriverB", key=friends.key_hash(KEY)),
            "%016X" % THIRD: dict(blank, name="Blocked once"),
            "0009000000000004": dict(blank, name="Has a block", blocked={"%016X" % THIRD: old})}))
        store.load()
        self.assertEqual(sorted(store.players), ["%016X" % XUID, "%016X" % OTHER, "0009000000000004"])
        self.assertEqual(auth.bound_pub(store, XUID).hex(), pub)
        # so the XUID is still its owner's, and nobody else's
        self.server.friends.store.players = store.players
        thief, (flags, _, _) = self.sign_in(seed=SEED_B, addr=("203.0.113.70", 3074), token=70)
        self.assertFalse(thief.friends_ok)
        self.assertTrue(thief.auth_conflict)
        self.assertTrue(self.sign_in()[0].friends_ok)


class ChecksTest(LimitsBase):
    """Who a signature check is charged to."""

    ATTACKER = "203.0.113.80"

    def junk(self, n, xuid, port=None):
        """The attacker's game n, registered and told its challenge, signs a HELLO for `xuid`
        with a key of its own: a signature that does not check out."""
        addr = (self.ATTACKER, port or 5000 + n % srv.MAX_PEERS_PER_IP)
        kw = dict(xuid=xuid, token=5000 + n % srv.MAX_PEERS_PER_IP, name=b"Junk")
        _, (_, _, nonce) = self.hello(b"", addr, **kw)
        block = bytearray(auth.hello_block(seed_of(900), MAIN, xuid, nonce, self.hello_body(**kw)))
        block[30] ^= 1
        return self.hello(bytes(block), addr, **kw)

    def test_a_hello_before_its_challenge_costs_no_check(self):
        """A signature made before this address was sent its challenge cannot be right."""
        body = self.hello_body()
        block = auth.hello_block(SEED, MAIN, XUID, os.urandom(16), body)
        with mock.patch.object(ed25519, "verify", side_effect=AssertionError("checked")):
            peer, (flags, _, nonce) = self.hello(block, ("203.0.113.81", 3074))
        self.assertFalse(peer.friends_ok)
        self.assertEqual(auth.COUNTS["hello before its challenge"], 1)
        # with the challenge its WELCOME carried, it is checked and proven
        peer, _ = self.hello(auth.hello_block(SEED, MAIN, XUID, nonce, body), ("203.0.113.81", 3074))
        self.assertTrue(peer.friends_ok)

    def flood(self, count=120):
        """Junk signed HELLOs from the attacker's address for XUIDs nobody has: how many were
        checked."""
        with mock.patch.object(auth, "HELLO_CHECKS", (1000, 1000)), \
                mock.patch.object(auth, "FIRST_USE_SIGNED", (1000, 1000)), \
                mock.patch.object(ed25519, "verify", wraps=ed25519.verify) as verify:
            self.store.first_use = auth.FirstUse()
            for n in range(count):
                self.assertFalse(self.junk(n, 0x0009000000004000 + n % srv.MAX_PEERS_PER_IP)[0].friends_ok)
            return verify.call_count

    def test_an_address_has_a_budget_of_checks(self):
        with mock.patch.object(auth, "BAD_LIMIT", 10 ** 6), \
                mock.patch.object(auth, "SOURCE_CHECKS", (0.001, auth.SOURCE_CHECKS[1])):
            checked = self.flood(300)
        self.assertEqual(checked, auth.SOURCE_CHECKS[1])
        self.assertEqual(auth.COUNTS["checks over an address's budget"], 300 - auth.SOURCE_CHECKS[1])
        self.assertEqual(auth.COUNTS["checks over budget"], 0)  # the server's own was not touched

    def test_failed_signatures_stop_an_address_s_checks(self):
        with mock.patch.object(auth, "SOURCE_CHECKS", (1000, 1000)), \
                self.assertLogs("forza-live", "WARNING") as logs:
            checked = self.flood()
        self.assertEqual(checked, auth.BAD_LIMIT)
        self.assertEqual(auth.COUNTS["checks skipped after bad signatures"], 120 - auth.BAD_LIMIT)
        warned = [line for line in logs.output if "did not check out" in line]
        self.assertEqual(len(warned), 1, logs.output)
        self.assertIn(self.ATTACKER, warned[0])

    def test_naming_a_victim_s_xuid_does_not_stop_the_victim(self):
        victim, _ = self.sign_in(addr=VICTIM)
        self.assertTrue(victim.friends_ok)
        victim.auth_proved_at -= auth.HELLO_RECHECK + 1  # its next keep-alive is checked again
        self.flood()  # the attacker's address is out of checks...
        for n in range(3):  # ...and names the victim's XUID besides: refused without a check
            self.assertTrue(self.junk(n, XUID)[0].auth_conflict)
        _, (_, _, nonce) = self.hello(b"", VICTIM)
        with mock.patch.object(ed25519, "verify", wraps=ed25519.verify) as verify:
            peer, _ = self.hello(auth.hello_block(SEED, MAIN, XUID, nonce, self.hello_body()), VICTIM)
        self.assertTrue(peer.friends_ok)
        self.assertEqual(verify.call_count, 1)
        self.assertEqual(self.status(self.race_stats(1)), (0, 0))  # and its signed calls

    def forged_call(self, who):
        """A read for DriverA's XUID with a made-up signature on the connection `who`."""
        headers = {"x-classname": "Forza.WebServices.Hopper, Forza.WebServices", "x-method": "GetList",
                   "x-userid": "0x%x" % XUID}
        body = bytes(x.encode(x.HopperGetList_Params, {"maxCategoryResults": 2, "maxHopperResults": 32}))
        headers[auth.HEADER] = auth.auth_header(seed_of(901), MAIN, XUID, "POST", TITLE, body, headers)
        entered = auth.enter(who)
        try:
            return lsp.service_call(headers, body, self.server, "POST", TITLE)
        finally:
            auth.leave(entered)

    def test_http_checks_are_charged_to_the_connection_s_address(self):
        """One connection posted calls with made-up signatures for an XUID from the status page:
        each cost a check from the one budget every player's calls share."""
        self.sign_in(addr=VICTIM)
        with mock.patch.object(ed25519, "verify", wraps=ed25519.verify) as verify, \
                self.assertLogs("forza-live", "WARNING") as logs:
            for _ in range(3):  # new connections do not renew the address's budget
                who = auth.Client(self.ATTACKER, connection=True, checks=self.store.checks)
                for _ in range(100):
                    self.assertEqual(self.status(self.forged_call(who)), (0, 0))  # a read: answered
            self.assertLessEqual(verify.call_count, auth.BAD_LIMIT + 2)
        self.assertEqual(len([line for line in logs.output if "did not check out" in line]), 1)
        self.assertIn(self.ATTACKER, "\n".join(logs.output))
        self.assertEqual(auth.COUNTS["checks over budget"], 0)
        # the player's own connection, from its own address, is checked as ever
        mine = auth.Client(VICTIM[0], connection=True, checks=self.store.checks)
        entered = auth.enter(mine)
        try:
            self.assertEqual(self.status(self.race_stats(1)), (0, 0))
        finally:
            auth.leave(entered)
        self.assertTrue(mine.proved)
        self.assertFalse(who.proved)

    def test_connections_without_an_address_share_a_pool(self):
        """A tunnel that names no address: each connection has its own budget and count of bad
        signatures, and all of them one pool, apart from the players whose address is known."""
        self.sign_in(addr=VICTIM)
        with mock.patch.object(auth, "POOL_CHECKS", (0.001, auth.POOL_CHECKS[1])), \
                mock.patch.object(ed25519, "verify", wraps=ed25519.verify) as verify:
            one = auth.Client(None, connection=True, checks=self.store.checks)
            for _ in range(100):
                self.forged_call(one)
            self.assertEqual(verify.call_count, auth.BAD_LIMIT)  # that connection gets no more
            for _ in range(20):
                who = auth.Client(None, connection=True, checks=self.store.checks)
                for _ in range(30):
                    self.forged_call(who)
            self.assertEqual(verify.call_count, auth.POOL_CHECKS[1])
        self.assertGreater(auth.COUNTS["checks over the pool's budget"], 0)
        mine = auth.Client(VICTIM[0], connection=True, checks=self.store.checks)
        entered = auth.enter(mine)
        try:
            self.assertEqual(self.status(self.race_stats(1)), (0, 0))
        finally:
            auth.leave(entered)


class UpgradeEndTest(LimitsBase):
    """An XUID still bound to the raw friends key of a game before signed requests."""

    THIEF = ("203.0.113.90", 3074)

    def setUp(self):
        super().setUp()
        self.assertTrue(self.hello(KEY)[0].friends_ok)

    def steal(self):
        """Someone who copied the raw key signs in with a key of their own and the upgrade."""
        body = dict(token=90)
        _, (flags, _, nonce) = self.hello(b"", self.THIEF, **body)
        block = auth.hello_block(SEED_B, MAIN, XUID, nonce, self.hello_body(**body), upgrade=KEY)
        return flags, self.hello(block, self.THIEF, **body)[0]

    def test_an_upgrade_is_logged_with_its_address(self):
        with self.assertLogs("forza-live", "WARNING") as logs:
            self.assertTrue(self.sign_in()[0].friends_ok)
        line = [line for line in logs.output if "upgraded from its friends key" in line]
        self.assertEqual(len(line), 1, logs.output)
        self.assertIn("from 127.0.0.1", line[0])
        self.assertNotIn(KEY.hex(), "\n".join(logs.output))

    def test_upgrades_end_after_the_grace(self):
        auth.RAW_KEY_CUTOFF = int(time.time()) - auth.UPGRADE_GRACE - 1
        self.assertEqual(auth.upgrade_cutoff(), auth.RAW_KEY_CUTOFF + auth.UPGRADE_GRACE)
        flags, thief = self.steal()
        self.assertFalse(flags & auth.W_LEGACY)  # nor is anyone told the XUID is still on a raw key
        self.assertFalse(thief.friends_ok)
        self.assertIsNone(auth.bound_pub(self.store, XUID))
        self.assertEqual(self.store.get(XUID)["key"], friends.key_hash(KEY))
        self.assertEqual(auth.COUNTS["refused upgrade"], 1)
        # the owner's game is told whom to ask
        owner, _ = self.sign_in()
        self.assertFalse(owner.friends_ok)
        self.assertIn(b"ask on Discord", self.notices())
        # the operator releases the XUID (its key cleared): the next signed game binds it
        self.store.get(XUID)["key"] = ""
        self.assertTrue(self.sign_in()[0].friends_ok)
        self.assertEqual(self.store.get(XUID)["pub"], ed25519.public_key(SEED).hex())

    def test_a_date_of_its_own(self):
        auth.UPGRADE_CUTOFF = int(time.time()) - 1
        self.addCleanup(setattr, auth, "UPGRADE_CUTOFF", None)
        self.assertFalse(self.steal()[1].friends_ok)
        self.assertTrue(self.hello(KEY)[0].friends_ok)  # raw keys have their own cutoff
        auth.UPGRADE_CUTOFF = int(time.time()) + 3600
        self.assertTrue(self.sign_in()[0].friends_ok)

    def test_no_cutoff_no_end(self):
        self.assertIsNone(auth.upgrade_cutoff())
        self.assertTrue(self.sign_in()[0].friends_ok)
        self.assertEqual(ALL & srv.FEATURE_FRIENDS, srv.FEATURE_FRIENDS)
