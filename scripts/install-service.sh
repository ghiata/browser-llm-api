#!/usr/bin/env bash
# Portable installer: set up a venv, install deps, and register an always-on
# systemd --user service that runs THIS clone (no hardcoded paths — the unit is
# generated from the template with the real install directory).
#
# After this the API is at http://localhost:8081 and auto-starts / auto-restarts
# (survives logout via linger).
#
#   scripts/install-service.sh            the service and its watchdog
#   scripts/install-service.sh --cleanup  also the hourly timer that deletes the
#                                         conversations the server made on the
#                                         account (docs/running.md)
set -euo pipefail
CLEANUP=0
for arg in "$@"; do
  case "$arg" in
    --cleanup) CLEANUP=1 ;;
    *) echo "usage: $0 [--cleanup]" >&2; exit 2 ;;
  esac
done
# This script lives in scripts/; DIR is the checkout root the units run from.
DIR="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
cd "$DIR"

# Fill a unit template with this checkout's path. Documentation= is a list of
# addresses split on spaces, so the path goes in it with each space as %%20
# (systemd reads %% as one %); every other line takes the path as it is.
fill() {
  sed -e "s#^Documentation=file://__INSTALL_DIR__#Documentation=file://${DIR// /%%20}#" \
      -e "s#__INSTALL_DIR__#$DIR#g" "$1"
}

# 1) venv + dependencies. ensure-venv.sh owns this so the installer, serve.sh,
# mcp.sh and the watchdog all build the venv the same way.
echo "[install] checking the venv…"
./scripts/ensure-venv.sh

# 2) generate the systemd --user unit for THIS machine/clone
mkdir -p ~/.config/systemd/user
UNIT="$HOME/.config/systemd/user/browser-llm-api.service"
fill deploy/systemd/browser-llm-api.service.template > "$UNIT"
echo "[install] wrote $UNIT (WorkingDirectory=$DIR)"

# 3) the watchdog. Without it a broken venv is invisible until the next restart
# — which once meant ten days of a doomed service and then a silent crash loop.
fill deploy/systemd/browser-llm-health.service.template \
  > "$HOME/.config/systemd/user/browser-llm-health.service"
cp deploy/systemd/browser-llm-health.timer "$HOME/.config/systemd/user/browser-llm-health.timer"

# The account cleanup. Written always, enabled only on request: it deletes
# conversations on the signed-in account (only the ones the server made).
fill deploy/systemd/browser-llm-cleanup.service.template \
  > "$HOME/.config/systemd/user/browser-llm-cleanup.service"
cp deploy/systemd/browser-llm-cleanup.timer "$HOME/.config/systemd/user/browser-llm-cleanup.timer"

# 4) enable + start, and keep it running after logout
systemctl --user daemon-reload
systemctl --user enable --now browser-llm-api.service
systemctl --user enable --now browser-llm-health.timer
if [ "$CLEANUP" = 1 ]; then
  systemctl --user enable --now browser-llm-cleanup.timer
fi
loginctl enable-linger "$(id -un)" >/dev/null 2>&1 || true

echo
echo "[install] up at http://localhost:8081   (model: chatgpt-browser, the default | gemini-browser, text only)"
systemctl --user --no-pager status browser-llm-api.service | head -6 || true
echo
echo "  logs:    journalctl --user -u browser-llm-api -f"
echo "  restart: systemctl --user restart browser-llm-api"
echo "  re-auth: DISPLAY=:1 ./venv/bin/browser-llm login chatgpt   # or: gemini"
echo "  health:  scripts/healthcheck.sh --deep   (timer runs it every 10 min)"
if [ "$CLEANUP" = 1 ]; then
  echo "  cleanup: ./venv/bin/browser-llm cleanup --dry-run   (timer runs it hourly)"
else
  echo "  cleanup: off; scripts/install-service.sh --cleanup turns on the hourly account cleanup"
fi
