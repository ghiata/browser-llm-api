"""
Lanes (browser_llm/lanes.py and its use in server.py): which lane a request
takes, how a lane's login is copied, the private display, and what the server
does when an extra lane cannot start. No browser and no network: Chrome is
replaced by a recorder, the cookie store is a small SQLite file, and the display
test needs Xvfb and is skipped without it.

Run from the repo root:
    ./venv/bin/python -m unittest discover -s tests -v
"""
import asyncio
import hashlib
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_llm import lanes, providers, server  # noqa: E402
from browser_llm.providers.chatgpt import ChatGPTProvider  # noqa: E402

NAME = "chatgpt-browser"


class FakeClock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


class LanePoolTest(unittest.IsolatedAsyncioTestCase):
    async def test_warm_lane_is_preferred_over_a_cold_one(self):
        pool = lanes.LanePool(3)
        pool.set_warm(0, True)
        pool.set_warm(2, True)
        self.assertEqual(await pool.acquire(), 0)
        self.assertEqual(await pool.acquire(), 2)     # running, though cold lane 1 is free and lower

    async def test_a_cold_lane_is_taken_only_when_every_warm_lane_is_busy(self):
        pool = lanes.LanePool(3)
        pool.set_warm(0, True)
        self.assertEqual(await pool.acquire(), 0)
        self.assertEqual(await pool.acquire(), 1)     # lowest cold lane
        self.assertEqual(await pool.acquire(), 2)
        self.assertTrue(pool.must_queue())

    async def test_waiters_are_served_in_arrival_order(self):
        pool = lanes.LanePool(2)
        a, b = await pool.acquire(), await pool.acquire()
        order = []

        async def want(tag):
            order.append((tag, await pool.acquire()))

        first = asyncio.create_task(want("first"))
        await asyncio.sleep(0)
        second = asyncio.create_task(want("second"))
        await asyncio.sleep(0)
        self.assertEqual(pool.waiting(), 2)
        pool.release(b)
        await asyncio.sleep(0.01)
        self.assertEqual(order, [("first", b)])
        pool.release(a)
        await asyncio.wait_for(asyncio.gather(first, second), 2)
        self.assertEqual([t for t, _ in order], ["first", "second"])

    async def test_a_lane_granted_to_a_cancelled_waiter_is_given_back(self):
        pool = lanes.LanePool(1)
        held = await pool.acquire()
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        pool.release(held)         # grants lane 0 to the waiter's future...
        waiter.cancel()            # ...but the waiter is cancelled before it resumes
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(pool.busy_count(), 0)
        self.assertEqual(await asyncio.wait_for(pool.acquire(), 1), 0)

    async def test_a_cancelled_waiter_leaves_the_queue(self):
        pool = lanes.LanePool(1)
        await pool.acquire()
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0)
        self.assertEqual(pool.waiting(), 1)
        waiter.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(pool.waiting(), 0)

    async def test_a_downed_lane_is_skipped_until_its_time_is_up(self):
        clock = FakeClock()
        pool = lanes.LanePool(2, clock=clock)
        pool.set_warm(0, True)
        await pool.acquire()                        # lane 0 busy
        lane = await pool.acquire()                 # lane 1, cold
        pool.release(lane, down_for=300, served=False)
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0.01)
        self.assertFalse(waiter.done())             # lane 1 is down, lane 0 busy
        self.assertEqual(pool.snapshot()[1]["state"], "down")
        clock.t += 301
        pool.release(0)                             # any release looks again
        self.assertEqual(await asyncio.wait_for(waiter, 1), 0)

    async def test_waiters_are_woken_when_a_downed_lane_comes_back(self):
        pool = lanes.LanePool(2)
        await pool.acquire()                        # lane 0 busy, held throughout
        lane = await pool.acquire()
        pool.release(lane, down_for=0.05, served=False)
        got = await asyncio.wait_for(pool.acquire(), 2)   # nobody releases: the timer wakes it
        self.assertEqual(got, 1)

    async def test_history_work_does_not_start_an_extra_lane(self):
        pool = lanes.LanePool(3)
        pool.set_warm(0, True)
        await pool.acquire()                        # lane 0 busy; 1 and 2 are cold
        waiter = asyncio.create_task(pool.acquire(cold_ok=False))
        await asyncio.sleep(0.02)
        self.assertFalse(waiter.done())
        pool.release(0)
        self.assertEqual(await asyncio.wait_for(waiter, 1), 0)

    async def test_history_work_may_use_a_warm_extra_lane(self):
        pool = lanes.LanePool(3)
        pool.set_warm(0, True)
        pool.set_warm(1, True)
        await pool.acquire()
        self.assertEqual(await asyncio.wait_for(pool.acquire(cold_ok=False), 1), 1)

    async def test_lane_zero_is_allowed_cold_for_history_work(self):
        pool = lanes.LanePool(2)                    # nothing started yet
        self.assertEqual(await asyncio.wait_for(pool.acquire(cold_ok=False), 1), 0)

    async def test_a_busy_lane_cannot_be_claimed_and_a_free_one_can(self):
        pool = lanes.LanePool(2)
        lane = await pool.acquire()
        self.assertFalse(pool.claim(lane))
        self.assertTrue(pool.claim(1))
        self.assertEqual(pool.busy_count(), 2)

    async def test_idle_extras_lists_only_running_free_extra_lanes_past_the_limit(self):
        clock = FakeClock()
        pool = lanes.LanePool(4, clock=clock)
        for i in range(4):
            pool.set_warm(i, True)
        busy = await pool.acquire()                 # lane 0 stays busy
        self.assertEqual(busy, 0)
        clock.t += 100
        pool.release(await pool.acquire())          # lane 1 was used just now
        clock.t += 500
        # lane 0 is busy and never closed; lane 1 was freed 500 s ago; 2 and 3
        # have been idle 600 s. A limit of 550 s leaves lane 1 open.
        self.assertEqual(pool.idle_extras(550), [2, 3])
        self.assertEqual(pool.idle_extras(400), [1, 2, 3])
        pool.release(0)
        self.assertNotIn(0, pool.idle_extras(0))    # lane 0 is never an extra

    async def test_must_queue_is_false_while_a_lane_is_free(self):
        pool = lanes.LanePool(2)
        self.assertFalse(pool.must_queue())
        await pool.acquire()
        self.assertFalse(pool.must_queue())
        await pool.acquire()
        self.assertTrue(pool.must_queue())

    async def test_a_one_lane_pool_behaves_like_the_old_lock(self):
        pool = lanes.LanePool(1)
        self.assertEqual(await pool.acquire(), 0)
        waiter = asyncio.create_task(pool.acquire())
        await asyncio.sleep(0.01)
        self.assertFalse(waiter.done())
        self.assertTrue(pool.must_queue())
        pool.release(0)
        self.assertEqual(await asyncio.wait_for(waiter, 1), 0)

    def test_a_pool_needs_a_lane(self):
        with self.assertRaises(ValueError):
            lanes.LanePool(0)


