"""The conversations this server made on a provider's account, kept so they
can be deleted later. Stdlib only, so tests import it without a browser.

Every drive opens a new conversation on the site. An ephemeral request deletes
its own as soon as the answer is read, but a lot of drives leave theirs behind:
image requests (never ephemeral), a request that times out or fails, a caller
that hangs up, a delete the site refused. On a busy install that is hundreds of
threads a week in the person's own sidebar, burying the chats they made
themselves. The account cannot tell those threads apart from the person's own
afterwards, so the server writes each id down the moment the site creates the
conversation, and the hourly cleanup (``POST /api/conversations/cleanup``)
deletes what is written here and nothing else.

What is NOT written here: a conversation started from the dashboard, which a
person typed and the dashboard keeps its own copy of. A conversation removed by
its ephemeral request comes off the list straight away.

The file is JSON, ``{"conversations": {id: {at, provider, kind, attempts}}}``,
replaced atomically on every change. It stays small: an entry lives about an
hour before the cleanup deletes the conversation and drops it.
"""
import json
import logging
import os
import threading
import time
from pathlib import Path
from typing import Callable, Iterable, Optional

logger = logging.getLogger(__name__)

# A conversation the site keeps refusing to delete (a 5xx every hour) is given
# up on after this many tries, so one bad id cannot sit in the list for good.
# A day of hourly runs.
MAX_ATTEMPTS = 24


class ConversationLedger:
    def __init__(self, path: Path, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self._clock = clock
        self._lock = threading.Lock()
        self._data: Optional[dict] = None

    # -- storage ------------------------------------------------------------
    def _load(self) -> dict:
        if self._data is None:
            try:
                raw = json.loads(self.path.read_text())
                convs = raw.get("conversations") if isinstance(raw, dict) else None
                self._data = {k: v for k, v in (convs or {}).items()
                              if isinstance(k, str) and isinstance(v, dict)}
            except FileNotFoundError:
                self._data = {}
            except Exception as e:
                # A damaged file must not stop requests. Starting empty loses
                # the record of a few tool chats, which then stay in the
                # sidebar; it never deletes anything that was not ours.
                logger.warning(f"conversation record unreadable ({e}); starting from empty")
                self._data = {}
        return self._data

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"conversations": self._load()}, indent=0))
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except Exception as e:
            logger.warning(f"could not save the conversation record to {self.path}: {e}")

    # -- writing ------------------------------------------------------------
    def note(self, conv_id: str, *, provider: str, kind: str = "chat") -> None:
        """Write down a conversation this server just made. Noting it twice
        keeps the first time, so its hour counts from when it was made."""
        conv_id = (conv_id or "").strip()
        if not conv_id:
            return
        with self._lock:
            data = self._load()
            if conv_id in data:
                return
            data[conv_id] = {"at": round(self._clock(), 1), "provider": provider,
                             "kind": kind, "attempts": 0}
            self._save()

    def forget(self, ids: Iterable[str]) -> int:
        """Take conversations off the list (deleted, or already gone)."""
        with self._lock:
            data = self._load()
            gone = [i for i in ids if i in data]
            for i in gone:
                del data[i]
            if gone:
                self._save()
            return len(gone)

    def failed(self, ids: Iterable[str]) -> list[str]:
        """Count a failed delete against each id. Returns the ids given up on
        (MAX_ATTEMPTS reached), which are taken off the list."""
        dropped = []
        with self._lock:
            data = self._load()
            for i in ids:
                entry = data.get(i)
                if entry is None:
                    continue
                entry["attempts"] = int(entry.get("attempts") or 0) + 1
                if entry["attempts"] >= MAX_ATTEMPTS:
                    del data[i]
                    dropped.append(i)
            self._save()
        return dropped

    # -- reading ------------------------------------------------------------
    def due(self, older_than: float, provider: Optional[str] = None) -> list[str]:
        """Ids made at least ``older_than`` seconds ago, oldest first. A
        conversation a job may still be continuing is younger than that."""
        cutoff = self._clock() - max(0.0, float(older_than))
        with self._lock:
            items = [(v.get("at") or 0.0, k) for k, v in self._load().items()
                     if (provider is None or v.get("provider") == provider)
                     and float(v.get("at") or 0.0) <= cutoff]
        return [k for _, k in sorted(items)]

    def __contains__(self, conv_id: str) -> bool:
        with self._lock:
            return conv_id in self._load()

    def __len__(self) -> int:
        with self._lock:
            return len(self._load())
