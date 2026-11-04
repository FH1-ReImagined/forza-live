"""The game link (members.links, members.game): the launcher's signed calls against a game
server's data folder made here (friends.json, players/, banned_xuids.json), and the website's
peek, claim and remove. Every check of a signed request (signature, server name, path, clock,
replay in this worker and in another, unbound and legacy keys, browsers), the start's checks
(ban, signed sign-in, limits), both confirmations, the rules (one key per account, moving, once a
day, unlink and sign out from both sides) and the pinned key (frozen, waiting)."""

import json
import os
import tempfile
import time
from datetime import timedelta
from pathlib import Path
from unittest import mock

from django.conf import settings
from django.core.cache import cache
from django.core.management import call_command
from django.test import Client, override_settings
from django.utils import timezone

from forza_live.core import auth, ed25519
from members import api, links
from members.management.commands import nightly
from members.models import GameLink, LinkRequest, SignedNonce, User
from members.tests.support import HOST, ORIGIN, AccountsTest, Browser, fake_provider

SEED = bytes(range(32))
SEED_B = bytes([0xB0] * 32)
SEED_C = bytes([0xC0] * 32)
XUID = 0x0009000000000001
XUID_B = 0x0009000000000002
START = "/api/game/link/start"


def body(response):
    return json.loads(response.content)


class LinkTest(AccountsTest):
    def setUp(self):
        super().setUp()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.data = Path(tmp.name)
        (self.data / "players").mkdir()
        override = override_settings(GAME_DATA=self.data)
        override.enable()
        self.addCleanup(override.disable)
        self.friends = {}
        self.stamp = 1_000_000_000_000_000_000
        self.player(XUID, SEED, "DriverA")
        self.game_client = Client(HTTP_HOST=HOST)

    # ---- the game server's data folder ----

    def player(self, xuid, seed=None, tag="Driver", signed=True, legacy=None):
        rec = {"name": tag, "seen": time.time(), "key": legacy or "", "friends": {}, "in": {}, "out": {},
               "blocked": {}}
        if seed is not None:
            rec["pub"] = ed25519.public_key(seed).hex()
        self.friends["%016X" % xuid] = rec
        self.write_friends()
        record = {"gamertag": tag, "logons": 1}
        if signed:
            record.update(firstSigned=int(time.time()) - 86400, signedDays=1)
        (self.data / "players" / ("%016X.json" % xuid)).write_text(json.dumps(record))

    def write_friends(self):
        path = self.data / "friends.json"
        path.write_text(json.dumps({"version": 1, "players": self.friends}))
        self.stamp += 1_000_000  # a new mtime every time, however fast
        os.utime(path, ns=(self.stamp, self.stamp))

    def ban(self, *xuids):
        (self.data / "banned_xuids.json").write_text(json.dumps(["%016X" % x for x in xuids]))

    # ---- the launcher ----

    def signed(self, method, path, data=None, *, seed=SEED, xuid=XUID, name=HOST, when=None, nonce=None,
               sign_path=None, sign_body=None, header=None, **extra):
        raw = b"" if data is None else json.dumps(data).encode()
        if header is None and seed is not None:
            header = auth.auth_header(seed, name, xuid, method, sign_path or path,
                                      raw if sign_body is None else sign_body, when=when, nonce=nonce)
        if header is not None:
            extra["HTTP_X_FORZA_AUTH"] = header
        return self.game_client.generic(method, path, raw, content_type="application/json", secure=True, **extra)

    def start(self, **kw):
        r = self.signed("POST", START, {"v": 1}, **kw)
        return r, (body(r) if r["Content-Type"].startswith("application/json") else None)

    def started(self, **kw):
        r, doc = self.start(**kw)
        self.assertEqual(r.status_code, 200, r.content)
        links.StartOut.model_validate(doc)
        return doc

    def poll(self, rid, **kw):
        return self.signed("GET", "/api/game/link/%s" % rid, **kw)

    def confirm(self, rid, account, **kw):
        return self.signed("POST", "/api/game/link/%s/confirm" % rid, {"account": account}, **kw)

    @staticmethod
    def token(doc):
        return doc["url"].split("#link=")[1]

    # ---- the website ----

    def signed_in(self, uid="111", ip="203.0.113.10"):
        b = Browser(ip=ip)
        with fake_provider() as fake:
            self.oauth_sign_in(b, fake, uid=uid)
        return b, User.objects.get(socialaccount__uid=uid)

    def link(self, browser, user, **kw):
        """The whole flow by the browser's token: start, claim, confirm."""
        doc = self.started(**kw)
        r = browser.post_json("/api/link/claim", {"token": self.token(doc)})
        self.assertEqual(r.status_code, 200, r.content)
        r = self.confirm(doc["id"], user.public_id, **{k: v for k, v in kw.items() if k in ("seed", "xuid")})
        self.assertEqual(r.status_code, 200, r.content)
        return doc


