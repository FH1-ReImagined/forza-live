"""support.loop_steps_under: a step of the event loop that uses CPU is flagged, one that only
waits is not, and the patch is gone afterwards."""

import asyncio
import time
import unittest

from tests.support import loop_steps_under


def spin(seconds):
    end = time.thread_time() + seconds
    while time.thread_time() < end:
        pass


class LoopStepsUnderTest(unittest.IsolatedAsyncioTestCase):
    async def test_a_step_that_uses_cpu_is_flagged(self):
        real = asyncio.events.Handle._run
        with self.assertRaisesRegex(AssertionError, "steps of the event loop over"):
            with loop_steps_under(0.05):
                asyncio.get_running_loop().call_soon(spin, 0.1)  # a step that starts inside
                await asyncio.sleep(0.01)
        self.assertIs(asyncio.events.Handle._run, real)

    async def test_a_step_that_waits_is_not(self):
        with loop_steps_under(0.05):
            asyncio.get_running_loop().call_soon(time.sleep, 0.15)  # wall time, no CPU: a
            await asyncio.sleep(0.01)                                # descheduled process

    async def test_nested_use_and_the_patch_is_restored(self):
        real = asyncio.events.Handle._run
        with loop_steps_under(1.0):
            with loop_steps_under(1.0):
                await asyncio.sleep(0)
        self.assertIs(asyncio.events.Handle._run, real)


if __name__ == "__main__":
    unittest.main()
