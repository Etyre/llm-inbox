#!/usr/bin/env python3
"""LLM Inbox web UI: read the messages that models sent to Eli and talk back to the same model.

Serves http://localhost:8798 . Left: the inbox (shared with the `llm-inbox` command). Right: the message, the
context it was sent from, and a chat that continues the conversation with the model that sent it. Each reply is
streamed back over Server-Sent Events and appended to the same transcript file the CLI uses.

Data (all shared with shared/llm_inbox.py):  ~/Library/Application Support/LLMInbox/messages.jsonl
                                             ~/Library/Application Support/LLMInbox/debriefs/<id>.jsonl
Config: ~/Library/Application Support/LLMInbox/config.env (WEB_PORT, default 8798)
Log:    ~/Library/Logs/llm-inbox-web.log
Run:    .venv python from shared/ (needs the anthropic SDK): ../shared/.venv/bin/python3 server.py [--port N] [--test]
"""
import argparse, json, os, sys, threading, time, traceback, urllib.request
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path

SHARED = Path(__file__).resolve().parent.parent / "shared"
sys.path.insert(0, str(SHARED))
import llm_contact as lc  # noqa: E402
import llm_inbox as li    # noqa: E402

STATIC = Path(__file__).resolve().parent / "static"
LOG_PATH = Path.home() / "Library/Logs/llm-inbox-web.log"
DEFAULT_PORT = 8798
MAX_TOKENS = 16000

_client = None
_client_lock = threading.Lock()
_reply_locks: dict[str, threading.Lock] = {}
_reply_locks_guard = threading.Lock()