class Flow(LinkTest):
    def test_link_by_the_browser_s_token(self):
        b, user = self.signed_in()
        doc = self.started()
        self.assertTrue(doc["url"].startswith(ORIGIN + "/account.html#link="))
        self.assertRegex(doc["code"], r"^[2-9A-HJ-NP-Z]{4}-[2-9A-HJ-NP-Z]{4}$")
        self.assertAlmostEqual(doc["expires"], time.time() + 600, delta=5)
        self.assertEqual(body(self.poll(doc["id"])), {"state": "pending", "account": None, "replaces": None})

        peek = b.post_json("/api/link/peek", {"token": self.token(doc)})
        self.assertEqual(peek.status_code, 200, peek.content)
        links.PeekOut.model_validate(body(peek))
        self.assertEqual((body(peek)["state"], body(peek)["gamertag"], body(peek)["replaces"]),
                         ("pending", "DriverA", None))
        claim = b.post_json("/api/link/claim", {"token": self.token(doc)})
        self.assertEqual(body(claim), {"ok": True, "state": "claimed", "account": user.public_id})

        poll = body(self.poll(doc["id"]))
        links.PollOut.model_validate(poll)
        self.assertEqual(poll["state"], "claimed")
        self.assertEqual(poll["account"], {"id": user.public_id, "method": "discord",
                                           "created": user.date_joined.date().isoformat()})
        r = self.confirm(doc["id"], user.public_id)
        self.assertEqual(body(r), {"ok": True, "state": "linked"})
        self.assertEqual(body(self.poll(doc["id"]))["state"], "linked")
        self.assertEqual(body(b.post_json("/api/link/peek", {"token": self.token(doc)}))["state"], "linked")

        me = body(b.get_json("/api/me"))
        api.MeOut.model_validate(me)
        self.assertEqual(me["link"], {"state": "active", "gamertag": "DriverA",
                                      "since": timezone.now().date().isoformat(),
                                      "counted": False})  # one signed day, a day ago (members.likes)
        self.assertNotIn("0009000000000001", json.dumps(me))  # never the XUID
        self.assertIn("linked the game of DriverA", [e["event"] for e in me["events"]])
        link = GameLink.objects.get()
        self.assertEqual((link.xuid, link.pub, link.user, link.state),
                         ("%016X" % XUID, ed25519.public_key(SEED).hex(), user, GameLink.ACTIVE))
        account = body(self.signed("GET", "/api/game/account"))
        api_doc = links.GameAccountOut.model_validate(account)
        self.assertEqual((api_doc.linked, api_doc.state, api_doc.account.id), (True, "active", user.public_id))

    def test_link_by_the_code_on_another_device(self):
        b, user = self.signed_in()
        doc = self.started()
        typed = doc["code"].lower().replace("-", " ")
        self.assertEqual(body(b.post_json("/api/link/peek", {"code": typed}))["gamertag"], "DriverA")
        self.assertEqual(b.post_json("/api/link/claim", {"code": typed}).status_code, 200)
        self.assertEqual(self.confirm(doc["id"], user.public_id).status_code, 200)
        self.assertEqual(GameLink.objects.get().user, user)

    def test_the_token_and_code_are_kept_only_as_hashes(self):
        doc = self.started()
        row = LinkRequest.objects.get()
        stored = json.dumps([getattr(row, f.attname) for f in LinkRequest._meta.fields], default=str)
        self.assertNotIn(self.token(doc), stored)
        self.assertNotIn(doc["code"].replace("-", ""), stored)

    def test_other_sessions_end_when_the_link_is_made(self):
        b, user = self.signed_in()
        other = Browser(ip="203.0.113.11")
        with fake_provider() as fake:
            self.oauth_sign_in(other, fake, uid="111")
        self.assertEqual(body(b.get_json("/api/me"))["sessions"], 2)
        self.link(b, user)
        self.assertEqual(other.get_json("/api/me").status_code, 401)  # a session from before
        me = b.get_json("/api/me")  # the browser that claimed the link stays signed in
        self.assertEqual(me.status_code, 200)
        self.assertEqual(body(me)["sessions"], 1)

    def test_not_me_declines(self):
        b, user = self.signed_in()
        doc = self.started()
        b.post_json("/api/link/claim", {"token": self.token(doc)})
        r = self.signed("POST", "/api/game/link/%s/cancel" % doc["id"], {})
        self.assertEqual(body(r), {"ok": True, "state": "declined"})
        self.assertEqual(body(b.post_json("/api/link/peek", {"token": self.token(doc)}))["state"], "declined")
        self.assertEqual(self.confirm(doc["id"], user.public_id).status_code, 409)
        self.assertFalse(GameLink.objects.exists())

    def test_a_new_start_ends_the_previous_request(self):
        b, _ = self.signed_in()
        first = self.started()
        second = self.started()
        self.assertEqual(body(self.poll(first["id"]))["state"], "cancelled")
        self.assertEqual(b.post_json("/api/link/peek", {"token": self.token(first)}).status_code, 404)
        self.assertEqual(b.post_json("/api/link/peek", {"token": self.token(second)}).status_code, 200)


