"""The converted worlds' rooms (core/rooms.py): which rooms are a world's, and which world a
player's game is in from its sessions.

Run: python3 -m unittest discover -s tests -t .
"""

import unittest

from forza_live.core import rooms


def world_room(room, key):
    """The launcher's fh2ffworld.world_room (FH1-ReImagined/ForzaHorizon1-ReImagined #832): the
    player's room plus the world's key, cut to 32 characters."""
    return (room[:31 - len(key)] + "+" + key)[:32]


def world_room_in_bytes(room, key):
    """world_room cut to the game's 32 BYTES: what keeps the ending whole for a room of letters
    that take more than one byte in UTF-8 (the launcher has to cut it this way)."""
    return room.encode("utf-8")[:31 - len(key)].decode("utf-8", "ignore") + "+" + key


def game_room(env_room, tu4=False):
    """The room the server sees: the game keeps the first 32 bytes of FORZA_ROOM (forza-recomp
    kernel/identity.cpp, LiveRoom), a Title Update 4 game adds "#TU4", and the server reads the
    HELLO's room as UTF-8 (server.py)."""
    raw = env_room.encode("utf-8")[:32] + (b"#TU4" if tu4 else b"")
    return raw.decode("utf-8", "replace")


# rooms with letters that take two or three bytes in UTF-8, short and long
WIDE_ROOMS = ("Équipe", "Équipe de France Horizon", "Ñandú Racing Club 2026 !!",
              "x" * 24 + "é", "é" * 20, "クラブ" * 4, "クラブ")


class WorldKeyTest(unittest.TestCase):
    def test_the_launcher_s_rooms(self):
        for room in ("", "club", "my room", "r" * 40, "+anthem", "a+fh2", "#TU4", "x" * 31):
            for key in ("anthem", "fh2"):
                made = world_room(room, key)
                self.assertLessEqual(len(made), 32)
                self.assertEqual(rooms.world_key(made), key, made)
                for tu4 in (False, True):  # a Title Update 4 game adds "#TU4" after the 32 bytes
                    self.assertEqual(rooms.world_key(game_room(made, tu4)), key, made)

    def test_rooms_of_wider_letters_keep_their_ending_only_when_cut_in_bytes(self):
        """The game keeps 32 bytes, not 32 letters. A room cut to 32 letters that is longer than
        32 bytes loses its ending in the game, and the server takes it as Colorado's room: the
        launcher has to cut the room in bytes for those."""
        lost = []
        for room in WIDE_ROOMS:
            for key in ("anthem", "fh2"):
                made = world_room_in_bytes(room, key)
                self.assertLessEqual(len(made.encode("utf-8")), 32)
                for tu4 in (False, True):
                    self.assertEqual(rooms.world_key(game_room(made, tu4)), key, (room, key))
                if rooms.world_key(game_room(world_room(room, key), True)) != key:
                    lost.append((room, key))
        # cut in letters (#832 as it is): these lose the ending, so their world counts as Colorado
        self.assertEqual(lost, [("Ñandú Racing Club 2026 !!", "anthem"), ("x" * 24 + "é", "anthem"),
                                ("é" * 20, "anthem"), ("é" * 20, "fh2"), ("クラブ" * 4, "anthem"),
                                ("クラブ" * 4, "fh2")])

    def test_colorado_s_rooms(self):
        for room in ("", "#TU4", "club", "club#TU4", "anthem", "fh2", "+fh", "myfh2", "my-anthem",
                     "club+FH2", "Club+Anthem#TU4", "+anthem2", "+anthem ", "+fh2#tu4",
                     "+anthem+club", "+fh2#TU4#TU4", "probe", "+anthem#TU5"):
            self.assertIsNone(rooms.world_key(room), room)

    def test_not_a_room(self):
        for room in (None, b"+fh2", 7):
            self.assertIsNone(rooms.world_key(room))

    def test_a_room_typed_to_end_that_way_is_a_world_s(self):
        """The server cannot tell it from the launcher's: such a room meets the world's players."""
        self.assertEqual(rooms.world_key("club+fh2"), "fh2")


class WorldOfSessionsTest(unittest.TestCase):
    def test_no_session(self):
        self.assertIsNone(rooms.world_of_sessions([]))

    def test_colorado(self):
        self.assertIsNone(rooms.world_of_sessions([("#TU4", 1, 5.0)]))

    def test_a_world(self):
        self.assertEqual(rooms.world_of_sessions([("+anthem#TU4", 1, 5.0)]),
                         ("anthem", 1, "+anthem#TU4"))

    def test_a_world_and_colorado_at_once_is_the_world(self):
        self.assertEqual(rooms.world_of_sessions([("#TU4", 1, 9.0), ("+fh2#TU4", 2, 5.0)])[0], "fh2")

    def test_two_worlds_the_newest_heard(self):
        self.assertEqual(rooms.world_of_sessions([("+fh2", 1, 9.0), ("+anthem", 2, 5.0)])[:2],
                         ("fh2", 1))
        self.assertEqual(rooms.world_of_sessions([("+fh2", 1, 1.0), ("+anthem", 2, 5.0)])[:2],
                         ("anthem", 2))

    def test_a_world_session_that_left(self):
        left = ("anthem", 3, "+anthem", 100.0)
        self.assertEqual(rooms.world_of_sessions([], left, now=100.0 + rooms.LEFT_KEEP - 1),
                         ("anthem", 3, "+anthem"))
        self.assertIsNone(rooms.world_of_sessions([], left, now=100.0 + rooms.LEFT_KEEP))
        # a live session decides
        self.assertIsNone(rooms.world_of_sessions([("#TU4", 4, 101.0)], left, now=101.0))


class LeftOutTest(unittest.TestCase):
    def setUp(self):
        rooms._told.clear()
        self.addCleanup(rooms._told.clear)

    def test_once_per_session_and_kind(self):
        found = ("fh2", 7, "+fh2#TU4")
        with self.assertLogs("forza-live", "INFO") as logs:
            for _ in range(3):
                rooms.left_out("scores", "Scoreboard.SubmitScore", found, 0x0009000000000001)
            rooms.left_out("race stats", "User.WriteRaceStatsEx", found, 0x0009000000000001)
            rooms.left_out("scores", "Scoreboard.SubmitScore", ("fh2", 8, "+fh2#TU4"),
                           0x0009000000000001)
        self.assertEqual(len(logs.output), 3)
        self.assertIn("0009000000000001 in room '+fh2#TU4' (Forza Horizon 2's world): not kept, "
                      "as Colorado's: scores (first: Scoreboard.SubmitScore; told once per "
                      "session)", logs.output[0])

    def test_the_call_s_world(self):
        self.assertIsNone(rooms.call_world())
        rooms.left_out("scores", "nothing")   # outside a world's call: nothing to say
        token = rooms.set_call(("anthem", 1, "+anthem"), 5)
        try:
            self.assertEqual(rooms.call_world(), "anthem")
            with self.assertLogs("forza-live", "INFO") as logs:
                rooms.left_out("challenge progress", "a race step")
            self.assertIn("0000000000000005 in room '+anthem'", logs.output[0])
        finally:
            rooms.reset_call(token)
        self.assertIsNone(rooms.call_world())
        token = rooms.set_call(None, 5)
        self.assertIsNone(rooms.call_world())
        rooms.reset_call(token)


if __name__ == "__main__":
    unittest.main()
