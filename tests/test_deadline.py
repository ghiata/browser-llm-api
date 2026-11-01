"""
Unit tests for the completion deadline in ``_stream_completion`` — no browser.

GPT-6 at High can think for more than seven minutes with no answer text growing
and no WebSocket frames while the page shows it is still working. The loop used
to give up at 420 s in that phase and return a partial or empty answer. It now
waits while the page says it is generating (up to ``_THINKING_DEADLINE``), and a
page that stopped working still gives up near ``_BASE_DEADLINE``.
"""
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_llm import server  # noqa: E402

NO_IMG = {"loaded": 0, "pending": 0, "creating": False}


class FakeProvider:
    """Answers each poll from ``script(now) -> (text, generating, img)``."""
    name = "fake"
    buffered_stream = True

    def __init__(self, clock, script):
        self.clock = clock
        self.script = script

    async def get_response_text(self, page):
        return self.script(self.clock[0])[0]

    async def is_generating(self, page):
        return self.script(self.clock[0])[1]

    async def image_status(self, page):
        return self.script(self.clock[0])[2]

    async def has_user_turn(self, page):
        return True

    async def page_visibility(self, page):
        return "visible"


class FakeMonitor:
    stream_done = types.SimpleNamespace(is_set=lambda: False)

    def seconds_since_ws_frame(self, now):
        return None


async def _drive(script):
    """Run the loop on a fake clock; return (answer, seconds the request took)."""
    clock = [0.0]

    async def sleep(seconds):
        clock[0] += seconds

    fake_time = types.SimpleNamespace(monotonic=lambda: clock[0])
    fake_asyncio = types.SimpleNamespace(sleep=sleep)
    out = []
    with mock.patch.object(server, "time", fake_time), \
            mock.patch.object(server, "asyncio", fake_asyncio):
        async for delta in server._stream_completion(FakeProvider(clock, script), None,
                                                     FakeMonitor()):
            out.append(delta)
    return "".join(out), clock[0]


class ThinkingDeadline(unittest.IsolatedAsyncioTestCase):

    async def test_long_silent_thinking_phase_then_answer(self):
        # Ten minutes of thinking: a short status line that never grows, the
        # page still generating. Then the answer arrives and generation ends.
        answer = "Here are the eight talking points."

        def script(now):
            if now < 600:
                return ("Thought for a while", True, NO_IMG)
            return (answer, False, NO_IMG)

        text, took = await _drive(script)
        self.assertEqual(text, answer)
        self.assertGreater(took, 600)

    async def test_generation_stuck_forever_gives_up_at_the_thinking_cap(self):
        text, took = await _drive(lambda now: ("Thinking", True, NO_IMG))
        self.assertGreaterEqual(took, server._THINKING_DEADLINE)
        self.assertLess(took, server._THINKING_DEADLINE + 5)

    async def test_a_stalled_page_still_gives_up_near_the_base_deadline(self):
        # Not generating, nothing growing, an image that never renders: the
        # tracker never settles, and nothing says the page is still working.
        stuck = {"loaded": 0, "pending": 1, "creating": False}
        text, took = await _drive(lambda now: ("", False, stuck))
        self.assertGreaterEqual(took, server._BASE_DEADLINE)
        self.assertLess(took, server._BASE_DEADLINE + 5)

    async def test_a_normal_answer_is_unaffected(self):
        def script(now):
            return ("OK", now < 5, NO_IMG) if now >= 2 else ("", True, NO_IMG)

        text, took = await _drive(script)
        self.assertEqual(text, "OK")
        self.assertLess(took, 15)


class ExtendedDeadline(unittest.TestCase):

    def test_streaming_extends_up_to_the_streaming_cap(self):
        d = server._extended_deadline(0, server._MAX_DEADLINE, streaming=True, generating=False)
        self.assertIsNone(d)
        d = server._extended_deadline(0, 420, streaming=True, generating=False)
        self.assertEqual(d, 540)

    def test_generating_extends_up_to_the_thinking_cap(self):
        d = server._extended_deadline(0, server._MAX_DEADLINE, streaming=False, generating=True)
        self.assertEqual(d, min(server._THINKING_DEADLINE, server._MAX_DEADLINE + 120))
        self.assertIsNone(server._extended_deadline(0, server._THINKING_DEADLINE,
                                                    streaming=False, generating=True))

    def test_neither_gives_up(self):
        self.assertIsNone(server._extended_deadline(0, 420, streaming=False, generating=False))


if __name__ == "__main__":
    unittest.main()
