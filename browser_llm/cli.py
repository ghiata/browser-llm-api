"""The ``browser-llm`` command: one entry point for every tool in the package.

    browser-llm serve                    run the API server (what the service runs)
    browser-llm login chatgpt|gemini     sign in once per provider, in a visible window
    browser-llm ask "prompt" [options]   one chat completion; --image FILE attaches images
    browser-llm image --prompt ... --out FILE [options]    one image asset
    browser-llm design kinds|prompt|set|tokens|review ...  designs as images
    browser-llm mcp                      the MCP stdio server for agent clients
    browser-llm health                   the running server's state, in short
    browser-llm cleanup [options]        delete the conversations this server made
    browser-llm version

``serve`` and ``login`` use the browser profiles in the current directory, so
run them from the checkout (the service does). Each subcommand takes the same
options as the script it replaces; ``browser-llm <command> --help`` lists them.
"""
import json
import os
import sys
import urllib.error
import urllib.request

from ._version import __version__

COMMANDS = {
    "serve": "run the API server",
    "login": "sign in to a provider in a visible browser window",
    "ask": "send one chat completion",
    "image": "make one image asset (resize, crop, favicon, transparency)",
    "design": "design sections, screens and brand images; tokens; review",
    "mcp": "run the MCP stdio server",
    "health": "show the running server's state",
    "cleanup": "delete the conversations this server made, once an hour old",
    "version": "print the version",
}


def _usage() -> str:
    width = max(map(len, COMMANDS))
    lines = [f"browser-llm {__version__}: an OpenAI-compatible API over signed-in "
             f"ChatGPT and Gemini sessions", "", "usage: browser-llm <command> [options]", ""]
    lines += [f"  {name:<{width}}  {text}" for name, text in COMMANDS.items()]
    lines += ["", "browser-llm <command> --help shows that command's options."]
    return "\n".join(lines)


def _run_main(module, prog, rest, *, takes_argv=False):
    """Run a module's main() as if it were its own script."""
    if takes_argv:
        return module.main(rest)
    saved = sys.argv
    sys.argv = [prog] + rest
    try:
        return module.main()
    finally:
        sys.argv = saved


def health(base=None) -> int:
    base = _api_base(base)
    req = urllib.request.Request(base + "/api/status")
    key = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            st = json.load(r)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"unreachable: {base} ({e})", file=sys.stderr)
        return 1
    print(f"browser-llm {st.get('version')} at {base}, up {int(st.get('uptime_seconds') or 0)} s, "
          f"default {st.get('default_provider')}, images from "
          f"{', '.join(st.get('image_providers') or []) or 'none'}")
    for name, p in (st.get("providers") or {}).items():
        quota = p.get("image_quota") or {}
        bits = ["browser up" if p.get("browser_running") else "browser not started",
                "busy" if p.get("busy") else "idle",
                f"{p.get('requests', 0)} requests, {p.get('errors', 0)} errors"]
        if (p.get("lanes_total") or 1) > 1:
            lane = f"{p.get('lanes_busy', 0)} of {p['lanes_total']} lanes busy, {p.get('lanes_running', 0)} running"
            if p.get("queued"):
                lane += f", {p['queued']} waiting"
            bits.insert(2, lane)
        if p.get("model_setting"):
            bits.append(f"model {p['model_setting']}")
        if quota.get("remaining") is not None:
            bits.append(f"site counter {quota['remaining']} images")
        if p.get("last_error"):
            bits.append(f"last error: {str(p['last_error'])[:80]}")
        print(f"  {name}: " + "; ".join(bits))
    return 0


def _api_base(base=None) -> str:
    port = os.environ.get("BROWSER_LLM_PORT", "").strip() or "8081"
    return (base or os.environ.get("BROWSER_LLM_API") or f"http://localhost:{port}").rstrip("/")


