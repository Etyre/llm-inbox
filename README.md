# LLM Inbox (web UI)

A local web page for reading the messages that models send to Eli from his apps, and talking back to the same model. Built 2026-11-07 as the front end for the shared LLM inbox (`../shared/README.md`), after the terminal-only `llm-inbox debrief` turned out to be the wrong shape for an actual conversation.

Open http://localhost:8798 . The inbox is on the left; a message, the context it was sent from, and the conversation with its model are on the right.

## How it works

- `server.py` is a Python standard-library HTTP server. It reads the same files as the `llm-inbox` command: `messages.jsonl` for the inbox and `debriefs/<id>.jsonl` for conversations, so the two front ends never disagree.
- Opening a message shows what the model said, the system prompt and conversation it was answering, and every debrief turn so far.
- A reply is posted to `/api/messages/<id>/reply`. The server replays the original system prompt, input and response to the same model (with the `contact_eli` tool so the history is valid), adds Eli's turn, and streams the answer back over Server-Sent Events. The first turn is prefaced with a short introduction saying this is Eli and the app's task is over. Pending tool calls in the stored response get `tool_result` blocks automatically.
- Both turns are appended to the transcript only after the answer completes, so a failed request writes nothing and the page keeps the draft.
- The reply box is a draft: saved to `localStorage` on every keystroke, keyed by message id, restored before the box is shown, cleared only when the model has answered.
- Notifications from the shared helper end with a link straight to the message: `http://localhost:8798/#<id>`.
- A launchd agent keeps the server running and starts it at login. It uses the Python in `../shared/.venv`, which has the anthropic SDK.

## Files

| File | Purpose |
|---|---|
| `server.py` | the server, the API, and the `--test` self-test |
| `static/index.html`, `static/app.js`, `static/app.css` | the page |
| `com.elityre.llm-inbox.plist` | launchd agent definition |
| `install.sh` | copies the plist into `~/Library/LaunchAgents` and (re)starts the agent |

## Runtime locations

- Inbox and transcripts: `~/Library/Application Support/LLMInbox/` (shared with the CLI)
- Config: `~/Library/Application Support/LLMInbox/config.env`; `WEB_PORT` overrides 8798, `WEB_URL` the link put in notifications
- Log: `~/Library/Logs/llm-inbox-web.log` (plus launchd's `.out.log` / `.err.log` beside it)
- API key: `ANTHROPIC_API_KEY`, else the Keychain item Task Tracker uses (service `TaskTracker`, account `anthropic`)

## Common tasks

```sh
./install.sh                                  # install or restart the agent
../shared/.venv/bin/python3 server.py --test  # self-test: fake model, temporary inbox, every route (no API spend)
../shared/.venv/bin/python3 server.py --port 8799   # run by hand on another port
tail -f ~/Library/Logs/llm-inbox-web.log
launchctl bootout gui/$(id -u)/com.elityre.llm-inbox   # stop it
```

## Settings that matter

- Replies cost API tokens at the sending model's rate, with the whole original context replayed each turn.
- Enter sends, Shift+Enter inserts a newline. "Mark handled" hides a message from the default list; "show handled" brings them back.

## Gotchas learned

- A stored response that ends in a `tool_use` block (the model called `contact_eli`) must be followed by a `tool_result` before the model can be addressed again, or the API rejects the request. `debrief_setup` / `user_turn` in `../shared/llm_inbox.py` handle this for both front ends.
- The server resolves the API key lazily, so it starts (and serves the inbox) even when no key is available; only sending a reply fails, with the reason shown under the box.
- One reply at a time per message: a second POST while one is streaming gets HTTP 409.
