#!/bin/zsh
# Installs/restarts the launchd agent that serves the LLM Inbox web UI on http://localhost:8798. Idempotent.
set -e
HERE=~/Documents/agent-coding/llm-inbox
PLIST=~/Library/LaunchAgents/com.elityre.llm-inbox.plist
[ -x ~/Documents/agent-coding/shared/.venv/bin/python3 ] || { echo "run ../shared/install.sh first (creates the venv with the anthropic SDK)"; exit 1; }
cp "$HERE/com.elityre.llm-inbox.plist" "$PLIST"
launchctl bootout gui/$(id -u)/com.elityre.llm-inbox 2>/dev/null || true
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "LLM Inbox web UI: http://localhost:8798   log: ~/Library/Logs/llm-inbox-web.log"