class SignedChecks(LinkTest):
    def assertRefused(self, r, status=401):
        self.assertEqual(r.status_code, status, r.content)
        self.assertEqual(body(r), {"error": "refused"})
        self.assertLessEqual(abs(int(r["X-Forza-Time"]) - time.time()), 2)  # for the launcher's clock
        self.assertFalse(LinkRequest.objects.exists())

    def test_a_signed_start_works(self):
        r, _ = self.start()
        self.assertEqual(r.status_code, 200)
        self.assertIn("X-Forza-Time", r)

    def test_unsigned(self):
        self.assertRefused(self.start(seed=None)[0])

    def test_a_wrong_signature(self):
        self.assertRefused(self.start(seed=SEED_B)[0])
        self.assertRefused(self.start(header=auth.auth_header(SEED, HOST, XUID, "POST", START, b"{}")[:-2] + "00")[0])

    def test_a_changed_body_or_path(self):
        self.assertRefused(self.start(sign_body=b'{"v": 2}')[0])
        self.assertRefused(self.start(sign_path="/api/game/unlink")[0])

    def test_another_server_s_name(self):
        self.assertRefused(self.start(name="forza-reimagined.playit.plus")[0])
        self.assertRefused(self.start(name="my.server.example")[0])

    def test_a_replay(self):
        header = auth.auth_header(SEED, HOST, XUID, "POST", START, b'{"v": 1}')
        self.assertEqual(self.start(header=header)[0].status_code, 200)
        r, _ = self.start(header=header)
        self.assertEqual((r.status_code, body(r)), (401, {"error": "refused"}))

    def test_a_replay_in_another_worker(self):
        header = auth.auth_header(SEED, HOST, XUID, "POST", START, b'{"v": 1}')
        self.assertEqual(self.start(header=header)[0].status_code, 200)
        auth._nonces.clear()  # this worker's cache: another worker has none of it
        self.assertEqual(SignedNonce.objects.count(), 1)
        self.assertEqual(self.start(header=header)[0].status_code, 401)
        self.assertEqual(LinkRequest.objects.count(), 1)

    def test_an_expired_or_early_request(self):
        self.assertRefused(self.start(when=time.time() - 200)[0])
        self.assertRefused(self.start(when=time.time() + 200)[0])
        self.assertEqual(self.start(when=time.time() - 100)[0].status_code, 200)

    def test_unbound_and_legacy_keys_look_alike(self):
        stranger = 0x0009000000000099
        self.assertRefused(self.start(xuid=stranger, seed=SEED_C)[0])
        self.player(XUID_B, None, "Legacy", legacy="ab" * 32)  # a raw friends key only
        self.assertRefused(self.start(xuid=XUID_B, seed=SEED_B)[0])

    def test_another_player_s_key(self):
        self.player(XUID_B, SEED_B, "DriverB")
        self.assertRefused(self.start(xuid=XUID_B, seed=SEED)[0])

    def test_browsers_are_refused(self):
        r, _ = self.start(HTTP_ORIGIN=ORIGIN)
        self.assertEqual(r.status_code, 403)
        r, _ = self.start(HTTP_ORIGIN="https://evil.example")
        self.assertEqual(r.status_code, 403)
        b, _ = self.signed_in()
        r = b.post_json(START, {"v": 1})  # a signed-in browser's own request: no signature
        self.assertEqual(r.status_code, 403)
        self.assertFalse(LinkRequest.objects.exists())

    def test_off_without_the_game_server_s_data(self):
        with override_settings(GAME_DATA=None):
            r, doc = self.start()
        self.assertEqual((r.status_code, doc), (503, {"error": "linking is not available"}))

    def test_signed_calls_have_a_rate(self):
        limits = dict(settings.ACCOUNT_RATE_LIMITS, game_call="3/h/key")
        with override_settings(ACCOUNT_RATE_LIMITS=limits):
            rid = self.started()["id"]
            self.assertEqual(self.poll(rid).status_code, 200)
            self.assertEqual(self.poll(rid).status_code, 200)
            self.assertEqual(body(self.poll(rid)), {"error": "too many requests"})


