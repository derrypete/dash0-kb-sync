#!/usr/bin/env zsh
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$SCRIPT_DIR"
PLIST_NAME="com.dash0.kb.sync.plist"
TEMPLATE_PLIST="$PROJECT_DIR/com.dash0.kb.sync.plist.template"
SRC_PLIST="$PROJECT_DIR/$PLIST_NAME"
DEST_DIR="$HOME/Library/LaunchAgents"
DEST_PLIST="$DEST_DIR/$PLIST_NAME"

mkdir -p "$DEST_DIR" "$PROJECT_DIR/logs"
chmod +x "$PROJECT_DIR/scripts/run_daily_sync.sh"

# Generate local plist from template if template exists
if [[ -f "$TEMPLATE_PLIST" ]]; then
  sed -e "s|__PROJECT_DIR__|$PROJECT_DIR|g" \
      -e "s|__USER_HOME__|$HOME|g" \
      "$TEMPLATE_PLIST" > "$SRC_PLIST"
fi

# Validate plist syntax before installing
plutil -lint "$SRC_PLIST"

cp "$SRC_PLIST" "$DEST_PLIST"
chmod 644 "$DEST_PLIST"

USER_DOMAIN="gui/$(id -u)"

# Unload existing agent if already loaded
launchctl bootout "$USER_DOMAIN/com.dash0.kb.sync" 2>/dev/null || \
  launchctl unload "$DEST_PLIST" 2>/dev/null || true

# Bootstrap into current macOS GUI user domain
if launchctl bootstrap "$USER_DOMAIN" "$DEST_PLIST" 2>/dev/null; then
  launchctl enable "$USER_DOMAIN/com.dash0.kb.sync" 2>/dev/null || true
else
  launchctl load -w "$DEST_PLIST"
fi

echo "✓ Installed and enabled LaunchAgent: $DEST_PLIST"
echo "  Schedule: Daily at 03:00 local time (caffeinate -i; Sunday=--force-full, Mon-Sat=--incremental)"
echo "  Check status anytime with: launchctl print $USER_DOMAIN/com.dash0.kb.sync"
