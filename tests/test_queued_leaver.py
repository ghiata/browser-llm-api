"""
A non-streaming chat request whose caller left while it waited in the provider's
queue is dropped before it takes the browser; a caller still waiting is served.

Driven through the real ``server.app`` on real uvicorn over real sockets, so the
API-key middleware sits in front of the route as it does in the service. That
matters: ``request.is_disconnected()`` reads a caller who left as connected
behind an ``@app.middleware("http")`` (Starlette's BaseHTTPMiddleware), so a
check proved on a bare FastAPI app proves nothing here. No browser starts:
lifespan is off and ``drive_once`` is replaced by a recorder.
"""
import asyncio
import json
import socket
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import uvicorn  # noqa: E402

from browser_llm import server  # noqa: E402

PROVIDER = "chatgpt-browser"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _request(port: int, prompt: str) -> bytes:
    body = json.dumps({"model": PROVIDER,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    head = (f"POST /v1/chat/completions HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\n"
            f"Connection: close\r\n\r\n").encode()
    return head + body


class QueuedLeaverTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.driven: list[str] = []

        async def fake_drive(provider, prompt, *a, **kw):
            self.driven.append(prompt)
            return f"answer to {prompt}", []

        self.patches = [mock.patch.object(server, "drive_once", fake_drive),
                        mock.patch.object(server, "API_KEY", ""),
                        mock.patch.object(server, "GUESTS", {})]
        for p in self.patches:
            p.start()
        # A fresh lock bound to this test's event loop.
        self.lock_patch = mock.patch.dict(server._locks, {PROVIDER: asyncio.Lock()})
        self.lock_patch.start()
        self.lock = server._locks[PROVIDER]
        self.port = _free_port()
        config = uvicorn.Config(server.app, host="127.0.0.1", port=self.port,
                                lifespan="off", log_level="warning")
        self.uv = uvicorn.Server(config)
        self.uv_task = asyncio.create_task(self.uv.serve())
        for _ in range(200):
            if self.uv.started:
                break
            await asyncio.sleep(0.02)
        self.assertTrue(self.uv.started, "uvicorn did not start")

    async def asyncTearDown(self):
        if self.lock.locked():
            self.lock.release()
        self.uv.should_exit = True
        await asyncio.wait_for(self.uv_task, 10)
        self.lock_patch.stop()
        for p in self.patches:
            p.stop()

    async def _queued(self, n: int):
        """Wait until n requests wait on the provider lock."""
        for _ in range(250):
            if len(getattr(self.lock, "_waiters", None) or ()) >= n:
                return
            await asyncio.sleep(0.02)
        self.fail(f"{n} request(s) never queued on the provider lock")

    async def _send(self, prompt: str):
        reader, writer = await asyncio.open_connection("127.0.0.1", self.port)
        writer.write(_request(self.port, prompt))
        await writer.drain()
        return reader, writer

    async def test_leaver_is_dropped_and_live_caller_is_served(self):
        await self.lock.acquire()          # another request holds the browser
        _, leaver = await self._send("leaver")
        await self._queued(1)
        leaver.close()                     # the caller gives up while queued
        await leaver.wait_closed()
        live_r, live_w = await self._send("live")
        await self._queued(2)
        await asyncio.sleep(0.3)           # let the server see the closed socket
        self.lock.release()                # the browser comes free

        raw = await asyncio.wait_for(live_r.read(), 15)
        live_w.close()
        status = raw.split(b"\r\n", 1)[0]
        self.assertIn(b" 200 ", status, raw[:300])
        answer = json.loads(raw.split(b"\r\n\r\n", 1)[1])
        self.assertEqual(answer["choices"][0]["message"]["content"], "answer to live")
        # FIFO lock: the leaver was first in line, so its turn came and went
        # before the live caller's drive.
        self.assertEqual(self.driven, ["live"])

    async def test_live_caller_queued_alone_is_served(self):
        await self.lock.acquire()
        r, w = await self._send("waiting")
        await self._queued(1)
        await asyncio.sleep(0.3)
        self.lock.release()
        raw = await asyncio.wait_for(r.read(), 15)
        w.close()
        self.assertIn(b" 200 ", raw.split(b"\r\n", 1)[0], raw[:300])
        self.assertEqual(self.driven, ["waiting"])

    async def test_unqueued_caller_is_served(self):
        r, w = await self._send("straight")
        raw = await asyncio.wait_for(r.read(), 15)
        w.close()
        self.assertIn(b" 200 ", raw.split(b"\r\n", 1)[0], raw[:300])
        self.assertEqual(self.driven, ["straight"])


if __name__ == "__main__":
    unittest.main()
