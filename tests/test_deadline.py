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


async def _drive(script, timeout=None):
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
                                                     FakeMonitor(), timeout=timeout):
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


class RequestTimeout(unittest.IsolatedAsyncioTestCase):
    """A caller's own ``timeout`` (chatgpt_proof.py --timeout) sets the cap."""

    async def test_a_longer_timeout_waits_past_the_server_default(self):
        # 30 minutes of thinking, then the answer: past the 1500 s default,
        # inside the caller's 2400 s.
        answer = "The rows, rewritten."

        def script(now):
            return ("Thinking", True, NO_IMG) if now < 1800 else (answer, False, NO_IMG)

        text, took = await _drive(script, timeout=2400)
        self.assertEqual(text, answer)
        self.assertGreater(took, 1800)
        # Without the caller's timeout the same request gives up at the default.
        text, took = await _drive(script)
        self.assertNotEqual(text, answer)
        self.assertLess(took, server._THINKING_DEADLINE + 5)

    async def test_stuck_generation_gives_up_at_the_callers_timeout(self):
        text, took = await _drive(lambda now: ("Thinking", True, NO_IMG), timeout=2000)
        self.assertGreaterEqual(took, 2000)
        self.assertLess(took, 2005)

    async def test_a_shorter_timeout_ends_sooner(self):
        text, took = await _drive(lambda now: ("Thinking", True, NO_IMG), timeout=300)
        self.assertGreaterEqual(took, 300)
        self.assertLess(took, 305)

    async def test_a_short_timeout_never_cuts_a_streaming_answer(self):
        # Text grows every poll until 700 s: a 300 s timeout must not cut it.
        def script(now):
            if now < 700:
                return ("x" * (int(now) + 1), True, NO_IMG)
            return ("x" * 701 + " end", False, NO_IMG)

        text, took = await _drive(script, timeout=300)
        self.assertTrue(text.endswith(" end"))
        self.assertGreater(took, 700)

    async def test_a_stalled_page_still_gives_up_near_the_base_deadline(self):
        stuck = {"loaded": 0, "pending": 1, "creating": False}
        text, took = await _drive(lambda now: ("", False, stuck), timeout=3000)
        self.assertGreaterEqual(took, server._BASE_DEADLINE)
        self.assertLess(took, server._BASE_DEADLINE + 5)


class RequestCaps(unittest.TestCase):

    def test_unset_keeps_the_defaults(self):
        self.assertEqual(server._request_caps(None),
                         (server._BASE_DEADLINE, server._MAX_DEADLINE, server._THINKING_DEADLINE))
        self.assertEqual(server._request_caps(float("nan")), server._request_caps(None))
        self.assertEqual(server._request_caps(float("inf")), server._request_caps(None))

    def test_clamped_to_the_allowed_range(self):
        self.assertEqual(server._request_caps(1)[2], server._REQUEST_TIMEOUT_MIN)
        self.assertEqual(server._request_caps(-5)[2], server._REQUEST_TIMEOUT_MIN)
        self.assertEqual(server._request_caps(10 ** 9)[2], server._REQUEST_TIMEOUT_MAX)

    def test_a_short_timeout_bounds_thinking_not_streaming(self):
        self.assertEqual(server._request_caps(100), (100, server._MAX_DEADLINE, 100))
        # Silent but generating: gives up at the caller's 100 s.
        self.assertIsNone(server._extended_deadline(0, 100, streaming=False, generating=True,
                                                    stream_cap=server._MAX_DEADLINE, think_cap=100))
        # Text still arriving: carries on to the streaming cap.
        self.assertEqual(server._extended_deadline(0, 100, streaming=True, generating=True,
                                                   stream_cap=server._MAX_DEADLINE, think_cap=100), 220)

    def test_the_request_model_takes_the_field(self):
        req = server.ChatCompletionRequest(messages=[{"role": "user", "content": "hi"}],
                                           timeout=1500)
        self.assertEqual(req.timeout, 1500)
        self.assertIsNone(server.ChatCompletionRequest(messages=[]).timeout)


if __name__ == "__main__":
    unittest.main()
