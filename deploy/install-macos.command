#!/bin/zsh
set -euo pipefail
PLIST="/Users/ck/Library/LaunchAgents/com.ck.scribble-mcp.vault-api.plist"
DOMAIN="gui/$(id -u)"
LABEL="com.ck.scribble-mcp.vault-api"

launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"
launchctl print "$DOMAIN/$LABEL" > /tmp/scribble-mcp-launchctl-print.txt
curl -sS -m 10 -i http://100.90.117.53:8765/health > /tmp/scribble-mcp-health.txt
printf 'Vault API macOS LaunchAgent installed and verified.\n'
printf 'Readback saved to /tmp/scribble-mcp-launchctl-print.txt and /tmp/scribble-mcp-health.txt\n'
