#!/bin/zsh
# Installs/restarts the launchd agent that serves the LLM Inbox web UI on http://localhost:8798. Idempotent.
set -e
HERE=~/Documents/agent-coding/llm-inbox
PLIST=~/Library/LaunchAgents/com.elityre.llm-inbox.plist
LABEL=gui/$(id -u)/com.elityre.llm-inbox
[ -x ~/Documents/agent-coding/shared/.venv/bin/python3 ] || { echo "run ../shared/install.sh first (creates the venv with the anthropic SDK)"; exit 1; }
# The port the server will use: WEB_PORT in config.env, else 8798 (same rule as server.py).
PORT=$(sed -n 's/^[[:space:]]*WEB_PORT[[:space:]]*=[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p' ~/Library/Application\ Support/LLMInbox/config.env 2>/dev/null | tail -1)
PORT=${PORT:-8798}
cp "$HERE/com.elityre.llm-inbox.plist" "$PLIST"
launchctl bootout $LABEL 2>/dev/null || true
# bootout returns before the old agent is gone, and bootstrap fails ("Input/output error") until it is.
for i in {1..50}; do launchctl print $LABEL >/dev/null 2>&1 || break; sleep 0.2; done
# A server started by hand (or left over from an interrupted install) holds the port; stop it by PID, never by name.
for PID in $(lsof -tnP -iTCP:$PORT -sTCP:LISTEN 2>/dev/null); do kill "$PID" && echo "stopped stray server pid $PID"; done
for i in {1..25}; do lsof -tnP -iTCP:$PORT -sTCP:LISTEN >/dev/null 2>&1 || break; sleep 0.2; done
launchctl bootstrap gui/$(id -u) "$PLIST"
echo "LLM Inbox web UI: http://localhost:$PORT   log: ~/Library/Logs/llm-inbox-web.log"