def log(s: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {s}\n"
    if sys.stdout.isatty():                      # echo only for hand runs; under launchd the file is the log
        print(line, end="", flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_PATH, "a") as f:
            f.write(line)
    except OSError:
        pass


def client():
    """The Anthropic client, created on first use so the server starts even without a key."""
    global _client
    with _client_lock:
        if _client is None:
            import anthropic
            _client = anthropic.Anthropic(api_key=li.api_key())
        return _client


def reply_lock(rid: str) -> threading.Lock:
    with _reply_locks_guard:
        return _reply_locks.setdefault(rid, threading.Lock())


def summary(r: dict) -> dict:
    msg = (r.get("message") or "").strip()
    return {"id": r["id"], "at": r["at"], "app": r["app"], "model": r["model"], "via": r.get("via"),
            "status": r.get("status", "new"), "note": r.get("note", ""),
            "first_line": msg.splitlines()[0][:160] if msg else "(empty)",
            "turns": len(li.load_transcript(r["id"]))}


def detail(r: dict) -> dict:
    d = li.debrief_setup(r)
    return {**summary(r), "message": r.get("message"), "system": d["system"], "messages": r.get("messages") or [],
            "response": r.get("response") or {}, "tools": [t.get("name") for t in d["tools"]],
            "transcript": li.load_transcript(r["id"]), "intro": d["intro"], "cwd": r.get("cwd")}


def run_reply(r: dict, text: str, send) -> None:
    """Send Eli's reply to the model and stream the answer through `send(event, data)`.
    The user turn and the answer are written to the transcript only after the answer completes, so a failed
    request leaves nothing half-written and the UI can keep the draft."""
    import anthropic
    d = li.debrief_setup(r)
    prior = li.load_transcript(r["id"])
    messages = d["messages"] + [{"role": e["role"], "content": e["content"]} for e in prior]
    if prior:
        last = prior[-1]
        pending = [b for b in last["content"] if isinstance(b, dict) and b.get("type") == "tool_use"] if last["role"] == "assistant" else []
    else:
        pending = d["pending"]
    content = li.user_turn(text, pending, first=not prior, intro=d["intro"])
    messages.append({"role": "user", "content": content})
    kwargs = dict(model=d["model"], max_tokens=MAX_TOKENS, tools=d["tools"], messages=messages)
    if d["system"]:
        kwargs["system"] = d["system"]
    try:
        with client().messages.stream(**kwargs) as stream:
            for t in stream.text_stream:
                send("delta", {"text": t})
            final = stream.get_final_message()
    except anthropic.APIStatusError as ex:
        log(f"reply {r['id']}: API error {ex.status_code}: {ex.message}")
        send("error", {"message": f"API error {ex.status_code}: {ex.message}"}); return
    except Exception as ex:  # noqa: BLE001
        log(f"reply {r['id']}: {ex!r}\n{traceback.format_exc()}")
        send("error", {"message": str(ex)}); return
    final_content = final.model_dump(mode="json")["content"]
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    li.append_transcript(r["id"], {"role": "user", "content": content, "at": now})
    li.append_transcript(r["id"], {"role": "assistant", "content": final_content, "stop_reason": final.stop_reason, "at": now})
    log(f"reply {r['id']}: {d['model']} answered, stop_reason={final.stop_reason}")
    send("done", {"user": {"role": "user", "content": content, "at": now},
                  "assistant": {"role": "assistant", "content": final_content, "stop_reason": final.stop_reason, "at": now}})


class Handler(BaseHTTPRequestHandler):
    server_version = "LLMInbox/1.0"

    def log_message(self, fmt, *args):  # quiet access log; errors still go through log()
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _static(self, name: str):
        p = STATIC / name
        if not p.is_file():
            self._json({"error": "not found"}, 404); return
        ctype = {"html": "text/html", "js": "application/javascript", "css": "text/css"}.get(p.suffix[1:], "application/octet-stream")
        body = p.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _find(self, rid: str):
        recs = li.load()
        hits = [r for r in recs if r["id"] == rid]
        return hits[0] if hits else None

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self._static("index.html")
        if path in ("/app.js", "/app.css"):
            return self._static(path[1:])
        if path == "/api/messages":
            return self._json([summary(r) for r in reversed(li.load())])
        if path.startswith("/api/messages/"):
            r = self._find(path.split("/")[3])
            return self._json(detail(r)) if r else self._json({"error": "no such message"}, 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        parts = self.path.split("?")[0].split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "messages"]:
            rid, action = parts[3], parts[4]
            r = self._find(rid)
            if not r:
                return self._json({"error": "no such message"}, 404)
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            if action == "status":
                li.set_status(rid, body.get("status", "handled"), body.get("note", ""))
                log(f"{rid} marked {body.get('status', 'handled')}")
                return self._json({"ok": True})
            if action == "reply":
                return self._reply(r, body.get("text", ""))
        self._json({"error": "not found"}, 404)

    def _reply(self, r: dict, text: str):
        if not text.strip():
            return self._json({"error": "empty reply"}, 400)
        lock = reply_lock(r["id"])
        if not lock.acquire(blocking=False):
            return self._json({"error": "a reply to this message is already in progress"}, 409)
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()

            def send(event, data):
                try:
                    self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    raise
            run_reply(r, text, send)
        except (BrokenPipeError, ConnectionResetError):
            log(f"reply {r['id']}: browser disconnected mid-stream")
        finally:
            lock.release()


def serve(port: int) -> ThreadingHTTPServer:
    httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    httpd.daemon_threads = True
    return httpd


# ---------- self-test: fake model, temporary inbox, exercises every route ----------
class _FakeStream:
    def __init__(self, kw):
        self.kw = kw
        self.text_stream = iter(["Hello ", "Eli. ", "Thanks for replying."])
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def get_final_message(self):
        class M:
            stop_reason = "end_turn"
            def model_dump(self, mode="json"):
                return {"content": [{"type": "text", "text": "Hello Eli. Thanks for replying."}]}
        return M()

class _FakeClient:
    calls: list = []
    class messages:  # noqa: N801
        @staticmethod
        def stream(**kw):
            _FakeClient.calls.append(kw); return _FakeStream(kw)


def self_test() -> int:
    import tempfile
    global _client
    tmp = Path(tempfile.mkdtemp(prefix="llm-inbox-test-"))
    lc.INBOX_DIR, lc.MESSAGES_PATH = tmp, tmp / "messages.jsonl"
    li.DEBRIEF_DIR = tmp / "debriefs"
    lc.notify = lambda rec: None                      # no ntfy/email/Mac alert from the test
    rec = lc.check(app="selftest-app", model="fake-model", system="SYSTEM", messages=[{"role": "user", "content": "do the task"}],
                   response={"content": [{"type": "tool_use", "id": "tu1", "name": "contact_eli", "input": {"message": "Hi Eli, test."}}],
                             "stop_reason": "tool_use"}, tools=[lc.CONTACT_TOOL])
    _client = _FakeClient()
    httpd = serve(0); port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{port}"
    ok = True
    def check(cond, what):
        nonlocal ok
        print(("ok   " if cond else "FAIL ") + what); ok = ok and cond
    page = urllib.request.urlopen(base + "/").read().decode()
    check("<title>" in page and "app.js" in page, "GET / serves the page")
    lst = json.load(urllib.request.urlopen(base + "/api/messages"))
    check(len(lst) == 1 and lst[0]["id"] == rec["id"] and lst[0]["status"] == "new", "GET /api/messages lists the message")
    det = json.load(urllib.request.urlopen(base + f"/api/messages/{rec['id']}"))
    check(det["message"] == "Hi Eli, test." and det["transcript"] == [] and det["system"] == "SYSTEM", "GET /api/messages/<id> has message, context, empty transcript")
    req = urllib.request.Request(base + f"/api/messages/{rec['id']}/reply", data=json.dumps({"text": "Hello model"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    events = urllib.request.urlopen(req).read().decode()
    check("event: delta" in events and "event: done" in events, "POST reply streams delta and done events")
    kw = _FakeClient.calls[-1]
    first_user = kw["messages"][-1]["content"]
    check(kw["model"] == "fake-model" and kw["system"] == "SYSTEM", "reply goes to the same model with the same system prompt")
    check(first_user[0]["type"] == "tool_result" and first_user[0]["tool_use_id"] == "tu1", "pending contact_eli call gets a tool_result")
    check(first_user[1]["text"].startswith("This is Eli.") and first_user[1]["text"].endswith("Hello model"), "first turn carries the introduction")
    check(any(t["name"] == "contact_eli" for t in kw["tools"]), "contact_eli tool is offered")
    tr = li.load_transcript(rec["id"])
    check(len(tr) == 2 and tr[1]["role"] == "assistant", "transcript saved: user + assistant")
    det2 = json.load(urllib.request.urlopen(base + f"/api/messages/{rec['id']}"))
    check(len(det2["transcript"]) == 2, "detail shows the transcript")
    req = urllib.request.Request(base + f"/api/messages/{rec['id']}/reply", data=json.dumps({"text": "second"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()
    kw2 = _FakeClient.calls[-1]
    check(len(kw2["messages"]) == 5 and kw2["messages"][-1]["content"][0]["type"] == "text", "second turn replays history, no duplicate tool_result, no intro")
    req = urllib.request.Request(base + f"/api/messages/{rec['id']}/status", data=json.dumps({"status": "handled", "note": "t"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()
    check(json.load(urllib.request.urlopen(base + "/api/messages"))[0]["status"] == "handled", "POST status marks handled")
    try:
        urllib.request.urlopen(base + "/api/messages/nope"); check(False, "404 for unknown id")
    except urllib.error.HTTPError as e:
        check(e.code == 404, "404 for unknown id")
    httpd.shutdown()
    print("SELF-TEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--port", type=int, default=int(lc.load_config().get("WEB_PORT", DEFAULT_PORT)))
    ap.add_argument("--test", action="store_true", help="run the self-test against a fake model and a temporary inbox")
    a = ap.parse_args()
    if a.test:
        sys.exit(self_test())
    httpd = serve(a.port)
    log(f"LLM Inbox web UI on http://localhost:{a.port}  inbox={lc.MESSAGES_PATH}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