class StartChecks(LinkTest):
    def test_a_banned_xuid(self):
        self.ban(XUID)
        r, doc = self.start()
        self.assertEqual((r.status_code, doc), (403, {"error": "not available"}))

    def test_a_key_that_never_signed_in_to_the_game_server(self):
        self.player(XUID_B, SEED_B, "DriverB", signed=False)
        r, doc = self.start(xuid=XUID_B, seed=SEED_B)
        self.assertEqual((r.status_code, doc), (409, {"error": "play online first"}))

    def test_a_player_without_a_gamertag(self):
        self.player(XUID_B, SEED_B, "")
        r, doc = self.start(xuid=XUID_B, seed=SEED_B)
        self.assertEqual((r.status_code, doc), (409, {"error": "play online first"}))

    def test_five_starts_an_hour(self):
        for _ in range(5):
            self.started()
        r, doc = self.start()
        self.assertEqual((r.status_code, doc), (429, {"error": "too many requests"}))

    def test_a_key_just_bound_is_found(self):
        stranger = 0x0009000000000077
        self.assertEqual(self.start(xuid=stranger, seed=SEED_C)[0].status_code, 401)
        self.player(stranger, SEED_C, "Newcomer")  # friends.json written by the game server
        self.assertEqual(self.start(xuid=stranger, seed=SEED_C)[0].status_code, 200)


