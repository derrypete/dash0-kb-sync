#!/usr/bin/env zsh
set -euo pipefail

PLIST_NAME="com.dash0.kb.sync.plist"
DEST_DIR="$HOME/Library/LaunchAgents"
DEST_PLIST="$DEST_DIR/$PLIST_NAME"
USER_DOMAIN="gui/$(id -u)"

if [[ -f "$DEST_PLIST" ]]; then
  echo "Unloading LaunchAgent: $DEST_PLIST"
  launchctl bootout "$USER_DOMAIN/com.dash0.kb.sync" 2>/dev/null || \
    launchctl unload "$DEST_PLIST" 2>/dev/null || true
  rm -f "$DEST_PLIST"
  echo "✓ Successfully uninstalled $DEST_PLIST"
else
  echo "No installed LaunchAgent found at $DEST_PLIST"
fi
