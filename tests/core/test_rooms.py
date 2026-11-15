"""The converted worlds' rooms (core/rooms.py): which rooms are a world's, and which world a
player's game is in from its sessions.

Run: python3 -m unittest discover -s tests -t .
"""

import unittest

from forza_live.core import rooms


def world_room(room, key):
    """The launcher's fh2ffworld.world_room (FH1-ReImagined/ForzaHorizon1-ReImagined #832): the
    player's room plus the world's key, in the game's 32 characters."""
    return (room[:31 - len(key)] + "+" + key)[:32]


class WorldKeyTest(unittest.TestCase):
    def test_the_launcher_s_rooms(self):
        for room in ("", "club", "my room", "r" * 40, "+anthem", "a+fh2", "#TU4", "x" * 31):
            for key in ("anthem", "fh2"):
                made = world_room(room, key)
                self.assertLessEqual(len(made), 32)
                self.assertEqual(rooms.world_key(made), key, made)
                # a Title Update 4 game adds "#TU4" after the game's 32 characters
                self.assertEqual(rooms.world_key(made + "#TU4"), key, made)

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
