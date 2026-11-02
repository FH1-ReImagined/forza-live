"""The sign-in (Logon2) tells the game the player's car club (#566). Its userData is the game's
copy of the player: the party lobby's [TAG] and INVITE CLUB MEMBERS read the club from it, and
before this the game knew the club only after the car club sign's GetUserData. Drives the
server's handlers directly (no sockets)."""

import struct
import tempfile
import unittest

import forza_clubs as clubs
import forza_live_lsp as lsp
import forza_live_server as srv
import forza_xtsw as x

LEADER, MEMBER, LONER = 0x0009000000000A01, 0x0009000000000B02, 0x0009000000000C03


class LogonClubTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir, lsp.DATA_DIR = lsp.DATA_DIR, self.tmp.name
        self.old_data, clubs.CLUBS.data = clubs.CLUBS.data, None
        clubs.CLUBS.__init__()
        clubs.configure(self.tmp.name, lsp.load_player)
        self.server = srv.ForzaLive("test", 64)
        self.server.transport = type("T", (), {"sendto": lambda *_: None})()
        club = {"id": 7, "tag": "TST", "name": "Test Club", "description": "", "open": True,
                "created": 0, "requests": []}
        d = clubs.CLUBS.load()
        d["clubs"]["7"] = club
        clubs.join(LEADER, club, clubs.LEADER)
        clubs.join(MEMBER, club, clubs.MEMBER)

    def tearDown(self):
        lsp.DATA_DIR = self.old_dir
        clubs.CLUBS.data = self.old_data
        clubs.CLUBS.__init__()
        clubs.configure(self.old_dir, lsp.load_player)
        self.tmp.cleanup()

    def logon(self, xuid, name="Driver"):
        login = {"qwXuid": xuid, "wzGamerTag": name, "currentCareerLevel": 12,
                 "currentBadgeId": 3, "currentPlayerTitleId": 4}
        headers = {"x-classname": "Forza.WebServices.User, Forza.WebServices",
                   "x-method": "Logon2", "x-userid": "0x%x" % xuid}
        reply = lsp.service_call(headers, bytes(x.encode(x.UserLogon2_Params, {"loginData": login})),
                                 self.server)
        self.assertEqual(struct.unpack_from(">I", reply)[0], 0)
        return x.decode(x.UserLogon2_Results, reply, 12)[0]["responseData"]["userData"]

    def test_leader_gets_the_club_at_sign_in(self):
        u = self.logon(LEADER, "Lead")
        self.assertEqual((u["clanId"], u["clanTag"], u["roleInClan"]), (7, "TST", clubs.LEADER))
        self.assertEqual((u["wzGamerTag"], u["currentCareerLevel"]), ("Lead", 12))

    def test_member_gets_the_club_and_their_role(self):
        u = self.logon(MEMBER, "Mem")
        self.assertEqual((u["clanId"], u["clanTag"], u["roleInClan"]), (7, "TST", clubs.MEMBER))

    def test_no_club_no_clan_fields(self):
        u = self.logon(LONER, "Alone")
        self.assertEqual((u["clanId"], u["clanTag"], u["roleInClan"]), (0, "", 0))

    def test_left_club_is_gone_at_the_next_sign_in(self):
        self.assertEqual(self.logon(MEMBER)["clanId"], 7)
        clubs.leave(MEMBER)
        self.assertEqual(self.logon(MEMBER)["clanId"], 0)

    def test_same_club_fields_as_get_user_data(self):
        u, got = self.logon(MEMBER), clubs.user_data(MEMBER)
        for k in ("clanId", "clanTag", "roleInClan"):
            self.assertEqual(u[k], got[k])


if __name__ == "__main__":
    unittest.main()
