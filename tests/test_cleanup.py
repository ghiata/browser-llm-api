"""
The hourly account cleanup: the ledger of conversations this server made
(ledger.py), how a drive writes into it, POST /api/conversations/cleanup and
the ``browser-llm cleanup`` command the timer runs. No browser, no network.

Deleting a conversation on the site cannot be undone, so the rules pinned here
are the ones that keep a person's own chats safe: only ids in the ledger are
deleted, never a young one, never one a drive is still in, never a dashboard
chat, never while a request is waiting, and never the saved images.
"""
import asyncio
import contextlib
import io
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import HTTPException  # noqa: E402
from starlette.requests import Request  # noqa: E402

from browser_llm import authz, cli, guests, lanes, ledger, providers, server  # noqa: E402
from browser_llm.providers.chatgpt import ChatGPTProvider  # noqa: E402

NAME = "chatgpt-browser"


def _id(n: int) -> str:
    return f"{n:08x}-0000-4000-8000-000000000000"


def req(origin=None, host="localhost:8081", path="/api/conversations/cleanup"):
    headers = [(b"host", host.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    return Request({"type": "http", "method": "POST", "path": path,
                    "headers": headers, "query_string": b""})


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.path = Path(self.dir.name) / "state" / "conversations.json"
        self.clock = Clock()
        self.ledger = ledger.ConversationLedger(self.path, clock=self.clock)

    def test_due_is_only_what_is_old_enough_oldest_first(self):
        self.ledger.note(_id(1), provider=NAME)
        self.clock.t += 100
        self.ledger.note(_id(2), provider=NAME, kind="image")
        self.clock.t += 3600
        self.ledger.note(_id(3), provider=NAME)
        self.assertEqual(self.ledger.due(3600), [_id(1), _id(2)])
        self.assertEqual(self.ledger.due(3700), [_id(1)])
        self.assertEqual(self.ledger.due(0), [_id(1), _id(2), _id(3)])

    def test_due_filters_by_provider(self):
        self.ledger.note(_id(1), provider=NAME)
        self.ledger.note(_id(2), provider="gemini-browser")
        self.assertEqual(self.ledger.due(0, NAME), [_id(1)])

    def test_noting_twice_keeps_the_first_time(self):
        self.ledger.note(_id(1), provider=NAME)
        self.clock.t += 3000
        self.ledger.note(_id(1), provider=NAME)
        self.clock.t += 700
        self.assertEqual(self.ledger.due(3600), [_id(1)])

    def test_empty_id_is_ignored(self):
        self.ledger.note("", provider=NAME)
        self.ledger.note("   ", provider=NAME)
        self.assertEqual(len(self.ledger), 0)
        self.assertFalse(self.path.exists())

    def test_survives_a_restart_and_is_private(self):
        self.ledger.note(_id(1), provider=NAME)
        again = ledger.ConversationLedger(self.path, clock=self.clock)
        self.assertIn(_id(1), again)
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_forget(self):
        self.ledger.note(_id(1), provider=NAME)
        self.ledger.note(_id(2), provider=NAME)
        self.assertEqual(self.ledger.forget([_id(1), _id(9)]), 1)
        again = ledger.ConversationLedger(self.path, clock=self.clock)
        self.assertNotIn(_id(1), again)
        self.assertIn(_id(2), again)

    def test_a_delete_that_keeps_failing_is_given_up_on(self):
        self.ledger.note(_id(1), provider=NAME)
        for _ in range(ledger.MAX_ATTEMPTS - 1):
            self.assertEqual(self.ledger.failed([_id(1)]), [])
        self.assertIn(_id(1), self.ledger)
        self.assertEqual(self.ledger.failed([_id(1)]), [_id(1)])
        self.assertNotIn(_id(1), self.ledger)

    def test_a_damaged_file_starts_empty_instead_of_failing(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        damaged = ledger.ConversationLedger(self.path, clock=self.clock)
        with self.assertLogs("browser_llm.ledger", "WARNING"):
            self.assertEqual(damaged.due(0), [])
        damaged.note(_id(1), provider=NAME)
        self.assertIn(_id(1), ledger.ConversationLedger(self.path))


class TryAcquireTest(unittest.IsolatedAsyncioTestCase):
    async def test_takes_a_free_lane_now_or_none(self):
        pool = lanes.LanePool(2)
        self.assertEqual(pool.try_acquire(), 0)          # lane 0 may always start
        self.assertIsNone(pool.try_acquire())            # lane 1 is cold
        pool.set_warm(1, True)
        self.assertEqual(pool.try_acquire(), 1)
        self.assertIsNone(pool.try_acquire())            # all busy
        pool.release(0)
        pool.release(1)
        self.assertEqual(pool.busy_count(), 0)

    async def test_a_waiting_request_comes_first(self):
        pool = lanes.LanePool(2)
        pool.set_warm(1, True)
        first = await pool.acquire()
        second = await pool.acquire()
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        self.assertEqual(pool.waiting(), 1)
        self.assertIsNone(pool.try_acquire())
        pool.release(first)                              # goes to the waiter
        self.assertEqual(await waiter, first)
        pool.release(first)
        pool.release(second)


class TrackedTest(unittest.TestCase):
    """Which requests' conversations go in the ledger."""

    def test_the_dashboard_is_a_person(self):
        self.assertFalse(server._tracked(req("http://localhost:8081")))
        self.assertFalse(server._tracked(req("http://192.168.1.20:8081", "192.168.1.20:8081")))

    def test_tools_and_other_pages_are_tracked(self):
        self.assertTrue(server._tracked(req()))                          # curl, MCP, scripts
        self.assertTrue(server._tracked(req("https://site.example")))     # the widget elsewhere
        self.assertTrue(server._tracked(req("null")))
        self.assertTrue(server._tracked(None))

    def test_a_guest_is_tracked_even_from_the_dashboard(self):
        g = guests.Guest("pat", "k" * 24, 5)
        self.assertTrue(server._tracked(req("http://localhost:8081"), g))

    def test_guests_cannot_reach_the_cleanup(self):
        self.assertFalse(authz.guest_may("/api/conversations/cleanup", "POST"))
        self.assertTrue(authz.needs_key("/api/conversations/cleanup", "POST"))


# ---------------------------------------------------------------------------
# A drive writing into the ledger
# ---------------------------------------------------------------------------
class StreamCallbackTest(unittest.IsolatedAsyncioTestCase):
    """_stream_completion reports the conversation as soon as the URL holds it."""

    async def test_reported_once_as_soon_as_it_appears(self):
        clock = [0.0]
        reads = []

        class P:
            name = "fake"
            buffered_stream = True

            async def get_response_text(self, page):
                return "answer" if clock[0] > 6 else ""

            async def is_generating(self, page):
                return clock[0] <= 6

            async def image_status(self, page):
                return {"loaded": 0, "pending": 0, "creating": False}

            async def has_user_turn(self, page):
                return True

            async def conversation_id(self, page):
                reads.append(clock[0])
                return _id(7) if clock[0] > 3 else ""

        class M:
            stream_done = types.SimpleNamespace(is_set=lambda: False)

            def seconds_since_ws_frame(self, now):
                return None

        async def sleep(s):
            clock[0] += s

        seen = []
        with mock.patch.object(server, "time", types.SimpleNamespace(monotonic=lambda: clock[0])), \
                mock.patch.object(server, "asyncio", types.SimpleNamespace(sleep=sleep)):
            out = [d async for d in server._stream_completion(P(), None, M(),
                                                              on_conversation=seen.append)]
        self.assertEqual("".join(out), "answer")
        self.assertEqual(seen, [_id(7)])
        # Read every 2 s at most until found, then never again.
        self.assertTrue(all(b - a >= 2.0 for a, b in zip(reads, reads[1:])), reads)
        self.assertLessEqual(reads[-1], 5.0)


class FakeDriveProvider:
    name = NAME
    supports_history = True

    def __init__(self, discard_ok=True):
        self.discard_ok = discard_ok
        self.discarded = 0

    async def open_and_send(self, browser, prompt, attachments=None, options=None):
        return object(), object()

    async def answered_model(self, page):
        return {}

    async def conversation_id(self, page):
        return _id(5)

    async def get_images(self, page):
        return []

    async def discard_conversation(self, page):
        self.discarded += 1
        return self.discard_ok


class DriveLedgerTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.ledger = ledger.ConversationLedger(Path(self.dir.name) / "c.json")
        self.fail_after_id = False
        self.live_during: list = []

        async def fake_stream(provider, page, monitor, timeout=None, on_conversation=None):
            on_conversation(_id(5))
            self.live_during.append(_id(5) in server._live_conversations)
            if self.fail_after_id:
                raise RuntimeError("the page went away")
            yield "ok"

        async def fake_browser(p):
            return object()

        for p in (mock.patch.object(server, "LEDGER", self.ledger),
                  mock.patch.object(server, "_stream_completion", fake_stream),
                  mock.patch.object(server, "get_browser", fake_browser),
                  mock.patch.object(server, "_live_conversations", set())):
            p.start()
            self.addCleanup(p.stop)

    async def test_a_tool_chat_is_written_down(self):
        text, _ = await server.drive_once(FakeDriveProvider(), "hi")
        self.assertEqual(text, "ok")
        self.assertIn(_id(5), self.ledger)
        self.assertEqual(self.live_during, [True])
        self.assertEqual(server._live_conversations, set())

    async def test_the_streaming_drive_too(self):
        out = [d async for d in server.run_chat(FakeDriveProvider(), "hi")]
        self.assertEqual(out, ["ok"])
        self.assertIn(_id(5), self.ledger)
        self.assertEqual(server._live_conversations, set())

    async def test_a_dashboard_chat_is_not(self):
        await server.drive_once(FakeDriveProvider(), "hi", track=False)
        self.assertEqual(len(self.ledger), 0)

    async def test_an_ephemeral_delete_takes_it_off(self):
        p = FakeDriveProvider()
        await server.drive_once(p, "hi", ephemeral=True)
        self.assertEqual(p.discarded, 1)
        self.assertNotIn(_id(5), self.ledger)

    async def test_a_failed_ephemeral_delete_leaves_it_for_the_cleanup(self):
        p = FakeDriveProvider(discard_ok=False)
        await server.drive_once(p, "hi", ephemeral=True)
        self.assertIn(_id(5), self.ledger)

    async def test_a_failed_drive_still_leaves_it_written_down(self):
        self.fail_after_id = True
        with mock.patch.object(server, "_evict_dead_browser", mock.AsyncMock()):
            with self.assertRaises(RuntimeError):
                await server.drive_once(FakeDriveProvider(), "hi")
        self.assertIn(_id(5), self.ledger)
        self.assertEqual(server._live_conversations, set())


# ---------------------------------------------------------------------------
# POST /api/conversations/cleanup
# ---------------------------------------------------------------------------
class FakeBrowser:
    stopped = False

    def stop(self):
        self.stopped = True


class CleanupEndpointTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.clock = Clock()
        self.ledger = ledger.ConversationLedger(Path(self.dir.name) / "c.json", clock=self.clock)
        self.group = providers._build_lanes(3)[NAME]
        self.pool = lanes.LanePool(3)
        self.calls: list[list[str]] = []
        self.answer = None          # (done, failed) override
        self.raise_on_delete = None
        self.lanes_used: list[int] = []

        async def fake_get_browser(lp):
            self.lanes_used.append(lp.lane)
            server._browsers.setdefault(lp.key, FakeBrowser())
            return server._browsers[lp.key]

        @contextlib.asynccontextmanager
        async def fake_session_tab(prov, browser):
            yield object()

        async def fake_delete(prov, page, ids):
            self.calls.append(list(ids))
            if self.raise_on_delete:
                raise self.raise_on_delete
            if self.answer is not None:
                return self.answer
            return list(ids), {}

        def no_images(*a, **k):
            raise AssertionError("the cleanup must not touch saved images")

        for p in (mock.patch.dict(server.LANES, {NAME: self.group}),
                  mock.patch.dict(server._pools, {NAME: self.pool}),
                  mock.patch.dict(server._browsers, {}, clear=True),
                  mock.patch.object(server, "_verified_lanes", {lp.key for lp in self.group}),
                  mock.patch.object(server, "get_browser", fake_get_browser),
                  mock.patch.object(server, "LEDGER", self.ledger),
                  mock.patch.object(server, "_live_conversations", set()),
                  mock.patch.object(server, "_conversation_images", no_images),
                  mock.patch.object(server, "_unlink_saved", no_images),
                  mock.patch.object(ChatGPTProvider, "session_tab", fake_session_tab),
                  mock.patch.object(ChatGPTProvider, "delete_conversations", fake_delete)):
            p.start()
            self.addCleanup(p.stop)

    def note(self, n, age):
        self.clock.t -= age
        self.ledger.note(_id(n), provider=NAME)
        self.clock.t += age

    async def call(self, origin=None, **body):
        return await server.api_conversations_cleanup(
            server.ConversationCleanupRequest(**body), req(origin))

    async def test_deletes_only_recorded_conversations_an_hour_old(self):
        self.note(1, 7200)
        self.note(2, 3700)
        self.note(3, 600)                    # a job may still continue it
        out = await self.call()
        self.assertEqual(self.calls, [[_id(1), _id(2)]])
        self.assertEqual(out["deleted"], [_id(1), _id(2)])
        self.assertEqual(self.ledger.due(0), [_id(3)])
        self.assertEqual(self.lanes_used, [0])
        self.assertEqual(self.pool.busy_count(), 0)

    async def test_a_conversation_still_being_driven_is_skipped(self):
        self.note(1, 7200)
        self.note(2, 7200)
        server._live_conversations.add(_id(2))
        out = await self.call()
        self.assertEqual(out["deleted"], [_id(1)])
        self.assertIn(_id(2), self.ledger)

    async def test_nothing_due_takes_no_browser(self):
        self.note(1, 60)
        out = await self.call()
        self.assertEqual((out["due"], self.calls, self.lanes_used), (0, [], []))

    async def test_dry_run_names_them_and_deletes_nothing(self):
        self.note(1, 7200)
        out = await self.call(dry_run=True)
        self.assertEqual(out["ids"], [_id(1)])
        self.assertEqual(self.calls, [])
        self.assertIn(_id(1), self.ledger)

    async def test_busy_server_answers_409_and_waits_for_nobody(self):
        self.note(1, 7200)
        held = self.pool.try_acquire()
        with self.assertRaises(HTTPException) as caught:
            await self.call()
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.calls, [])
        # Not idle-only: still no free warm lane (1 and 2 are cold), still 409.
        with self.assertRaises(HTTPException) as caught:
            await self.call(idle_only=False)
        self.assertEqual(caught.exception.status_code, 409)
        # A warm free lane runs it beside the request.
        self.pool.set_warm(1, True)
        out = await self.call(idle_only=False)
        self.assertEqual(out["deleted"], [_id(1)])
        self.assertEqual(set(self.lanes_used), {1})       # readied, then used
        self.pool.release(held)

    async def test_a_waiting_request_gets_409(self):
        self.note(1, 7200)
        a = self.pool.try_acquire()
        waiter = asyncio.create_task(self.pool.acquire())
        await asyncio.sleep(0)
        self.pool.set_warm(1, True)                 # free, but someone is waiting
        self.pool._dispatch()
        b = await waiter                            # the waiter got lane 1
        waiter2 = asyncio.create_task(self.pool.acquire())
        await asyncio.sleep(0)
        self.pool.set_warm(2, True)
        self.pool._dispatch()
        c = await waiter2
        waiter3 = asyncio.create_task(self.pool.acquire())
        await asyncio.sleep(0)
        with self.assertRaises(HTTPException) as caught:
            await self.call(idle_only=False)
        self.assertEqual(caught.exception.status_code, 409)
        for lane in (a, b, c):
            self.pool.release(lane)
        self.pool.release(await waiter3)

    async def test_an_image_waiting_at_the_gate_counts_as_busy(self):
        self.note(1, 7200)
        gate = asyncio.Semaphore(1)
        await gate.acquire()
        with mock.patch.dict(server._image_gates, {NAME: gate}):
            with self.assertRaises(HTTPException) as caught:
                await self.call()
        self.assertEqual(caught.exception.status_code, 409)

    async def test_gone_failed_and_malformed(self):
        for n in (1, 2, 3, 4):
            self.note(n, 7200)
        self.answer = ([_id(1)], {_id(2): "HTTP 404", _id(3): "HTTP 500", _id(4): "malformed id"})
        out = await self.call()
        self.assertEqual(out["already_gone"], [_id(2)])
        self.assertEqual(sorted(out["failed"]), [_id(3), _id(4)])
        self.assertEqual(self.ledger.due(0), [_id(3)])      # tried again next hour
        data = json.loads(self.ledger.path.read_text())["conversations"]
        self.assertEqual(data[_id(3)]["attempts"], 1)

    async def test_a_session_error_is_502_and_forgets_nothing(self):
        self.note(1, 7200)
        self.raise_on_delete = RuntimeError("no session token")
        with mock.patch.object(server, "_evict_dead_browser", mock.AsyncMock()):
            with self.assertRaises(HTTPException) as caught:
                await self.call()
        self.assertEqual(caught.exception.status_code, 502)
        self.assertIn(_id(1), self.ledger)
        self.assertEqual(self.pool.busy_count(), 0)

    async def test_large_batches_stop_when_a_request_arrives(self):
        for n in range(server._MAX_BULK_DELETE + 5):
            self.note(n, 7200)
        arrived = []

        async def delete_then_a_request_arrives(prov, page, ids):
            self.calls.append(list(ids))
            if not arrived:
                arrived.append(asyncio.create_task(self.pool.acquire()))
                await asyncio.sleep(0)
            return list(ids), {}

        with mock.patch.object(ChatGPTProvider, "delete_conversations",
                               delete_then_a_request_arrives):
            out = await self.call()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(out["stopped"], "a request arrived")
        self.assertEqual(len(self.ledger), 5)
        self.pool.release(await arrived[0])

    async def test_cross_origin_is_refused(self):
        self.note(1, 7200)
        with self.assertRaises(HTTPException) as caught:
            await self.call("https://site.example")
        self.assertEqual(caught.exception.status_code, 403)
        self.assertEqual(self.calls, [])

    async def test_delete_endpoint_can_keep_images_and_updates_the_ledger(self):
        self.note(1, 60)
        out = await server.api_conversations_delete(
            server.ConversationDeleteRequest(ids=[_id(1)], keep_images=True), req())
        self.assertEqual(out["deleted"], [_id(1)])
        self.assertEqual(out["images_deleted"], [])
        self.assertNotIn(_id(1), self.ledger)


# ---------------------------------------------------------------------------
# browser-llm cleanup (what the timer runs)
# ---------------------------------------------------------------------------
class CleanupCommandTest(unittest.TestCase):
    def run_cli(self, answers, *args):
        clock = [0.0]
        sent = []

        def post(url, body):
            sent.append(dict(body, url=url, at=clock[0]))
            a = answers.pop(0)
            if isinstance(a, Exception):
                raise a
            return a

        def sleep(s):
            clock[0] += s

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.dict(os.environ, {"BROWSER_LLM_API": "http://h:1"}):
            code = cli.cleanup(list(args), post=post, sleep=sleep, clock=lambda: clock[0])
        return code, sent, out.getvalue(), err.getvalue()

    def ok(self, **kw):
        return 200, {"model": NAME, "due": 2, "recorded": 3, "deleted": [_id(1), _id(2)],
                     "already_gone": [], "failed": [], **kw}

    def test_one_call_when_the_server_is_free(self):
        code, sent, out, _ = self.run_cli([self.ok()])
        self.assertEqual(code, 0)
        self.assertEqual(sent[0]["url"], "http://h:1/api/conversations/cleanup")
        self.assertEqual((sent[0]["older_than"], sent[0]["idle_only"]), (3600, True))
        self.assertIn("deleted 2 of 2", out)

    def test_waits_for_idle_then_takes_any_free_browser(self):
        busy = (409, {"detail": "busy"})
        answers = [busy] * 45 + [self.ok()]
        code, sent, _, _ = self.run_cli(answers, "--retry", "30")
        self.assertEqual(code, 0)
        idle = [s["idle_only"] for s in sent]
        self.assertTrue(all(idle[:40]))              # 20 minutes of idle-only tries
        self.assertFalse(any(idle[40:]))

    def test_gives_up_quietly_on_a_server_busy_for_the_whole_wait(self):
        code, sent, _, err = self.run_cli([(409, {"detail": "busy"})] * 200)
        self.assertEqual(code, 0)
        self.assertIn("stayed busy", err)
        self.assertLessEqual(sent[-1]["at"], 50 * 60)

    def test_an_unreachable_server_is_an_error_after_a_few_tries(self):
        import urllib.error
        code, sent, _, err = self.run_cli([urllib.error.URLError("refused")] * 10)
        self.assertEqual((code, len(sent)), (1, 5))
        self.assertIn("unreachable", err)

    def test_a_server_error_is_an_error(self):
        code, _, _, err = self.run_cli([(502, {"detail": "no session token"})])
        self.assertEqual(code, 1)
        self.assertIn("no session token", err)

    def test_dry_run_lists_the_ids(self):
        code, sent, out, _ = self.run_cli(
            [(200, {"model": NAME, "due": 1, "recorded": 1, "dry_run": True, "ids": [_id(1)]})],
            "--dry-run", "--older-than", "90")
        self.assertEqual(code, 0)
        self.assertEqual((sent[0]["dry_run"], sent[0]["older_than"]), (True, 5400))
        self.assertIn(_id(1), out)


class UnitTemplateTest(unittest.TestCase):
    """The units install-service.sh writes, filled the way it fills them."""

    ROOT = Path(__file__).resolve().parent.parent

    def fill(self, template: str, install_dir: str) -> str:
        import subprocess
        script = (self.ROOT / "scripts/install-service.sh").read_text()
        start = script.index("fill() {")
        body = script[start:script.index("\n}\n", start) + 3]
        out = subprocess.run(
            ["bash", "-c", body + 'fill "$1"', "fill", str(self.ROOT / "deploy/systemd" / template)],
            env={**os.environ, "DIR": install_dir}, capture_output=True, text=True, check=True)
        return out.stdout

    def test_a_path_with_spaces_fills_every_line_correctly(self):
        d = "/home/u/My Projects (x)/browser-llm-api"
        for template in ("browser-llm-api.service.template", "browser-llm-health.service.template",
                         "browser-llm-cleanup.service.template"):
            unit = self.fill(template, d)
            self.assertNotIn("__INSTALL_DIR__", unit, template)
            for line in unit.splitlines():
                if line.startswith("Documentation="):
                    # A list split on spaces; %% is systemd's escape for %.
                    self.assertNotIn(" ", line, template)
                    self.assertIn("My%%20Projects%%20(x)", line, template)
                elif line.startswith("WorkingDirectory="):
                    self.assertEqual(line, f"WorkingDirectory={d}", template)

    def test_the_cleanup_unit_runs_the_command_hourly(self):
        unit = self.fill("browser-llm-cleanup.service.template", "/opt/blm")
        self.assertIn('ExecStart="/opt/blm/venv/bin/python" -m browser_llm cleanup', unit)
        self.assertIn("Type=oneshot", unit)
        timer = (self.ROOT / "deploy/systemd/browser-llm-cleanup.timer").read_text()
        self.assertIn("OnCalendar=hourly", timer)
        self.assertIn("WantedBy=timers.target", timer)


if __name__ == "__main__":
    unittest.main()