class Claims(LinkTest):
    def test_expired_requests(self):
        b, user = self.signed_in()
        doc = self.started()
        LinkRequest.objects.update(expires=timezone.now() - timedelta(seconds=1))
        self.assertEqual(body(self.poll(doc["id"]))["state"], "expired")
        self.assertEqual(b.post_json("/api/link/peek", {"token": self.token(doc)}).status_code, 404)
        self.assertEqual(b.post_json("/api/link/claim", {"token": self.token(doc)}).status_code, 404)

    def test_expired_after_the_claim(self):
        b, user = self.signed_in()
        doc = self.started()
        b.post_json("/api/link/claim", {"token": self.token(doc)})
        LinkRequest.objects.update(expires=timezone.now() - timedelta(seconds=1))
        r = self.confirm(doc["id"], user.public_id)
        self.assertEqual((r.status_code, body(r)), (409, {"error": "not claimed"}))
        self.assertFalse(GameLink.objects.exists())

    def test_a_double_claim(self):
        a, user_a = self.signed_in("111")
        b, _ = self.signed_in("222", ip="203.0.113.20")
        doc = self.started()
        self.assertEqual(a.post_json("/api/link/claim", {"token": self.token(doc)}).status_code, 200)
        for path in ("/api/link/claim", "/api/link/peek"):
            r = b.post_json(path, {"token": self.token(doc)})
            self.assertEqual((r.status_code, body(r)), (409, {"error": "another account claimed this link"}))
        self.assertEqual(body(self.poll(doc["id"]))["account"]["id"], user_a.public_id)
        # claiming it again from the same account changes nothing
        self.assertEqual(a.post_json("/api/link/claim", {"token": self.token(doc)}).status_code, 200)

    def test_a_confirm_naming_another_account(self):
        a, user_a = self.signed_in("111")
        _, user_b = self.signed_in("222", ip="203.0.113.20")
        doc = self.started()
        a.post_json("/api/link/claim", {"token": self.token(doc)})
        r = self.confirm(doc["id"], user_b.public_id)
        self.assertEqual((r.status_code, body(r)), (409, {"error": "the account does not match"}))
        self.assertEqual(body(self.poll(doc["id"]))["state"], "claimed")
        self.assertFalse(GameLink.objects.exists())

    def test_a_confirm_before_the_claim(self):
        _, user = self.signed_in()
        doc = self.started()
        r = self.confirm(doc["id"], user.public_id)
        self.assertEqual((r.status_code, body(r)), (409, {"error": "not claimed"}))

    def test_another_key_cannot_see_or_confirm_the_request(self):
        b, user = self.signed_in()
        self.player(XUID_B, SEED_B, "DriverB")
        doc = self.started()
        b.post_json("/api/link/claim", {"token": self.token(doc)})
        self.assertEqual(self.poll(doc["id"], xuid=XUID_B, seed=SEED_B).status_code, 404)
        self.assertEqual(self.confirm(doc["id"], user.public_id, xuid=XUID_B, seed=SEED_B).status_code, 404)
        self.assertFalse(GameLink.objects.exists())

    def test_the_key_changed_between_start_and_confirm(self):
        b, user = self.signed_in()
        doc = self.started()
        b.post_json("/api/link/claim", {"token": self.token(doc)})
        self.player(XUID, SEED_B, "DriverA")  # the operator re-keyed the XUID meanwhile
        r = self.confirm(doc["id"], user.public_id, seed=SEED_B)
        self.assertEqual((r.status_code, body(r)), (409, {"error": "the key changed"}))

    def test_signed_out_browsers_cannot_peek_or_claim(self):
        doc = self.started()
        for path in ("/api/link/peek", "/api/link/claim"):
            self.assertEqual(Browser().post_json(path, {"token": self.token(doc)}).status_code, 401)

    def test_a_token_or_a_code(self):
        b, _ = self.signed_in()
        doc = self.started()
        for data in ({}, {"token": self.token(doc), "code": doc["code"]}, {"token": "short"}):
            self.assertEqual(b.post_json("/api/link/peek", data).status_code, 422)
        self.assertEqual(body(b.post_json("/api/link/peek", {"code": "not-it"})), {"error": "not a code"})

    def test_tries_per_account(self):
        b, _ = self.signed_in()
        for _ in range(10):
            self.assertEqual(b.post_json("/api/link/peek", {"code": "2222-2222"}).status_code, 404)
        doc = self.started()
        r = b.post_json("/api/link/peek", {"token": self.token(doc)})
        self.assertEqual((r.status_code, body(r)), (429, {"error": "too many tries"}))

    def test_too_many_wrong_codes_stop_codes_but_not_tokens(self):
        limits = dict(settings.ACCOUNT_RATE_LIMITS, link_code_failed="3/m/key")
        browsers = [self.signed_in(str(900 + i), ip="203.0.113.%d" % (40 + i))[0] for i in range(4)]
        doc = self.started()
        with override_settings(ACCOUNT_RATE_LIMITS=limits):
            for b in browsers:
                self.assertEqual(b.post_json("/api/link/peek", {"code": "2222-2222"}).status_code, 404)
            r = browsers[0].post_json("/api/link/peek", {"code": doc["code"]})
            self.assertEqual((r.status_code, body(r)), (429, {"error": "codes are paused"}))
            self.assertEqual(browsers[0].post_json("/api/link/peek", {"token": self.token(doc)}).status_code, 200)