def _post_json(url: str, body: dict, timeout: float = 600):
    """POST JSON; returns (status, parsed body). Raises URLError/OSError when
    the server cannot be reached."""
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"})
    key = os.environ.get("BROWSER_LLM_API_KEY", "").strip()
    if key:
        req.add_header("Authorization", f"Bearer {key}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.load(e)
        except ValueError:
            return e.code, {"detail": str(e)}


def cleanup(argv, *, post=_post_json, sleep=None, clock=None) -> int:
    """Ask the running server to delete the conversations it made (its ledger)
    that are older than --older-than minutes. What the hourly timer runs.

    The server never waits for a browser: it answers 409 while it is working.
    This retries every --retry seconds, first waiting for the provider to be
    idle, then after --busy-after minutes taking any free browser, and gives up
    after --wait minutes (the next hour's run picks the rest up). Exit 0 when
    the run finished or gave up because the server stayed busy, 1 on an error.
    """
    import argparse
    import time
    sleep = sleep or time.sleep
    clock = clock or time.monotonic
    ap = argparse.ArgumentParser(prog="browser-llm cleanup", description=cleanup.__doc__.split("\n\n")[0])
    ap.add_argument("--older-than", type=float, default=60, metavar="MIN",
                    help="only conversations made at least this many minutes ago (default 60)")
    ap.add_argument("--model", default="chatgpt-browser", help="provider (default chatgpt-browser)")
    ap.add_argument("--dry-run", action="store_true", help="say what would go; delete nothing")
    ap.add_argument("--wait", type=float, default=50, metavar="MIN",
                    help="give up after this many minutes of a busy server (default 50)")
    ap.add_argument("--busy-after", type=float, default=20, metavar="MIN",
                    help="after this many minutes, run beside a request on a free browser (default 20)")
    ap.add_argument("--retry", type=float, default=30, metavar="SEC",
                    help="seconds between tries while the server is busy (default 30)")
    ap.add_argument("--api", default=None, help="server address (default $BROWSER_LLM_API or http://localhost:8081)")
    a = ap.parse_args(argv)
    url = _api_base(a.api) + "/api/conversations/cleanup"
    start = clock()
    unreachable = 0
    while True:
        waited = (clock() - start) / 60
        body = {"model": a.model, "older_than": max(0.0, a.older_than) * 60,
                "dry_run": a.dry_run, "idle_only": waited < a.busy_after}
        try:
            status, data = post(url, body)
            unreachable = 0
        except (urllib.error.URLError, OSError, ValueError) as e:
            # A restart takes a few seconds; anything longer is a real fault.
            unreachable += 1
            if unreachable >= 5 or waited >= a.wait:
                print(f"cleanup: {url} unreachable ({e})", file=sys.stderr)
                return 1
            sleep(a.retry)
            continue
        if status == 409:
            if waited >= a.wait:
                print(f"cleanup: the server stayed busy for {a.wait:.0f} min; "
                      f"left for the next run ({data.get('detail', '')})", file=sys.stderr)
                return 0
            sleep(a.retry)
            continue
        if status != 200:
            print(f"cleanup: HTTP {status}: {data.get('detail', data)}", file=sys.stderr)
            return 1
        mins = f"{a.older_than:g} min"
        if data.get("dry_run"):
            print(f"cleanup {data.get('model')}: would delete {data.get('due', 0)} recorded "
                  f"conversation(s) older than {mins} ({data.get('recorded', 0)} recorded in all)")
            for cid in data.get("ids") or []:
                print(f"  {cid}")
            return 0
        line = (f"cleanup {data.get('model')}: deleted {len(data.get('deleted') or [])} of "
                f"{data.get('due', 0)} recorded conversation(s) older than {mins}")
        if data.get("already_gone"):
            line += f", {len(data['already_gone'])} already gone"
        if data.get("failed"):
            line += f", {len(data['failed'])} failed (tried again next run)"
        if data.get("stopped"):
            line += f"; stopped early: {data['stopped']}"
        print(line)
        return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        print(_usage())
        return 0
    cmd, rest = args[0], args[1:]
    prog = f"browser-llm {cmd}"
    if cmd in ("version", "--version", "-V"):
        print(__version__)
        return 0
    if cmd == "serve":
        from . import server
        return _run_main(server, prog, rest) or 0
    if cmd == "login":
        from . import login
        return login.cli(rest, prog=prog)
    if cmd == "ask":
        from . import client
        return _run_main(client, prog, rest) or 0
    if cmd == "image":
        from . import gen_asset
        return _run_main(gen_asset, prog, rest) or 0
    if cmd == "design":
        from . import design
        return design.main(rest, prog=prog)
    if cmd == "mcp":
        from . import mcp_server
        return _run_main(mcp_server, prog, rest) or 0
    if cmd == "health":
        return health(rest[0] if rest else None)
    if cmd == "cleanup":
        return cleanup(rest)
    print(f"browser-llm: unknown command {cmd!r}\n\n{_usage()}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
