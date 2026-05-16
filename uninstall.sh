#!/usr/bin/env bash
set -euo pipefail
PLIST_NAME="com.junaidahmed.claude-discord.plist"
USER_LAUNCH_DIR="$HOME/Library/LaunchAgents"

launchctl bootout "gui/$(id -u)/com.junaidahmed.claude-discord" 2>/dev/null || true
rm -f "$USER_LAUNCH_DIR/$PLIST_NAME"
echo "LaunchAgent removed. The ~/.claude-discord directory and logs remain — delete manually if you want."