class Rules(LinkTest):
    def test_a_key_already_linked_elsewhere_moves(self):
        a, user_a = self.signed_in("111")
        b, user_b = self.signed_in("222", ip="203.0.113.20")
        self.link(a, user_a)
        cache.clear()  # a day later: the once-a-day limit
        doc = self.started()
        b.post_json("/api/link/claim", {"token": self.token(doc)})
        self.assertEqual(body(self.poll(doc["id"]))["replaces"], user_a.public_id)
        self.assertEqual(self.confirm(doc["id"], user_b.public_id).status_code, 200)
        link = GameLink.objects.get()
        self.assertEqual(link.user, user_b)
        self.assertIsNotNone(link.moved_at)
        self.assertIsNone(body(a.get_json("/api/me"))["link"])
        self.assertIn("the game moved its link to another account",
                      [e["event"] for e in body(a.get_json("/api/me"))["events"]])

    def test_an_account_linked_to_another_key_moves_to_this_one(self):
        b, user = self.signed_in()
        self.player(XUID_B, SEED_B, "DriverB")
        self.link(b, user)
        cache.clear()
        doc = self.started(xuid=XUID_B, seed=SEED_B)
        peek = body(b.post_json("/api/link/peek", {"token": self.token(doc)}))
        self.assertEqual((peek["gamertag"], peek["replaces"]), ("DriverB", "DriverA"))
        b.post_json("/api/link/claim", {"token": self.token(doc)})
        self.assertEqual(self.confirm(doc["id"], user.public_id, xuid=XUID_B, seed=SEED_B).status_code, 200)
        self.assertEqual(list(GameLink.objects.values_list("xuid", flat=True)), ["%016X" % XUID_B])
        self.assertEqual(body(b.get_json("/api/me"))["link"]["gamertag"], "DriverB")
        self.assertEqual(body(self.signed("GET", "/api/game/account")), {"linked": False, "state": None,
                                                                         "account": None})

    def test_once_a_day_per_key(self):
        a, user_a = self.signed_in("111")
        self.link(a, user_a)
        r, doc = self.start()
        self.assertEqual((r.status_code, doc), (429, {"error": "changed in the past day"}))

    def test_once_a_day_per_account(self):
        b, user = self.signed_in()
        self.player(XUID_B, SEED_B, "DriverB")
        self.link(b, user)
        doc = self.started(xuid=XUID_B, seed=SEED_B)
        r = b.post_json("/api/link/claim", {"token": self.token(doc)})
        self.assertEqual((r.status_code, body(r)), (429, {"error": "changed in the past day"}))

    def test_new_links_a_day_in_all(self):
        limits = dict(settings.ACCOUNT_RATE_LIMITS, new_link="1/d/key")
        a, user_a = self.signed_in("111")
        b, user_b = self.signed_in("222", ip="203.0.113.20")
        self.player(XUID_B, SEED_B, "DriverB")
        with override_settings(ACCOUNT_RATE_LIMITS=limits):
            self.link(a, user_a)
            doc = self.started(xuid=XUID_B, seed=SEED_B)
            b.post_json("/api/link/claim", {"token": self.token(doc)})
            r = self.confirm(doc["id"], user_b.public_id, xuid=XUID_B, seed=SEED_B)
            self.assertEqual((r.status_code, body(r)), (429, {"error": "too many links today"}))

    def test_unlink_from_the_website_needs_a_recent_sign_in(self):
        b, user = self.signed_in()
        self.link(b, user)
        with mock.patch("time.time", return_value=time.time() + 601):
            r = b.post_json("/api/link/remove", {})
        self.assertEqual((r.status_code, body(r)), (403, {"error": "sign in again"}))
        self.assertTrue(GameLink.objects.exists())
        self.assertEqual(body(b.post_json("/api/link/remove", {})), {"ok": True, "unlinked": True})
        self.assertIsNone(body(b.get_json("/api/me"))["link"])
        self.assertEqual(body(b.post_json("/api/link/remove", {})), {"ok": True, "unlinked": False})
        self.assertEqual(body(self.signed("GET", "/api/game/account"))["linked"], False)

    def test_unlink_from_the_game(self):
        b, user = self.signed_in()
        self.link(b, user)
        self.assertEqual(body(self.signed("POST", "/api/game/unlink", {})), {"ok": True, "unlinked": True})
        self.assertIsNone(body(b.get_json("/api/me"))["link"])
        self.assertIn("the game unlinked this account", [e["event"] for e in body(b.get_json("/api/me"))["events"]])
        self.assertEqual(body(self.signed("POST", "/api/game/unlink", {})), {"ok": True, "unlinked": False})

    def test_unlink_from_the_game_needs_the_signature(self):
        b, user = self.signed_in()
        self.link(b, user)
        self.assertEqual(self.signed("POST", "/api/game/unlink", {}, seed=SEED_B).status_code, 401)
        self.assertEqual(self.signed("POST", "/api/game/unlink", {}, seed=None).status_code, 401)
        self.assertTrue(GameLink.objects.exists())

    def test_sign_out_everywhere_from_the_game(self):
        b, user = self.signed_in()
        self.link(b, user)
        r = self.signed("POST", "/api/game/signout", {})
        self.assertEqual(body(r), {"ok": True, "ended": 1})
        self.assertEqual(b.get_json("/api/me").status_code, 401)
        self.assertTrue(GameLink.objects.exists())  # the link stays

    def test_sign_out_without_a_link(self):
        r = self.signed("POST", "/api/game/signout", {})
        self.assertEqual((r.status_code, body(r)), (404, {"error": "not linked"}))

    def test_deleting_the_account_removes_the_link(self):
        b, user = self.signed_in()
        self.link(b, user)
        b.post_json("/api/account/delete", {"confirm": user.public_id})
        self.assertFalse(GameLink.objects.exists())
        self.assertEqual(body(self.signed("GET", "/api/game/account"))["linked"], False)


