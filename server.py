#!/usr/bin/env python3
"""LLM Inbox web UI: read the messages that models sent to Eli and talk back to the same model.

Serves http://localhost:8798 . Left: the inbox (shared with the `llm-inbox` command). Right: the message, the
context it was sent from, and a chat that continues the conversation with the model that sent it. Each reply is
streamed back over Server-Sent Events and appended to the same transcript file the CLI uses.
A note (the note_to_eli tool: `via: "note"`, `halts: false`) is labelled as a note that did not stop the task;
everything else about it works like a stop message.

Data (all shared with shared/llm_inbox.py):  ~/Library/Application Support/LLMInbox/messages.jsonl
                                             ~/Library/Application Support/LLMInbox/debriefs/<id>.jsonl
Config: ~/Library/Application Support/LLMInbox/config.env (WEB_PORT, default 8798)
Log:    ~/Library/Logs/llm-inbox-web.log
Run:    .venv python from shared/ (needs the anthropic SDK): ../shared/.venv/bin/python3 server.py [--port N] [--test]
"""
import argparse, json, os, sys, threading, time, traceback, urllib.error, urllib.parse, urllib.request
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
# Host names the page may be reached by (see Handler._local). WEB_URL's host is added in main().
ALLOWED_HOSTS = {"localhost", "127.0.0.1"}

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
            "halts": r.get("halts", True),   # False for a note_to_eli note: the task carried on
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
    d = li.debrief_setup(r)
    prior = li.load_transcript(r["id"])
    # An answer with no content (a refusal can be empty) is kept in the transcript but not replayed: the API
    # rejects an empty assistant turn, which would block every later reply to this message.
    messages = d["messages"] + [{"role": e["role"], "content": e["content"]} for e in prior if e.get("content")]
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
    except (BrokenPipeError, ConnectionResetError):
        raise                                    # the browser went away; _reply logs it, nothing is saved
    except SystemExit as ex:                     # li.api_key() exits when there is no key; say why instead
        log(f"reply {r['id']}: {ex}")
        send("error", {"message": str(ex)}); return
    except Exception as ex:  # noqa: BLE001
        status = getattr(ex, "status_code", None)    # anthropic.APIStatusError, without importing the SDK here
        if status:
            msg = f"API error {status}: {getattr(ex, 'message', ex)}"
            log(f"reply {r['id']}: {msg}")
        else:
            msg = str(ex) or repr(ex)
            log(f"reply {r['id']}: {ex!r}\n{traceback.format_exc()}")
        send("error", {"message": msg}); return
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
        rid = urllib.parse.unquote(rid)
        hits = [r for r in li.load() if r["id"] == rid]
        return hits[0] if hits else None

    def _local(self, post: bool = False) -> bool:
        """Only this machine's own page may use the server. The Host check stops a web page that points its own
        domain at 127.0.0.1 (DNS rebinding) from reading the inbox; the Origin check stops any other page from
        posting replies, which would spend API tokens and speak to a model as Eli."""
        host = self.headers.get("Host") or ""
        name = urllib.parse.urlsplit("//" + host).hostname
        origin = self.headers.get("Origin")
        if name in ALLOWED_HOSTS and (not post or origin is None or urllib.parse.urlsplit(origin).netloc == host):
            return True
        log(f"refused {self.command} {self.path} (Host {host!r}, Origin {origin!r})")
        self._json({"error": "this server only answers its own page on localhost"}, 403)
        return False

    def do_GET(self):
        if not self._local():
            return
        path = self.path.split("?")[0]
        if path == "/":
            return self._static("index.html")
        if path in ("/app.js", "/app.css"):
            return self._static(path[1:])
        if path == "/api/messages":
            return self._json([summary(r) for r in reversed(li.load())])
        if path.startswith("/api/messages/") and path.count("/") == 3:
            r = self._find(path.split("/")[3])
            return self._json(detail(r)) if r else self._json({"error": "no such message"}, 404)
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        if not self._local(post=True):
            return
        parts = self.path.split("?")[0].split("/")
        if len(parts) == 5 and parts[1:3] == ["api", "messages"]:
            rid, action = parts[3], parts[4]
            r = self._find(rid)
            if not r:
                return self._json({"error": "no such message"}, 404)
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json({"error": "the request body must be a JSON object"}, 400)
            if action == "status":
                status, note = body.get("status", "handled"), body.get("note", "")
                if status not in ("new", "handled") or not isinstance(note, str):
                    return self._json({"error": "status must be \"new\" or \"handled\""}, 400)
                li.set_status(r["id"], status, note)
                log(f"{r['id']} marked {status}")
                return self._json({"ok": True})
            if action == "reply":
                text = body.get("text", "")
                if not isinstance(text, str):
                    return self._json({"error": "text must be a string"}, 400)
                return self._reply(r, text)
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
                self.wfile.write(f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, default=str)}\n\n".encode())
                self.wfile.flush()
            try:
                run_reply(r, text, send)
            except (BrokenPipeError, ConnectionResetError):
                raise
            except Exception as ex:  # noqa: BLE001 - the stream is open: report it there rather than end silently
                log(f"reply {r['id']}: {ex!r}\n{traceback.format_exc()}")
                send("error", {"message": str(ex) or repr(ex)})
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
    import shutil, tempfile
    global _client, LOG_PATH
    tmp = Path(tempfile.mkdtemp(prefix="llm-inbox-test-"))
    lc.INBOX_DIR, lc.MESSAGES_PATH = tmp, tmp / "messages.jsonl"
    li.DEBRIEF_DIR = tmp / "debriefs"
    LOG_PATH = lc.LOG_PATH = tmp / "test.log"         # fake messages stay out of the real logs
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
    # A note (note_to_eli): recorded without stopping the task, labelled as such, otherwise like any message.
    stopped = lc.check(app="selftest-app", model="fake-model", system="SYSTEM", messages=[{"role": "user", "content": "do the task"}],
                       response={"content": [{"type": "tool_use", "id": "tu2", "name": "note_to_eli", "input": {"message": "A note for Eli, test."}}],
                                 "stop_reason": "tool_use"}, tools=lc.CONTACT_TOOLS)
    lst = json.load(urllib.request.urlopen(base + "/api/messages"))
    notes = [m for m in lst if m["via"] == "note"]
    check(stopped is None and len(lst) == 2 and len(notes) == 1 and notes[0]["halts"] is False and notes[0]["status"] == "new"
          and notes[0]["first_line"] == "A note for Eli, test.", "a note is listed, marked as not halting")
    check([m["halts"] for m in lst if m["id"] == rec["id"]] == [True], "a stop message is listed as halting")
    nid = notes[0]["id"]
    det = json.load(urllib.request.urlopen(base + f"/api/messages/{nid}"))
    check(det["halts"] is False and det["message"] == "A note for Eli, test." and "note you left me" in det["intro"]
          and "carried on" in det["intro"], "note detail: not halting, and the introduction says the task carried on")
    js = urllib.request.urlopen(base + "/app.js").read().decode()
    check("isNote" in js and "did not stop the task" in js and 'id="d-note"' in page, "the page has the note label")
    req = urllib.request.Request(base + f"/api/messages/{nid}/reply", data=json.dumps({"text": "Thanks for the note"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    events = urllib.request.urlopen(req).read().decode()
    kw = _FakeClient.calls[-1]
    first_user = kw["messages"][-1]["content"]
    check("event: done" in events and first_user[0]["type"] == "tool_result" and first_user[0]["tool_use_id"] == "tu2"
          and first_user[1]["text"].endswith("Thanks for the note") and len(li.load_transcript(nid)) == 2,
          "reply to a note: pending note_to_eli call gets a tool_result, transcript saved")
    check({t["name"] for t in kw["tools"]} >= {"contact_eli", "note_to_eli"}, "reply to a note offers both tools")
    req = urllib.request.Request(base + f"/api/messages/{nid}/status", data=json.dumps({"status": "handled"}).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    urllib.request.urlopen(req).read()
    check([m["status"] for m in json.load(urllib.request.urlopen(base + "/api/messages")) if m["id"] == nid] == ["handled"],
          "a note can be marked handled")
    def post(path, data=None, headers=None, raw=None):
        """POST and return (HTTP status, body text)."""
        rq = urllib.request.Request(base + path, data=raw if raw is not None else json.dumps(data).encode(),
                                    headers={"Content-Type": "application/json", **(headers or {})}, method="POST")
        try:
            with urllib.request.urlopen(rq) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()
    # Bad requests get an answer instead of a dropped connection.
    check(post(f"/api/messages/{nid}/reply", raw=b"not json")[0] == 400, "a body that is not JSON gets 400")
    check(post(f"/api/messages/{nid}/status", {"status": "bogus"})[0] == 400, "an unknown status gets 400")
    check(post(f"/api/messages/{nid}/reply", {"text": "   "})[0] == 400, "an empty reply gets 400")
    # Only the local page may use the server.
    n_calls = len(_FakeClient.calls)
    check(post(f"/api/messages/{nid}/reply", {"text": "x"}, {"Origin": "http://evil.example"})[0] == 403
          and len(_FakeClient.calls) == n_calls, "a reply posted from another site is refused")
    check(post(f"/api/messages/{nid}/reply", {"text": "x"}, {"Origin": base})[0] == 200, "a reply from the page's own origin is accepted")
    try:
        urllib.request.urlopen(urllib.request.Request(base + "/api/messages", headers={"Host": "evil.example"}))
        check(False, "a request for another host name is refused")
    except urllib.error.HTTPError as e:
        check(e.code == 403, "a request for another host name is refused")
    # A failing model call reports its reason in the stream and writes nothing.
    turns = len(li.load_transcript(nid))
    class _NoKey:
        class messages:  # noqa: N801
            @staticmethod
            def stream(**kw): sys.exit("no API key: test")
    _client = _NoKey()
    code, events = post(f"/api/messages/{nid}/reply", {"text": "hello?"})
    check("event: error" in events and "no API key: test" in events and len(li.load_transcript(nid)) == turns,
          "a missing API key is reported in the stream, nothing saved")
    # An empty answer in the transcript is not replayed (the API rejects an empty assistant turn).
    _client = _FakeClient()
    li.append_transcript(nid, {"role": "user", "content": [{"type": "text", "text": "q"}]})
    li.append_transcript(nid, {"role": "assistant", "content": [], "stop_reason": "refusal"})
    post(f"/api/messages/{nid}/reply", {"text": "again"})
    check(all(m["content"] for m in _FakeClient.calls[-1]["messages"]), "an empty answer in the transcript is not replayed")
    httpd.shutdown()
    shutil.rmtree(tmp, ignore_errors=True)
    print("SELF-TEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cfg = lc.load_config()
    try:
        port = int(cfg.get("WEB_PORT") or DEFAULT_PORT)
    except ValueError:                               # a typo in config.env must not keep the inbox from starting
        log(f"WEB_PORT={cfg['WEB_PORT']!r} in {lc.CONFIG_PATH} is not a number; using {DEFAULT_PORT}")
        port = DEFAULT_PORT
    web_host = urllib.parse.urlsplit(cfg.get("WEB_URL") or "").hostname
    if web_host:
        ALLOWED_HOSTS.add(web_host)
    ap.add_argument("--port", type=int, default=port)
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
