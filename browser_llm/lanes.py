"""
Lanes: several browsers for one provider, so requests stop queueing behind one.

A provider used to have one Chrome and one lock, so one request ran at a time and
every other caller waited, up to the ten minutes a long GPT-6 High answer takes.
A *lane* is one Chrome with its own profile directory and its own private X
display. Lane 0 is the provider's own browser and profile. Every other lane
starts from a copy of lane 0's signed-in session, on demand, and closes when it
has been idle for a while.

Why a separate Chrome and not a second tab: ChatGPT multiplexes one WebSocket
per profile, so two live tabs in one browser leave one conversation reading
nothing (see CLAUDE.md, "One tab per profile"). Why a separate display: Chrome
on X11 reports a covered window as hidden and the sites then drop the typed
prompt, and three windows on one display cover each other.

Stdlib only, so tests import it without a browser. ``LanePool`` decides which
lane a request takes; ``clone_login`` and ``PrivateDisplay`` are what a lane
needs before its Chrome can start. server.py does the starting and stopping.
"""
import asyncio
import os
import secrets
import shutil
import socket
import sqlite3
import struct
import subprocess
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import Callable, Optional


class LaneCloneError(RuntimeError):
    """The login could not be copied from lane 0 into a lane's profile."""


class LaneUnavailable(RuntimeError):
    """A lane could not be started or is not signed in. The request that was
    given it goes back to the pool for another lane."""


# ---------------------------------------------------------------------------
# Choosing a lane
# ---------------------------------------------------------------------------
class _Waiter:
    __slots__ = ("fut", "cold_ok")

    def __init__(self, fut: "asyncio.Future[int]", cold_ok: bool):
        self.fut = fut
        self.cold_ok = cold_ok


