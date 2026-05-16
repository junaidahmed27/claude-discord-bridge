#!/usr/bin/env bash
# Sets up the venv, installs the LaunchAgent, and starts the listener.
# Re-run any time you change the plist or update dependencies.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PLIST_NAME="com.junaidahmed.claude-discord.plist"
USER_LAUNCH_DIR="$HOME/Library/LaunchAgents"
LOG_DIR="$HOME/Library/Logs/claude-discord"

echo "==> Verifying config.json exists"
if [[ ! -f "$HERE/config.json" ]]; then
    echo "FATAL: $HERE/config.json is missing." >&2
    echo "Copy config.example.json → config.json and fill it in first." >&2
    exit 2
fi

echo "==> Creating Python venv (if needed)"
if [[ ! -d "$HERE/.venv" ]]; then
    /usr/bin/python3 -m venv "$HERE/.venv"
fi

echo "==> Installing dependencies"
"$HERE/.venv/bin/pip" install --quiet --upgrade pip
"$HERE/.venv/bin/pip" install --quiet -r "$HERE/requirements.txt"

echo "==> Creating log directory"
mkdir -p "$LOG_DIR"

echo "==> Installing LaunchAgent"
mkdir -p "$USER_LAUNCH_DIR"
cp "$HERE/$PLIST_NAME" "$USER_LAUNCH_DIR/$PLIST_NAME"

# Unload if already loaded, ignoring errors, then load fresh.
launchctl bootout "gui/$(id -u)/com.junaidahmed.claude-discord" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$USER_LAUNCH_DIR/$PLIST_NAME"
launchctl enable "gui/$(id -u)/com.junaidahmed.claude-discord"
launchctl kickstart -k "gui/$(id -u)/com.junaidahmed.claude-discord"

echo
echo "==> Done. Useful commands:"
echo
echo "  Tail logs:        tail -f $LOG_DIR/listener.out.log $LOG_DIR/listener.err.log"
echo "  Stop:             launchctl bootout gui/\$(id -u)/com.junaidahmed.claude-discord"
echo "  Start:            launchctl bootstrap gui/\$(id -u) $USER_LAUNCH_DIR/$PLIST_NAME"
echo "  Restart:          launchctl kickstart -k gui/\$(id -u)/com.junaidahmed.claude-discord"
echo "  Uninstall:        bash $HERE/uninstall.sh"
echo
