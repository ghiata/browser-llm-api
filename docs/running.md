# Running it

The server keeps one Chrome per provider, with its own signed-in profile in the checkout (`chatgpt_profile/`, `gemini_profile/`, never committed). Both sites block Chrome's headless mode, so a background run uses an invisible Xvfb display instead. Run commands from the checkout, because the profiles are found relative to it.

## The service

```bash
scripts/install-service.sh
```

That builds the venv, writes a `systemd --user` service and a watchdog timer for this checkout, starts both, and turns on lingering so they survive logout.

```bash
systemctl --user status browser-llm-api
systemctl --user restart browser-llm-api
journalctl --user -u browser-llm-api -f     # the journal is the log
```

Before a restart, check that nothing is in flight. A restart cuts any answer being read, and the caller still gets a 200 with a partial reply:

```bash
browser-llm health          # "idle" for every provider means it is safe
```

Settings for the service go in `.env` or in a systemd drop-in (`systemctl --user edit browser-llm-api`); see [Configuration](configuration.md).

`./serve.sh` runs the same thing in the foreground. It uses the venv, rebuilding it if needed, clears stale browser locks from a crash, and picks the display: the current `DISPLAY` when it works, else its own Xvfb.

## The display

ChatGPT draws images on a canvas, so it needs an X display; any Xvfb display works. The service runs on whatever `DISPLAY` its environment sets.

On a desktop machine, `scripts/mode.sh visible` runs the service's Chrome on your real display, `scripts/mode.sh headless` on a private Xvfb, and `scripts/mode.sh` shows which.

**Do not cover or move the service's Chrome windows.** Chrome treats a fully covered or off-screen window as hidden, and a hidden page gets no animation frames. Both sites then take the prompt and never render an answer. The server starts Chrome with `--disable-backgrounding-occluded-windows`, restores and raises a hidden window before each request, and fails a request that shows no sign of life within 60 seconds. Keep that flag.

## Signing in

Each provider needs its own login, stored in its own profile. Sign in with the helper, on a visible display:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/browser-llm login chatgpt      # or: gemini
systemctl --user start browser-llm-api
```

A Chrome window opens on the same profile the server uses; sign in, and it detects the session and closes itself. Use the helper, not your everyday Chrome: the automation's Chrome stores cookies with `--password-store=basic`, and a normal Chrome uses the system keyring, so neither can read the other's cookies.

## Watchdog

The installer also starts `browser-llm-health.timer`, which runs `scripts/healthcheck.sh` every 10 minutes. It checks three things that fail independently:

- Integrity: can the venv still import the dependencies? A deleted or broken venv does not take the API down at once: the running process holds its imports and keeps serving, so the damage stays invisible until the next restart. In the incident behind this check, that gap was ten days, and the restart became a crash loop. The watchdog rebuilds the venv while the service is still healthy.
- Liveness: does `/api/status` answer? If not, it restarts the service once and alerts only if that fails.
- Sign-in: once a day it sends one short, self-deleting question per provider. An expired login is the most common failure, and the server stays healthy while returning nothing, so nothing cheaper can detect it.

It is silent when healthy and logs to `~/.local/state/browser-llm-api/health.log`. Alerts are off by default; `BLM_ALERT_DIR` and `BLM_ALERT_CMD` send them somewhere, and `notify-send` is used when installed. Each kind of alert has a cooldown.

```bash
systemctl --user list-timers browser-llm-health.timer
scripts/healthcheck.sh --deep       # run it now, including the daily question
```

## Cleaning up the account

Every request opens a new conversation in the signed-in account, and on a busy install that is hundreds of threads a week in the account's own sidebar, burying the chats a person made there. So the server writes down each conversation it makes for a tool (curl, scripts, the MCP server, the widget on another site, a guest key) the moment the site creates it, in `conversations.json` under `BROWSER_LLM_STATE_DIR`. A conversation started from the dashboard is not written down: a person typed it, and deleting the chat in the dashboard deletes its conversations.

`browser-llm cleanup` deletes the written-down conversations that are at least an hour old, and nothing else. Chats a person made in the account itself are never touched, whatever they are called, and neither is a conversation still being answered. Saved images stay on disk. The installer writes a timer for it and turns it on when asked:

```bash
scripts/install-service.sh --cleanup
systemctl --user list-timers browser-llm-cleanup.timer
journalctl --user -u browser-llm-cleanup     # one line per run
browser-llm cleanup --dry-run                # what would go now
```

The timer runs every hour, up to five minutes late on purpose. The cleanup never makes a request wait. While anything is in flight the server answers 409 and the command tries again every 30 seconds; after 20 minutes it uses any free browser beside the request, and after 50 it leaves the rest for the next run. Between batches of 200 it stops when another request arrives. A delete the site keeps refusing is retried hourly for a day and then dropped from the list.

An ephemeral request still deletes its own conversation straight away and takes it off the list; the cleanup picks up the ones it misses, such as image requests, a request that timed out and a delete the site refused. A delete cannot be undone, and conversations made before the server kept this list are not covered: delete those from the dashboard, or by exact id through `POST /api/conversations/delete`.

## Troubleshooting

Read the page before guessing. Each provider's Chrome runs with a debugging port, which `pgrep -af 'user-data-dir=./chatgpt_profile'` shows. The page's `document.visibilityState`, its URL and its composer tell you more than an empty answer does.

| Symptom | Likely cause | What to do |
|---|---|---|
| Every answer empty, page shows a sign-in or "verify you're human" wall | the session expired | sign in again (above) |
| Every answer empty, page signed in, `visibilityState` is `hidden` | the window is covered or off screen | uncover it; keep the occlusion flag |
| `input box not found`, and the error says the page still holds an editable element | the site changed its page | update the selector lists in the provider; see [Contributing](../CONTRIBUTING.md#when-a-site-changes) |
| `input box not found`, and the page has no editable element | a sign-in wall, or the page did not load | sign in again, or check the network |
| Image responses report `image_tool: false` | the **+** menu or its Create image item moved | update the image-mode selectors in the ChatGPT provider |
| 429 with a reset time | the site's usage limit | wait for the reset |
| A long answer at GPT-6 High comes back empty | not yet explained (seen twice in October 2026) | read the log line "answer read failed"; retry at `GPT-6/medium` |
| The service will not start, "No module named nodriver" | a broken venv | `scripts/ensure-venv.sh` rebuilds it |
| Chrome will not start after a crash | a stale `SingletonLock` in the profile | `./serve.sh` clears it; otherwise delete `*_profile/Singleton*` |

The provider's browser restarts on its own when the connection to Chrome dies, so one dead browser fails at most one request.