class LanePool:
    """Which lane the next request takes. One pool per provider.

    A request takes the lowest-numbered free lane whose browser is running
    (*warm*). Only when every warm lane is busy does it take a *cold* lane,
    which the caller then starts, so an extra browser exists only while there
    is work for it. Lane 0 counts as always allowed, warm or not: it is the
    provider's own browser, started on first use or at start-up.

    Waiting requests are served in arrival order. Not tied to a browser or a
    clock: ``clock`` is injectable so tests need not sleep.
    """

    def __init__(self, size: int, clock: Callable[[], float] = time.monotonic):
        if size < 1:
            raise ValueError("a pool needs at least one lane")
        self.size = size
        self._clock = clock
        self._busy: set[int] = set()
        self._warm: set[int] = set()
        self._down_until: dict[int, float] = {}
        self._idle_since: dict[int, float] = {i: clock() for i in range(size)}
        self._waiters: deque[_Waiter] = deque()
        self.last_lane: Optional[int] = None  # the lane that finished most recently

    # -- choosing -----------------------------------------------------------
    def _free(self) -> list[int]:
        now = self._clock()
        return [i for i in range(self.size)
                if i not in self._busy and self._down_until.get(i, 0.0) <= now]

    def _pick(self, cold_ok: bool) -> Optional[int]:
        free = self._free()
        for i in free:
            if i in self._warm:
                return i
        if cold_ok and free:
            return free[0]
        if 0 in free:        # the provider's own browser may always start
            return 0
        return None

    def _dispatch(self) -> None:
        """Hand every free lane to the waiters, oldest first. A waiter that
        cannot take the free lane is passed over, not blocked on."""
        for w in list(self._waiters):
            if w.fut.done():
                self._waiters.remove(w)
                continue
            lane = self._pick(w.cold_ok)
            if lane is None:
                continue
            self._busy.add(lane)
            self._waiters.remove(w)
            w.fut.set_result(lane)

    # -- taking and giving back --------------------------------------------
    async def acquire(self, *, cold_ok: bool = True) -> int:
        """Wait for a lane and return its number. ``cold_ok=False`` takes a
        lane other than 0 only if its browser is already running, for work that
        is not worth starting a Chrome for (reading the account's history)."""
        w = _Waiter(asyncio.get_running_loop().create_future(), cold_ok)
        self._waiters.append(w)
        self._dispatch()
        try:
            return await w.fut
        except asyncio.CancelledError:
            if w.fut.done() and not w.fut.cancelled():
                self.release(w.fut.result())   # granted, but nobody is left to use it
            elif w in self._waiters:
                self._waiters.remove(w)
            raise

    def release(self, lane: int, *, down_for: float = 0.0, served: bool = True) -> None:
        """Give a lane back. ``down_for`` keeps it out of the pool that many
        seconds, for a lane that failed to start. ``served`` is False when the
        lane did no request's work (it failed to start, or was being closed)."""
        self._busy.discard(lane)
        now = self._clock()
        self._idle_since[lane] = now
        if served:
            self.last_lane = lane
        if down_for > 0:
            self._down_until[lane] = now + down_for
            try:   # waiters are woken again when the lane returns
                asyncio.get_running_loop().call_later(down_for + 0.01, self._dispatch)
            except RuntimeError:
                pass
        self._dispatch()

    def claim(self, lane: int) -> bool:
        """Take a specific lane if it is free (the idle reaper stopping its
        browser). False when it is busy, so a lane in use is never stopped."""
        if lane in self._busy or any(not w.fut.done() for w in self._waiters):
            return False
        self._busy.add(lane)
        return True

    # -- what the browsers are doing ----------------------------------------
    def set_warm(self, lane: int, running: bool) -> None:
        (self._warm.add if running else self._warm.discard)(lane)

    def is_warm(self, lane: int) -> bool:
        return lane in self._warm

    def idle_extras(self, older_than: float) -> list[int]:
        """Extra lanes (not 0) whose browser runs, that nobody is using and
        that have been free at least ``older_than`` seconds."""
        now = self._clock()
        return [i for i in sorted(self._warm)
                if i > 0 and i not in self._busy
                and now - self._idle_since.get(i, now) >= older_than]

    # -- reading ------------------------------------------------------------
    def must_queue(self) -> bool:
        """Whether a request arriving now waits: someone is already waiting, or
        no lane is free."""
        return self.waiting() > 0 or self._pick(True) is None

    def waiting(self) -> int:
        return sum(1 for w in self._waiters if not w.fut.done())

    def busy_count(self) -> int:
        return len(self._busy)

    def snapshot(self) -> list[dict]:
        now = self._clock()
        out = []
        for i in range(self.size):
            down = max(0.0, self._down_until.get(i, 0.0) - now)
            out.append({
                "lane": i,
                "state": "busy" if i in self._busy else ("down" if down else "free"),
                "browser_running": i in self._warm,
                "idle_s": None if i in self._busy else int(now - self._idle_since.get(i, now)),
                "down_s": int(down) if down else None,
            })
        return out


# ---------------------------------------------------------------------------
# What a lane needs before Chrome starts
# ---------------------------------------------------------------------------
def lane_profile_dir(profile_dir: str, lane: int) -> str:
    """The profile directory of lane ``lane`` (0 is the provider's own).
    ``./chatgpt_profile`` gives ``chatgpt_lane2_profile`` for lane 1: it keeps
    the ``*_profile`` ending the repository ignores, and counts from 1 the way
    a person does."""
    if lane == 0:
        return profile_dir
    p = Path(profile_dir)
    stem = p.name[:-len("_profile")] if p.name.endswith("_profile") else p.name
    return str(p.with_name(f"{stem}_lane{lane + 1}_profile"))