class Pinning(LinkTest):
    def linked(self):
        b, user = self.signed_in()
        self.link(b, user)
        return b, user

    def test_a_changed_key_freezes_the_link(self):
        b, user = self.linked()
        self.player(XUID, SEED_B, "DriverA")  # someone else bound the XUID (or the operator re-keyed it)
        self.assertEqual(body(b.get_json("/api/me"))["link"]["state"], "frozen")
        account = body(self.signed("GET", "/api/game/account", seed=SEED_B))
        self.assertEqual(account, {"linked": True, "state": "frozen", "account": None})  # not to the new key
        r = self.signed("POST", "/api/game/unlink", {}, seed=SEED_B)
        self.assertEqual((r.status_code, body(r)), (409, {"error": "this link needs the operator"}))
        r = self.signed("POST", "/api/game/signout", {}, seed=SEED_B)
        self.assertEqual(r.status_code, 409)
        self.assertEqual(b.get_json("/api/me").status_code, 200)
        link = GameLink.objects.get()
        self.assertEqual((link.state, link.pub), (GameLink.FROZEN, ed25519.public_key(SEED).hex()))
        self.assertIsNotNone(link.frozen_at)

    def test_the_operator_unfreezes_it(self):
        from members.admin import GameLinkAdmin
        from django.contrib import admin
        b, user = self.linked()
        self.player(XUID, SEED_B, "DriverA")
        self.assertEqual(body(b.get_json("/api/me"))["link"]["state"], "frozen")
        with mock.patch.object(GameLinkAdmin, "message_user"):
            GameLinkAdmin(GameLink, admin.site).unfreeze_and_repin(None, GameLink.objects.all())
        self.assertEqual(body(b.get_json("/api/me"))["link"]["state"], "active")
        self.assertEqual(body(self.signed("GET", "/api/game/account", seed=SEED_B))["state"], "active")

    def test_a_forgotten_record_waits_and_comes_back(self):
        b, _ = self.linked()
        saved = self.friends.pop("%016X" % XUID)
        self.write_friends()
        self.assertEqual(body(b.get_json("/api/me"))["link"]["state"], "waiting")
        self.friends["%016X" % XUID] = saved  # the same identity signs in again
        self.write_friends()
        self.assertEqual(body(b.get_json("/api/me"))["link"]["state"], "active")

    def test_a_ban_after_the_link(self):
        b, _ = self.linked()
        self.ban(XUID)
        self.assertEqual(body(b.get_json("/api/me"))["link"]["state"], "banned")

    def test_a_new_gamertag_shows(self):
        b, _ = self.linked()
        self.player(XUID, SEED, "Renamed")
        self.assertEqual(body(b.get_json("/api/me"))["link"]["gamertag"], "Renamed")


class Upkeep(LinkTest):
    def test_nightly_keeps_linked_accounts_and_drops_old_requests(self):
        b, user = self.signed_in()
        self.link(b, user)
        self.signed_in("222", ip="203.0.113.20")
        LinkRequest.objects.update(expires=timezone.now() - timedelta(hours=2))
        later = timezone.now() + timedelta(days=31)
        gone = nightly.purge(later)
        self.assertEqual(gone, 1)  # the unlinked account
        self.assertTrue(User.objects.filter(pk=user.pk).exists())
        self.assertFalse(LinkRequest.objects.exists())
        self.assertFalse(SignedNonce.objects.exists())

    def test_the_migrations_are_complete(self):
        call_command("makemigrations", "members", "--check", "--dry-run", verbosity=0)