class ProfileTest(unittest.TestCase):
    def test_lane_profile_names_keep_the_ignored_ending(self):
        self.assertEqual(lanes.lane_profile_dir("./chatgpt_profile", 0), "./chatgpt_profile")
        self.assertEqual(lanes.lane_profile_dir("./chatgpt_profile", 1), "chatgpt_lane2_profile")
        self.assertEqual(lanes.lane_profile_dir("/data/x/gemini_profile", 2),
                         "/data/x/gemini_lane3_profile")
        self.assertEqual(lanes.lane_profile_dir("plain", 1), "plain_lane2_profile")

    def test_a_lane_provider_is_fresh_and_keyed_apart(self):
        main = ChatGPTProvider()
        main._model_seen = "GPT-6"
        lane = main.for_lane(2)
        self.assertEqual((main.key, lane.key), (NAME, f"{NAME}#2"))
        self.assertEqual(lane.profile_dir, "chatgpt_lane3_profile")
        self.assertIsNone(lane._model_seen)                 # the picker belongs to one browser
        self.assertEqual(main.profile_dir, "./chatgpt_profile")

    def test_lane_count_comes_from_the_environment_and_is_bounded(self):
        for raw, want in (("3", 3), ("", 1), ("x", 1), ("0", 1), ("99", 8), (" 2 ", 2)):
            with mock.patch.dict(os.environ, {"BROWSER_LLM_LANES": raw}):
                self.assertEqual(providers._lane_count(), want, raw)

    def test_only_a_provider_verified_for_lanes_gets_more_than_one(self):
        built = providers._build_lanes(3)
        self.assertEqual(len(built[NAME]), 3)
        self.assertEqual(len(built["gemini-browser"]), 1)
        self.assertIs(built[NAME][0], providers.PROVIDERS[NAME])
        self.assertEqual([p.lane for p in built[NAME]], [0, 1, 2])


class CloneLoginTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="blm-lane-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.src, self.dst = self.tmp / "main_profile", self.tmp / "lane2_profile"
        (self.src / "Default").mkdir(parents=True)
        (self.src / "Local State").write_text('{"main": true}')
        self._cookies(self.src, [("chatgpt.com", "session", "AAA"), ("chatgpt.com", "did", "BBB")])

    @staticmethod
    def _cookies(profile, rows):
        con = sqlite3.connect(profile / "Default" / "Cookies")
        con.execute("create table cookies (host_key text, name text, value text)")
        con.executemany("insert into cookies values (?,?,?)", rows)
        con.commit()
        con.close()

    @staticmethod
    def _digest(path):
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()

    def test_copies_the_cookies_and_leaves_the_source_alone(self):
        before = self._digest(self.src / "Default" / "Cookies")
        n = lanes.clone_login(str(self.src), str(self.dst))
        self.assertEqual(n, 2)
        con = sqlite3.connect(self.dst / "Default" / "Cookies")
        self.assertEqual(con.execute("select name, value from cookies order by name").fetchall(),
                         [("did", "BBB"), ("session", "AAA")])
        con.close()
        self.assertEqual(self._digest(self.src / "Default" / "Cookies"), before)
        self.assertEqual(sorted(p.name for p in (self.src / "Default").iterdir()), ["Cookies"])

    def test_a_second_clone_replaces_the_lanes_old_session(self):
        lanes.clone_login(str(self.src), str(self.dst))
        con = sqlite3.connect(self.src / "Default" / "Cookies")
        con.execute("update cookies set value = 'NEW' where name = 'session'")
        con.commit()
        con.close()
        lanes.clone_login(str(self.src), str(self.dst))
        con = sqlite3.connect(self.dst / "Default" / "Cookies")
        self.assertEqual(con.execute("select value from cookies where name='session'").fetchone(),
                         ("NEW",))
        con.close()

    def test_local_state_is_copied_once(self):
        lanes.clone_login(str(self.src), str(self.dst))
        self.assertEqual((self.dst / "Local State").read_text(), '{"main": true}')
        (self.dst / "Local State").write_text('{"lane": true}')
        lanes.clone_login(str(self.src), str(self.dst))
        self.assertEqual((self.dst / "Local State").read_text(), '{"lane": true}')

    def test_a_source_that_was_never_signed_in_is_refused_plainly(self):
        empty = self.tmp / "empty_profile"
        empty.mkdir()
        with self.assertRaises(lanes.LaneCloneError) as cm:
            lanes.clone_login(str(empty), str(self.dst))
        self.assertIn("sign in", str(cm.exception))

    def test_a_cookie_file_that_is_not_a_database_is_a_clone_error(self):
        (self.src / "Default" / "Cookies").write_bytes(b"not a database" * 100)
        with self.assertRaises(lanes.LaneCloneError):
            lanes.clone_login(str(self.src), str(self.dst))


@unittest.skipUnless(shutil.which("Xvfb") and shutil.which("xdpyinfo"), "needs Xvfb and xdpyinfo")
class PrivateDisplayTest(unittest.TestCase):
    def _xdpyinfo(self, env):
        return subprocess.run(["xdpyinfo"], env=env, capture_output=True, timeout=20)

    def test_a_display_admits_only_a_client_with_its_cookie_and_goes_away(self):
        d = lanes.PrivateDisplay(first=170)
        d.start()
        try:
            base = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "XAUTHORITY")}
            self.assertEqual(self._xdpyinfo({**base, **d.env}).returncode, 0)
            other = Path(tempfile.mkdtemp(prefix="blm-empty-xauth-"))
            self.addCleanup(shutil.rmtree, other, ignore_errors=True)
            (other / "x").write_bytes(b"")
            refused = self._xdpyinfo({**base, "DISPLAY": d.env["DISPLAY"],
                                      "XAUTHORITY": str(other / "x")})
            self.assertNotEqual(refused.returncode, 0)
            auth_dir = Path(d.env["XAUTHORITY"]).parent
            self.assertEqual(oct(Path(d.env["XAUTHORITY"]).stat().st_mode & 0o777), "0o600")
        finally:
            d.stop()
        self.assertFalse(auth_dir.exists())
        self.assertEqual(d.env, {})

    def test_two_displays_get_different_numbers(self):
        a, b = lanes.PrivateDisplay(first=180), lanes.PrivateDisplay(first=180)
        a.start()
        try:
            b.start()
            try:
                self.assertNotEqual(a.env["DISPLAY"], b.env["DISPLAY"])
            finally:
                b.stop()
        finally:
            a.stop()


