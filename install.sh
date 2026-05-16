#!/usr/bin/env bash
# Sets up the venv, renders the LaunchAgent plist for the current user, and
# starts the listener. Re-run any time you change the template or update deps.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TEMPLATE="$HERE/com.claude-discord-bridge.plist.template"
LABEL="local.claude-discord-bridge"
USER_LAUNCH_DIR="$HOME/Library/LaunchAgents"
PLIST_NAME="$LABEL.plist"
PLIST_OUT="$USER_LAUNCH_DIR/$PLIST_NAME"
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

echo "==> Rendering plist for HOME=$HOME"
mkdir -p "$USER_LAUNCH_DIR"
# Substitute __HOME__ with the runtime value; sed is fine because $HOME is a
# real path with no characters that need escaping in our usage.
sed "s|__HOME__|$HOME|g" "$TEMPLATE" > "$PLIST_OUT"

# Clean up the older personalized service if upgrading from an earlier install.
OLD_LABEL="com.junaidahmed.claude-discord"
OLD_PLIST="$USER_LAUNCH_DIR/$OLD_LABEL.plist"
if [[ -f "$OLD_PLIST" ]]; then
    echo "==> Removing legacy service $OLD_LABEL"
    launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
    rm -f "$OLD_PLIST"
fi

echo "==> Loading $LABEL"
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_OUT"
launchctl enable "gui/$(id -u)/$LABEL"
launchctl kickstart -k "gui/$(id -u)/$LABEL"

echo
echo "==> Done. Useful commands:"
echo
echo "  Tail logs:        tail -f $LOG_DIR/listener.out.log $LOG_DIR/listener.err.log"
echo "  Stop:             launchctl bootout gui/\$(id -u)/$LABEL"
echo "  Start:            launchctl bootstrap gui/\$(id -u) $PLIST_OUT"
echo "  Restart:          launchctl kickstart -k gui/\$(id -u)/$LABEL"
echo "  Uninstall:        bash $HERE/uninstall.sh"
echo