def clone_login(src: str, dst: str) -> int:
    """Copy the signed-in session of profile ``src`` into profile ``dst`` and
    return how many cookies were copied.

    Only the cookie store is copied, replacing the lane's old one, so a lane
    always starts with lane 0's newest session. The session is a cookie
    (``--password-store=basic`` encrypts cookies with a fixed key, so a copy
    decrypts in another profile). ``Local State`` is copied once, when the lane
    has none. ``src`` is opened read-only through SQLite's backup call, which
    gives a consistent copy while lane 0's Chrome has the file open and leaves
    that profile exactly as it was.
    """
    src_p, dst_p = Path(src), Path(dst)
    cookies = src_p / "Default" / "Cookies"
    if not cookies.exists():
        raise LaneCloneError(f"{src} has no cookie store: sign in to that provider first")
    (dst_p / "Default").mkdir(parents=True, exist_ok=True)
    state = src_p / "Local State"
    if state.exists() and not (dst_p / "Local State").exists():
        shutil.copy2(state, dst_p / "Local State")
    target = dst_p / "Default" / "Cookies"
    for stale in (target, target.with_name("Cookies-journal")):
        stale.unlink(missing_ok=True)
    try:
        a = sqlite3.connect(f"file:{cookies}?mode=ro", uri=True, timeout=10)
        try:
            b = sqlite3.connect(str(target))
            try:
                a.backup(b)
                return b.execute("select count(*) from cookies").fetchone()[0]
            finally:
                b.close()
        finally:
            a.close()
    except sqlite3.Error as e:
        raise LaneCloneError(f"could not copy the login from {src}: {e}") from e


def _xauth_entry(display: int, cookie: bytes) -> bytes:
    """One MIT-MAGIC-COOKIE-1 record of an Xauthority file for a local display."""
    host = socket.gethostname().encode()
    num = str(display).encode()
    name = b"MIT-MAGIC-COOKIE-1"
    return (struct.pack(">H", 256)                       # FamilyLocal
            + struct.pack(">H", len(host)) + host
            + struct.pack(">H", len(num)) + num
            + struct.pack(">H", len(name)) + name
            + struct.pack(">H", len(cookie)) + cookie)


class PrivateDisplay:
    """An Xvfb of its own for one lane, so no other window can cover the lane's
    Chrome. It carries an access cookie like the shared display does: without it
    any local process could open windows on it and read the page.

    ``env`` holds what a Chrome started for this lane needs (DISPLAY and
    XAUTHORITY). Starting and stopping are blocking calls of a few hundred
    milliseconds; the server runs them on a thread.
    """

    def __init__(self, size: str = "1920x1080x24", first: int = 100):
        self.size = size
        self._first = first
        self._proc: Optional[subprocess.Popen] = None
        self._dir: Optional[str] = None
        self.env: dict[str, str] = {}

    @staticmethod
    def available() -> bool:
        return shutil.which("Xvfb") is not None

    def start(self) -> None:
        exe = shutil.which("Xvfb")
        if not exe:
            raise LaneUnavailable("Xvfb is not installed (package xvfb), so an extra lane has no display")
        base = os.environ.get("XDG_RUNTIME_DIR") or None
        self._dir = tempfile.mkdtemp(prefix="blm-x-", dir=base)  # mode 0700
        auth = os.path.join(self._dir, "xauthority")
        cookie = secrets.token_bytes(16)
        for n in range(self._first, self._first + 100):
            if os.path.exists(f"/tmp/.X11-unix/X{n}") or os.path.exists(f"/tmp/.X{n}-lock"):
                continue
            with open(auth, "wb") as f:
                f.write(_xauth_entry(n, cookie))
            os.chmod(auth, 0o600)
            proc = subprocess.Popen(
                [exe, f":{n}", "-screen", "0", self.size, "-nolisten", "tcp",
                 "-noreset", "-auth", auth],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    break                        # lost a race for this number; try the next
                if os.path.exists(f"/tmp/.X11-unix/X{n}"):
                    self._proc = proc
                    self.env = {"DISPLAY": f":{n}", "XAUTHORITY": auth}
                    return
                time.sleep(0.05)
            else:
                proc.kill()
                proc.wait()
        self.stop()
        raise LaneUnavailable("no free X display number for an extra lane")

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait()
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self.env = {}
