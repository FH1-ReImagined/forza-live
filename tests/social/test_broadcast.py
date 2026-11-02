"""Message Center broadcasts: one stored notification shown to every player (/broadcast)."""
import json
import tempfile
import time
import unittest

from forza_live.title_services import lsp
from forza_live.social import notifications as notices
from forza_live.network import xtsw as x

A, B = 0x0009000000000001, 0x0009000000000002


def retrieve(xuid):
    body = x.encode(x.NotificationsRetrieveForUser_Params,
                    {"nonce": 1, "maxResults": 20, "cbParameterBlockSize": 65536})
    reply = notices.retrieve(xuid, bytes(body))
    return notices.pending(xuid), reply


def mark(xuid, nid, clear):
    notices.update(xuid, bytes(x.encode(x.NotificationsId_Params, {"notificationId": nid})), clear)


class BroadcastTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = notices.DATA_DIR
        notices.configure(self.temp.name)
        self.addCleanup(lambda: notices.configure(old))

    def stored(self):
        with open(notices.STORE.path()) as f:
            return json.load(f)

    def test_every_player_gets_it_once_stored(self):
        nid = notices.broadcast(notices.NEWS, {"Message": "Join the Discord"})
        for who in (A, B, 0x0009000000000099):  # a player who never had a notification
            got = notices.pending(who)
            self.assertEqual([(n["id"], n["read"]) for n in got], [(nid, False)])
        self.assertEqual(len(self.stored()["broadcasts"]), 1)
        self.assertEqual(self.stored()["byRecipient"], {})

    def test_read_and_clear_are_per_player(self):
        nid = notices.broadcast(notices.NEWS, {"Message": "hello"})
        mark(A, nid, False)
        self.assertTrue(notices.pending(A)[0]["read"])
        self.assertFalse(notices.pending(B)[0]["read"])
        mark(A, nid, True)
        self.assertEqual(notices.pending(A), [])
        self.assertEqual([n["id"] for n in notices.pending(B)], [nid])
        mark(A, nid, True)  # clearing twice is harmless
        self.assertEqual(self.stored()["broadcastState"]["%016X" % A]["cleared"], [nid])

    def test_personal_notifications_still_work_alongside(self):
        mine = notices.send(A, notices.NEWS, {"Message": "just you"})
        nid = notices.broadcast(notices.NEWS, {"Message": "everyone"})
        self.assertEqual([n["id"] for n in notices.pending(A)], [mine, nid])
        self.assertEqual([n["id"] for n in notices.pending(B)], [nid])
        mark(A, mine, True)
        self.assertEqual([n["id"] for n in notices.pending(A)], [nid])

    def test_retrieve_encodes_broadcasts(self):
        nid = notices.broadcast(notices.NEWS, {"Message": "everyone"})
        got, reply = retrieve(B)
        self.assertEqual([n["id"] for n in got], [nid])
        self.assertIn(b"everyone", bytes(reply))

    def test_expired_broadcasts_go_and_their_state_is_forgotten(self):
        old = notices.broadcast(notices.NEWS, {"Message": "old"}, keep_days=1)
        mark(A, old, True)
        d = notices.STORE.load()
        d["broadcasts"][0]["expires"] = time.time() - 1
        self.assertEqual(notices.pending(B), [])
        new = notices.broadcast(notices.NEWS, {"Message": "new"})
        self.assertEqual([b["id"] for b in self.stored()["broadcasts"]], [new])
        self.assertNotIn("%016X" % A, self.stored().get("broadcastState", {}))

    def test_bad_type_refused(self):
        with self.assertRaises(ValueError):
            notices.broadcast(0x7777, {})


class BroadcastEndpointTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = notices.DATA_DIR
        notices.configure(self.temp.name)
        self.addCleanup(lambda: notices.configure(old))

    def call(self, query):
        return lsp.broadcast_request({k: [v] for k, v in query.items()})

    def test_news_broadcast(self):
        code, _, body = self.call({"type": "0x100", "Message": "Join us", "days": "7"})
        self.assertEqual(code, 200)
        nid = json.loads(body)["id"]
        got = notices.pending(A)
        self.assertEqual([(n["id"], n["params"]) for n in got], [(nid, {"Message": "Join us"})])
        self.assertLess(got[0]["expires"], time.time() + 7 * 86400 + 5)

    def test_bad_requests(self):
        for q in ({"type": "nope"}, {"type": "0x7777"}, {"type": "0x100", "days": "0"},
                  {"type": "0x100", "days": "31"}, {}):
            self.assertEqual(self.call(q)[0], 400, q)
        self.assertEqual(notices.pending(A), [])

    def test_operator_only(self):
        # Not from this machine, no token: refused before anything is queued.
        for peer, headers in ((("203.0.113.5", 1), {}),
                              (("127.0.0.1", 1), {"x-forwarded-for": "203.0.113.5"})):
            code = lsp.handle(None, "GET", "/broadcast?type=0x100&Message=x", headers, b"", peer)[0]
            self.assertEqual(code, 403)
        code = lsp.handle(None, "GET", "/broadcast?type=0x100&Message=x", {}, b"",
                          ("127.0.0.1", 1))[0]
        self.assertEqual(code, 200)
        notices.STORE.load()["broadcasts"] = []
        self.assertEqual(notices.pending(A), [])


if __name__ == "__main__":
    unittest.main()
