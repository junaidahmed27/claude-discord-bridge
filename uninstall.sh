#!/usr/bin/env bash
set -euo pipefail
LABEL="local.claude-discord-bridge"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"

# Also clean up the older personalized service if it's still around.
OLD_LABEL="com.junaidahmed.claude-discord"
launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
rm -f "$HOME/Library/LaunchAgents/$OLD_LABEL.plist"

echo "LaunchAgent removed. The ~/.claude-discord directory and logs remain — delete manually if you want."