class FakeBrowser:
    stopped = False

    def __init__(self):
        self.stops = 0

    def stop(self):
        self.stops += 1
        self.stopped = True


class ServerLaneTest(unittest.IsolatedAsyncioTestCase):
    """server._lane with a fake get_browser: which lane a request gets, and what
    happens when an extra lane cannot start."""

    async def asyncSetUp(self):
        self.group = providers._build_lanes(3)[NAME]
        self.pool = lanes.LanePool(3)
        self.started: list[str] = []
        self.fail_start: set[int] = set()
        self.verify_fails: set[int] = set()

        async def fake_get_browser(lp):
            if lp.lane in self.fail_start:
                raise lanes.LaneUnavailable("could not start")
            if lp.key not in server._browsers:
                server._browsers[lp.key] = FakeBrowser()
                self.pool.set_warm(lp.lane, True)
                self.started.append(lp.key)
            return server._browsers[lp.key]

        async def fake_verify(lp, browser):
            if lp.lane in self.verify_fails:
                raise lanes.LaneUnavailable("signed out")

        patches = [mock.patch.dict(server.LANES, {NAME: self.group}),
                   mock.patch.dict(server._pools, {NAME: self.pool}),
                   mock.patch.dict(server._browsers, {}, clear=True),
                   mock.patch.dict(server._img_gen_count, {}, clear=True),
                   mock.patch.object(server, "_verified_lanes", set()),
                   mock.patch.object(server, "get_browser", fake_get_browser),
                   mock.patch.object(server, "_verify_lane_login", fake_verify)]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.main = self.group[0]

    async def test_concurrent_requests_take_different_lanes_and_the_third_waits(self):
        order = []
        release = [asyncio.Event() for _ in range(4)]

        async def one(n):
            async with server._lane(self.main) as lp:
                order.append((n, lp.lane))
                await release[n].wait()

        tasks = [asyncio.create_task(one(n)) for n in range(4)]
        await asyncio.sleep(0.05)
        self.assertEqual(sorted(lane for _, lane in order), [0, 1, 2])     # three lanes, one request waits
        self.assertEqual(self.pool.waiting(), 1)
        waiting = ({0, 1, 2, 3} - {n for n, _ in order}).pop()
        release[order[0][0]].set()
        await asyncio.sleep(0.05)
        self.assertEqual(len(order), 4)
        self.assertEqual(order[3][0], waiting)
        for r in release:
            r.set()
        await asyncio.gather(*tasks)
        self.assertEqual(self.pool.busy_count(), 0)

    async def test_one_request_at_a_time_never_starts_an_extra_browser(self):
        for _ in range(3):
            async with server._lane(self.main) as lp:
                self.assertEqual(lp.lane, 0)
        self.assertEqual(self.started, [])           # lane 0's browser is started by the drive, not here

    async def test_an_extra_lane_that_will_not_start_is_skipped_and_left_alone(self):
        self.fail_start = {1}
        hold = asyncio.Event()

        async def holder():
            async with server._lane(self.main):
                await hold.wait()

        busy = asyncio.create_task(holder())
        await asyncio.sleep(0.01)                     # lane 0 busy
        async with server._lane(self.main) as lp:     # lane 1 fails; lane 2 starts
            self.assertEqual(lp.lane, 2)
        self.assertEqual(self.pool.snapshot()[1]["state"], "down")
        self.assertEqual(self.started, [f"{NAME}#2"])
        hold.set()
        await busy

    async def test_a_signed_out_extra_lane_is_stopped_and_marked_down(self):
        self.verify_fails = {1, 2}
        hold = asyncio.Event()

        async def holder():
            async with server._lane(self.main):
                await hold.wait()

        busy = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        taker = asyncio.create_task(self._take())
        await asyncio.sleep(0.05)
        self.assertFalse(taker.done())                # both extras down: waits for lane 0
        self.assertEqual([r["state"] for r in self.pool.snapshot()], ["busy", "down", "down"])
        for key in (f"{NAME}#1", f"{NAME}#2"):
            self.assertNotIn(key, server._browsers)    # the failed browsers were stopped
        hold.set()
        self.assertEqual(await asyncio.wait_for(taker, 1), 0)
        await busy

    async def _take(self):
        async with server._lane(self.main) as lp:
            return lp.lane

    async def test_a_cancelled_start_gives_the_lane_back_and_stops_the_browser(self):
        gate = asyncio.Event()

        async def slow_get_browser(lp):
            server._browsers[lp.key] = FakeBrowser()
            await gate.wait()

        hold = asyncio.Event()

        async def holder():
            async with server._lane(self.main):
                await hold.wait()

        busy = asyncio.create_task(holder())
        await asyncio.sleep(0.01)
        with mock.patch.object(server, "get_browser", slow_get_browser):
            taker = asyncio.create_task(self._take())
            await asyncio.sleep(0.02)
            taker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await taker
        self.assertNotIn(f"{NAME}#1", server._browsers)
        self.assertEqual(self.pool.snapshot()[1]["state"], "free")
        hold.set()
        await busy

    async def test_history_work_waits_for_lane_zero_instead_of_starting_a_browser(self):
        hold = asyncio.Event()

        async def holder():
            async with server._lane(self.main):
                await hold.wait()

        busy = asyncio.create_task(holder())
        await asyncio.sleep(0.01)

        async def read():
            async with server._lane(self.main, cold_ok=False) as lp:
                return lp.lane

        reader = asyncio.create_task(read())
        await asyncio.sleep(0.05)
        self.assertFalse(reader.done())
        self.assertEqual(self.started, [])
        hold.set()
        self.assertEqual(await asyncio.wait_for(reader, 1), 0)
        await busy

    async def test_the_lane_is_released_when_the_request_fails(self):
        with self.assertRaises(RuntimeError):
            async with server._lane(self.main):
                raise RuntimeError("drive failed")
        self.assertEqual(self.pool.busy_count(), 0)

    async def test_idle_extra_lanes_are_closed_and_busy_or_main_lanes_are_not(self):
        clock = FakeClock()
        pool = lanes.LanePool(3, clock=clock)
        with mock.patch.dict(server._pools, {NAME: pool}):
            for lp in self.group:
                server._browsers[lp.key] = FakeBrowser()
                pool.set_warm(lp.lane, True)
            fakes = {lp.lane: server._browsers[lp.key] for lp in self.group}
            self.assertEqual(await pool.acquire(), 0)
            lane1 = await pool.acquire()
            clock.t += 5000
            with mock.patch.object(server, "_LANE_IDLE_SECONDS", 600.0):
                await server._reap_idle_lanes()
            self.assertEqual((fakes[0].stops, fakes[lane1].stops, fakes[2].stops), (0, 0, 1))
            self.assertNotIn(f"{NAME}#2", server._browsers)
            self.assertFalse(pool.is_warm(2))
            self.assertIn(NAME, server._browsers)

    async def test_stopping_a_lane_forgets_its_display_count_and_login_check(self):
        lp = self.group[1]
        display = mock.Mock()
        server._browsers[lp.key] = fake = FakeBrowser()
        server._displays[lp.key] = display
        server._verified_lanes.add(lp.key)
        server._img_gen_count[lp.key] = 7
        self.pool.set_warm(1, True)
        server._stop_browser(lp, fake)
        self.assertEqual(fake.stops, 1)
        display.stop.assert_called_once()
        self.assertNotIn(lp.key, server._displays)
        self.assertNotIn(lp.key, server._verified_lanes)
        self.assertEqual(server._img_gen_count[lp.key], 0)
        self.assertFalse(self.pool.is_warm(1))

    async def test_status_lists_every_lane_and_keeps_busy_for_the_restart_check(self):
        server._browsers[NAME] = FakeBrowser()
        self.pool.set_warm(0, True)
        body = server._status_body()["providers"][NAME]
        self.assertEqual((body["lanes_total"], body["lanes_busy"], body["busy"], body["queued"]),
                         (3, 0, False, 0))
        self.assertEqual([r["number"] for r in body["lanes"]], [1, 2, 3])
        async with server._lane(self.main):
            body = server._status_body()["providers"][NAME]
            self.assertEqual((body["busy"], body["lanes_busy"]), (True, 1))
            self.assertEqual(body["lanes"][0]["state"], "busy")


if __name__ == "__main__":
    unittest.main()
