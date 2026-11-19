# CLAUDE.md

Working instructions for this repository. History and per-feature reference detail moved word for word out of this guide on 12 November 2026 (cap 3,000 words): `docs/HISTORY.md`; read the part for the area you change. This guide wins where the two disagree.

## What this is

Drives a **chat web UI through an automated Chrome browser** ([`nodriver`](https://github.com/ultrafunkamsterdam/nodriver)) and exposes it as an **OpenAI-compatible HTTP API**: chat completions, image generation, image input (vision and image-to-image), and design tools built on ChatGPT's image tool. It uses a logged-in web session in a local Chrome profile, with **no official API key**. Prompts are typed into the page, input images go through the site's file picker, and answers are read back out of the DOM.

**This install's own facts** (drop-ins, LAN address, who decided what, where the clone lives) are in **`CLAUDE.local.md`**, which is gitignored and loaded alongside this file; read it before changing anything about the running service. This file stays generic, because the repository is public.

@CLAUDE.local.md

Two providers, chosen per request by the OpenAI `model` field: `chatgpt-browser` (chatgpt.com, profile `chatgpt_profile/`, images out and in, the default image provider) and `gemini-browser` (gemini.google.com, `gemini_profile/`, images out only if `IMAGE_PROVIDERS` names it, images in through the chooser). An unknown or absent `model` falls to `DEFAULT_PROVIDER` (default `chatgpt-browser`); `IMAGE_PROVIDERS` (comma list; unset = every provider that can draw) gates image requests, others get 403. This is **fragile**: each provider depends on its site's live DOM, and ChatGPT sits behind Cloudflare. A UI change can silently break extraction or submission.

**Scope (2.x).** One person's or team's signed-in sessions on Linux, one request at a time per browser (`BROWSER_LLM_LANES` gives ChatGPT up to eight browsers of the one account). `REMOTE_PROVIDERS` (forwarding a model to another install) was removed in 2.0: another machine calls the API directly with a key. Don't bring it back without a decision.

## Dashboard and widget

The dashboard is `browser_llm/webui/index.html` (single file, no build step) at `http://localhost:8081/`; four views by hash (`#chat`, `#studio`, `#gallery`, `#server`). Chat history lives in `localStorage` under `blm.chats` (`saveChats()` sheds image data first, never conversation text). An inline `<head>` script sets `documentElement.dataset.theme` **before first paint**: keep it there. Colours are tokens on `:root` and `:root[data-theme="dark"]`, never literal hex in a rule (`tests/test_dashboard.py` checks). Fonts are served from `/assets`, no CDN. Keep `[hidden]{display:none!important}` in the CSS (other rules set `display` and would show hidden elements). `md()` escapes first and marks fences with the plain-text `@@BLMCODE<n>@@`, never a NUL; every link and image address in an answer goes through `safeUrl()` (a `javascript:` link on the server's origin would reach the trusted history and delete endpoints). To work on the UI next to the service: `env -u DISPLAY BROWSER_LLM_PREWARM=0 BROWSER_LLM_PORT=<free port> ./venv/bin/browser-llm serve`, and mock `/v1/*` and the delete endpoints in the test browser.

The embeddable widget (`browser_llm/webui/widget.js`, served at `/widget.js`) is a Shadow-DOM chat bubble added with `<script src="http://<host>:8081/widget.js"></script>`; it finds its API base from its own script URL. Its `data-*` attributes and `window.BrowserLLMWidget` are listed in its header comment and docs/web-ui.md (`tests/test_widget.py` keeps them matching).

## Layout

```
browser_llm/    THE PACKAGE (relative imports). __init__.py loads .env FIRST (config.load_env): every module reads env at import time.
  server.py     FastAPI app, port 8081: router, completion loop, image persistence, attachment and local-path policy, CDP patch
  authz.py      stdlib-only access control (loopback + origin trust, key paths, guest_may)    guests.py  guest keys, ImageQuota
  ledger.py     ConversationLedger (tool conversations, for the cleanup)    lanes.py  LanePool, clone_login, PrivateDisplay
  providers/    base.py (Provider ABC, StreamMonitor, CompletionTracker, upload machinery, patch_cdp), chatgpt.py, gemini.py, limits.py
  design.py  gen_asset.py  client.py  mcp_server.py  login.py  cli.py (`browser-llm serve|login|ask|image|design|mcp|health|version|cleanup`)
  webui/        index.html, widget.js, assets/ (in the wheel)
scripts/        serve.sh (REFUSES to start without a venv), ensure-venv.sh, mcp.sh, healthcheck.sh, install-service.sh
deploy/systemd/ unit templates     desktop/  tray app     docs/  user docs     tests/  unit tests, no browser, no network
gen_asset.py design.py client.py serve.sh mcp.sh healthcheck.sh   FORWARDING FILES at the root: outside callers run them by path; do not delete or tidy them.
*_profile/      per-provider Chrome user-data dirs, gitignored. NEVER move or commit: moving one signs that account out.
```

`gen_asset.py` raises `GenAssetError` (code 3 = limit), never `SystemExit`: the MCP tool runs it on a worker thread, where `SystemExit` kills the thread silently. `mcp_server.py` carries protocol frames on stdout only: every log line goes to stderr.

**Tests:** `./venv/bin/python -m unittest discover -s tests`. Extend them when you change the done-decision logic, the attachment layer, or anything that decides who may read or delete an account's history or which conversations the cleanup may delete. Ruff selects E/F/W only. **Deps live in `./venv`** (build with `scripts/ensure-venv.sh`; use `./venv/bin/python -m pip`, not `./venv/bin/pip`). Needs Google Chrome, `xvfb`, Python 3.12.

## The `Provider` abstraction (`browser_llm/providers/base.py`)

Adding or altering a backend means editing a provider, not `server.py`. A provider is mostly declarative (`name`, `chat_url`, `profile_dir`, `input_selector`, `send_selectors`, `supports_images`, `supports_upload`, and so on) plus site-specific reads (`get_response_text`, `is_generating`, `image_status`, `get_images`, `logged_in`). The send, upload, stream and completion code is generic.

## How a request flows (`browser_llm/server.py`)

`get_provider(req.model)`; one persistent Chrome per provider (started lazily) and a per-provider `asyncio.Lock`; `_build_prompt()` flattens `messages` and returns `(prompt, specs)`; `_attachment_files()` materialises the specs; `open_and_send()` uploads, types and submits; `_stream_completion()` polls `get_response_text()`. Completion (`CompletionTracker`): an `<img>` stable for 4 s, or text unchanged for 2.5 s while not generating, or the give-up guards; nothing on the page for `NOT_STARTED_TIMEOUT` (60 s) raises `nostart`. The deadline is 420 s, extended to 900 s while the answer streams and to `BROWSER_LLM_THINKING_DEADLINE` (1500 s) while the page shows generating with no new text; a chat request's own `timeout` replaces the cap (`_request_caps`, 30 to 3600 s), a guest key's is ignored. A caller that gives up while queued still takes the browser when its turn comes (`request.is_disconnected()` always reads False behind the key middleware). The tab is **left open on purpose**: closing or navigating away destabilises the browser.

## Image input and generation

- Input wire formats: OpenAI vision parts, Anthropic image blocks, an `images: [...]` shorthand, `image`/`images` on `/v1/images/generations`, and `POST /v1/images/edits` (multipart needs `python-multipart`, else 400 with an install hint, or the same JSON). Specs may be a `data:` URL, bare base64, an `http(s)` URL (size-capped), `file://` or a local path; `MAX_ATTACHMENTS` 6, `MAX_ATTACHMENT_MB` 20. Caller paths are used in place and never deleted.
- **Local paths need a loopback and same-origin caller** (`_client_may_send_paths`), otherwise a keyed LAN client or any website open in the operator's browser could make the server upload any file to ChatGPT. `ALLOW_REMOTE_FILE_PATHS=1` opts out of both checks.
- Upload paths: an `<input type=file>` already in the DOM (verified by the attachment chip), then CDP file-chooser interception. Gemini keeps no input at rest and needs the chooser; check its menu-item labels first if a Gemini upload stops landing.
- **An input image must not look like a generated one:** `_isInput()` excludes anything inside the user turn, a `form`, a `file-tile` or `[data-composer-attachments]`. ChatGPT's "Analyzing image" shimmer returns `""` with `is_generating` True (`_PLACEHOLDER_RE`, text up to 48 characters).
- Images are stored as `<IMAGE_DIR>/<provider>/<provider>_<ts>_c<conv id>_<rand>.ext`, served at `/images/<provider>/<file>`, links from `BROWSER_LLM_PUBLIC_URL`; an unwritable dir skips saving. `POST /v1/images/generations` answers `{"created", "data":[{b64_json?, url?, path?, ...}], "model_used"}`; `n` is ignored, `size`, `background` and `model_variant` are honoured. **501** the provider cannot draw, **403** `IMAGE_PROVIDERS` leaves it out, **429** with `Retry-After` on a usage limit, **502** no image otherwise.
- Usage limits (`providers/limits.py`): `quota_refusal` raises when `image_gen.remaining <= CHATGPT_IMAGE_RESERVE`; `refusal_from_message` matches ChatGPT's refusal wording and works out the reset.

## Lanes (several browsers per provider)

`BROWSER_LLM_LANES=N` (default 1, at most 8) gives ChatGPT N browsers. `_lane(provider)` is the one way a request takes a lane and yields that lane's `Provider` (`LANES[name][i]`): every handler passes the yielded lane to `get_browser`, `run_chat` and `drive_once`, never the original. Lane 0 is the provider's own browser and profile; extra lanes start only when every running lane is busy and close after `BROWSER_LLM_LANE_IDLE_SECONDS`. `cold_ok=False` (history read and delete) never starts a lane; `wait=False` (the cleanup) never waits (`NoFreeLane`, HTTP 409). An extra lane has its own profile dir (`<name>_lane<N>_profile`, gitignored, never committed: it holds cookies) and its own Xvfb; `clone_login` copies lane 0's cookies. A new lane is checked with `_verify_lane_login` before it gets a request. Images take `_image_gates[name]` first (`BROWSER_LLM_IMAGE_CONCURRENCY`, default 1). Lanes share one account and its limits. Tests: `tests/test_lanes.py`, `tests/test_queued_leaver.py`.

## Design, ephemeral conversations

docs/images-and-design.md is the user guide. Design prompts are structural by default: no style words unless `--direction` (`tests/test_design.py` checks every kind). Consistency comes from reference images (each item a fresh chat with the first and previous item attached), not one long conversation. A set refuses when it does not fit the day's images unless `--partial`; a stopped set resumes.

`"ephemeral": true` deletes the conversation once the answer is read (unset falls back to `BROWSER_LLM_EPHEMERAL`, off). A failed delete never fails the request. Image generation is not covered: the hourly cleanup removes those threads. The MCP `ask` tool sets it by default (`keep_chat: true` opts out). **The dashboard sends `ephemeral: false`** on every chat completion; without it an install with `BROWSER_LLM_EPHEMERAL=1` would delete every dashboard chat on the site.

## Account history and cleanup

- The dashboard lists and opens conversations already in the provider account through in-page JS against the site's backend API (`providers/chatgpt.py`). **Titles first, bodies when opened**: importing all bodies at once draws a 429 and loses most. Paging stops on a short page. Gemini has no readable list (`/api/history` answers 501).
- A delete cascades: the local chat, its image files (`/api/gallery/delete`) and the account's conversations (`/api/conversations/delete`). Images carry their conversation in the filename (`_conv_stamp`), so the delete sweeps by stamp; never match older unstamped images by timestamp. **Bulk delete is one page call** (`delete_conversations`, 8 PATCHes in flight at most). **A delete cannot be undone**: build any bulk-delete list from exact conversation ids, never a time or title prefix. A 404 on delete means `already_gone`.
- `_gallery_target()` is the only thing allowed to name a file for unlink (strips the URL and `/images/` prefix, percent-decodes before the `..` check, resolves symlinks, needs a known image extension inside `IMAGE_DIR`; `tests/test_history.py` pins the refusals). These endpoints are same-origin only (`_require_trusted`).
- **The cleanup deletes what the ledger wrote down and nothing else.** `LEDGER` (`BROWSER_LLM_STATE_DIR/conversations.json`, mode 600) records each tool conversation as soon as the URL holds its id; recorded callers are every one except the dashboard (no `Origin`, another site's origin, `null`, every guest). `POST /api/conversations/cleanup` (`_require_trusted`, never images) deletes ids from `LEDGER.due(older_than)` minus `_live_conversations`; with `idle_only` it answers 409 unless nothing is busy or queued. `browser-llm cleanup` is what `browser-llm-cleanup.timer` runs hourly. **Never widen what it deletes** to titles, dates or "looks like a tool prompt": one-off sweeps use `/api/conversations/delete` with an exact id list and `keep_images: true`. Tests: `tests/test_cleanup.py`.

## Configuration and running

docs/configuration.md is the one table of every setting with its default; keep it complete when you add one. Settings may come from `.env` (config.py; the environment wins). Renamed settings still read old names through `config.env(new, *old)`; `REMOTE_PROVIDERS` and `REMOTE_API_KEY` were removed in 2.0. `serve.sh` exports the same `DEFAULT_PROVIDER` default as the code: keep them equal. `BROWSER_LLM_PREWARM=0` starts no browser (second instance, tests).

```bash
./serve.sh                                     # foreground → http://localhost:8081/v1
./venv/bin/browser-llm health                  # one-screen state; every provider "idle" = safe to restart
scripts/install-service.sh                     # background (systemd --user): venv, generated units, linger
journalctl --user -u browser-llm-api -f        # logs live in the journal, NOT server.log
systemctl --user list-timers browser-llm-health.timer    # the watchdog, every 10 min
scripts/healthcheck.sh --deep                  # by hand, including the model ping
tail ~/.local/state/browser-llm-api/health.log # silent when healthy
BROWSER_LLM_PREWARM=0 BROWSER_LLM_PORT=8097 ./venv/bin/browser-llm serve   # a second server that opens no browser
```

**Before any restart, read `/api/status` and don't go ahead while any provider is busy**: a restart cuts the request in flight. The service runs this working tree, so a restart also loads whatever is on disk: make a provider change in one write, or in a worktree.

## Authentication: the #1 failure mode

Each provider needs its own login (separate profile). A sign-in or "verify you're human" wall means that provider's session expired; **empty answers alone do not prove that** (a hidden page also answers nothing). Read the page first (its CDP port is in `pgrep -af 'user-data-dir=./chatgpt_profile'`; `document.visibilityState`, the URL and the composer tell more than the answer). Re-auth on a real display only when the page shows a sign-in:

```bash
systemctl --user stop browser-llm-api
DISPLAY=:1 ./venv/bin/browser-llm login gemini   # or: chatgpt — visible Chrome opens; sign in; auto-closes
systemctl --user start browser-llm-api
```

Use the login helper, not a normal Chrome: `nodriver` launches Chrome with `--password-store=basic`, a normal Chrome uses the system keyring, and cookies written by one cannot be decrypted by the other.

## Access from other machines (API key)

**This install listens on 127.0.0.1:8081 only** (`BROWSER_LLM_HOST=127.0.0.1`, since 12 November 2026, the owner's decision). Share it on the LAN (`BROWSER_LLM_HOST=0.0.0.0` with an API key) only on the owner's word. The guest access once given to another party has ended: no guest key is in use. Never write a key value or a key file's path in this guide.

- Localhost is always unauthenticated. With `BROWSER_LLM_API_KEY` set, non-loopback clients need the key on `/v1/*` and `/api/*`; pages and assets (`/`, `/ui`, `/widget.js`, `/demo`, `/version`, `/images/*`) stay public. CORS preflights are exempt. Helpers are in `browser_llm/authz.py`; the key middleware is registered **after** CORSMiddleware so it runs before it. `BROWSER_LLM_GUEST_KEYS` also locks the LAN out when no owner key is set.
- **Give other people a guest key, never the owner's** (`BROWSER_LLM_GUEST_KEYS`, guests.py): the owner's key passes `_require_trusted`, so its holder can list, read and delete the account's whole history and the gallery. A guest key is checked first, from any address, against an allowlist (`authz.guest_may`); its images are reserved before queueing and at zero its chat gets `_WORDS_ONLY`; its drives run ephemeral and `/api/status` drops telemetry for it.
- **A leaked key:** change `BROWSER_LLM_API_KEY`, restart once nothing is in flight, and block the address at the firewall; saved images carry their conversation in the filename, so a caller's images can be traced and deleted. The web UI takes the key once at `http://<host>:8081/#key=<key>`; the widget takes `data-key`; the CLI and `client.ask` take `BROWSER_LLM_API_KEY`.

## Gotchas and conventions

**Security**
- **Loopback is NOT trusted.** CORS is `allow_origins=["*"]` on purpose (the widget), so any website open in the operator's browser can POST here from `127.0.0.1`. `authz.origin_is_trusted(origin, host)` trusts no `Origin` (curl, CLI, desktop) or one matching the addressed host; all three attachment endpoints use it. **Don't simplify the path policy back to a loopback check.** `Origin: null` is not a missing Origin (a sandboxed `srcdoc` iframe sends it): it falls through to the host comparison and fails it.

**Browser and tabs**
- **One tab per profile, always.** `session_tab()` reuses a tab on the site's origin or navigates the first target. A second live chatgpt.com tab makes the next drive read 0 characters to the deadline. A tab pool cannot run two ChatGPT conversations in one Chrome (tried and reverted); concurrency is one browser per lane.
- **The lane is taken inside the SSE generator** in `chat_completions` (`async with _lane(provider)`): FastAPI runs the generator after the handler returns. Streaming failures go in-band as a `[browser-llm error: …]` chunk.
- **Non-headless is mandatory:** the sites block headless Chrome. Background = Xvfb, never `--headless`. Image generation works under Xvfb with the SwiftShader GL flags in `CHROME_ARGS`; if canvas generation stalls, run on a real display (`DISPLAY=:1 ./serve.sh`).
- **A hidden page answers nothing, on both providers:** an off-screen or covered window reads `visibilityState == "hidden"` and the sites drop the typed prompt, so a drive returns an empty 200. Layers: `--disable-backgrounding-occluded-windows` in `CHROME_ARGS`, `ensure_visible()`, and `nostart`. **Don't move or cover the service windows on its display, and don't remove the flag.**
- **A dead browser must not wedge a provider:** `_browser_alive()` in `get_browser()` replaces a dead cached browser; prove a change here by killing Chrome mid-session and checking the next request heals. Call `patch_cdp()` (in `base.py`) in any new entry point.
- **nodriver:** `page.evaluate()` cannot return a DOM element: use `eval_handle()` and pass the `object_id` to `cdp.dom.set_file_input_files`. Handler removal is `page.remove_handler` (singular). After a crash, delete `*_profile/Singleton*` (`serve.sh` clears them on start).

**Uploads**
- File-chooser interception stays ON for the tab's life. Attachment temps must outlive the upload (`_attachment_files()` wraps the whole drive). Never submit while an upload is in flight.

**ChatGPT's page**
- **Every selector is a LIST** holding the old and new layout, at the top of `providers/chatgpt.py`. When the page is redesigned (every request fails at "input box not found"), `Provider.composer_hint()` says whether it is a redesign or a sign-in wall: read the live page over CDP, measure each selector on a real turn, a code answer and an upload, and add the new layout to each list rather than replacing it. A generating signal that sticks holds every request to the deadline: add one only after watching it clear.
- **"Create image" mode is a pill inside the editor**: the order in `prepare_page` is empty the composer, choose the tool, attach, then `type_prompt`; a text request clears any pill first.
- The model picker needs real CDP mouse events (`_click_xy`). What answered comes from the conversation record (`metadata.model_slug`), not the picker.

**Gemini:** image generation failed for the account tested (account or region, not code); an install can refuse it with `IMAGE_PROVIDERS=chatgpt-browser` (403).

**Service, venv and logs**
- The service unit is generated, not committed (`scripts/install-service.sh` fills `deploy/systemd/browser-llm-api.service.template`); no paths are hardcoded in the repo. **Never reinstate a system-python fallback in `serve.sh`**: system Python can never hold nodriver, so it only crash-loops. `healthcheck.sh` probes venv integrity and liveness every 10 minutes; what it cannot repair raises a `critical` alert.
- The watchdog's daily deep check spends one real turn per provider, on purpose: keep it one short ephemeral completion.
- `usage` token counts are fake (`.split()` word counts). Importing `server.py` from the repo directory truncates the live `server.log`: import it from elsewhere.

## Git

Default branch **`master`** (no `main`), remote `origin` → https://github.com/StaticB1/browser-llm-api. Never commit `*_profile/`, `*.log`, `gemini_research_data.txt` or `__pycache__/`. Releases follow docs/releasing.md.
