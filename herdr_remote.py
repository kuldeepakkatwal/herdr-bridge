#!/usr/bin/env python3
"""Herdr Remote bridge: turns herdr's unix socket into a small HTTP + SSE API for the iPhone app.

Stdlib only. Binds 127.0.0.1; reach it through `tailscale serve`, which adds the
Tailscale-User-Login header this bridge checks.
"""
import argparse
import base64
import errno
import fcntl
import glob
import json
import os
import platform
import pty
import queue
import re
import select
import shutil
import signal
import socket
import struct
import subprocess
import sys
import termios
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HOME = os.path.expanduser("~")
SOCK = os.path.join(HOME, ".config/herdr/herdr.sock")
UPLOADS = os.path.join(HOME, ".herdr-remote", "uploads")   # pictures sent from the phone


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def call(method, params=None, timeout=5):
    """One request per connection: herdr speaks newline-delimited JSON."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(SOCK)
        s.sendall((json.dumps({"id": "b", "method": method, "params": params or {}}) + "\n").encode())
        buf = b""
        while b"\n" not in buf:
            chunk = s.recv(1 << 20)
            if not chunk:
                break
            buf += chunk
    finally:
        s.close()
    if not buf.strip():
        raise ConnectionError("herdr closed the connection without a reply")
    msg = json.loads(buf.split(b"\n", 1)[0])
    if "error" in msg:
        raise HerdrError(msg["error"].get("code", "error"), msg["error"].get("message", ""))
    return msg["result"]


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
MENU_LINE = re.compile(r"^(?:[❯›>]\s*)?(\d{1,2})[.)]\s+(.+?)$")


def tilde(path):
    return "~" + path[len(HOME):] if path and path.startswith(HOME) else path


CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
_transcripts = {}   # path -> ((mtime, size), items, title)


def session_file(session_id):
    if not session_id or "/" in session_id:
        return None
    hits = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", session_id + ".jsonl"))
    return hits[0] if hits else None


def _tool_line(block):
    i = block.get("input") or {}
    arg = i.get("command") or i.get("file_path") or i.get("pattern") or i.get("description") or ""
    if i.get("file_path"):
        arg = os.path.basename(arg)
    arg = " ".join(str(arg).split())
    return "%s %s" % (block.get("name", "tool"), arg[:80] + ("…" if len(arg) > 80 else ""))


CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>")
CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>")


class Tail:
    """Follows one Claude Code transcript (jsonl, appended to as the conversation goes).

    read() parses only what was appended since the last call and returns it as events
    {"item": {role, text}, "replace_last": bool}. replace_last: a tool line merged into the previous
    tool item, so that whole item is sent again. Keeps your prompts, Claude's replies and short tool
    lines; skips thinking, tool results, meta/sidechain records and local command output.
    """

    def __init__(self, path=None, session=None):
        self.path, self.session = path, session
        self.offset, self.rest = 0, b""
        self.items, self.title = [], None

    def _add(self, role, text):
        if role == "tool" and self.items and self.items[-1]["role"] == "tool":
            self.items[-1]["text"] += "\n" + text
            return {"item": dict(self.items[-1]), "replace_last": True}
        self.items.append({"role": role, "text": text})
        return {"item": dict(self.items[-1]), "replace_last": False}

    def _record(self, line):
        try:
            d = json.loads(line)
        except ValueError:
            return []
        t = d.get("type")
        if t == "ai-title":
            self.title = d.get("aiTitle") or self.title
            return []
        if t not in ("user", "assistant") or d.get("isMeta") or d.get("isSidechain"):
            return []
        content = (d.get("message") or {}).get("content")
        out = []
        if t == "user":
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            text = (content or "").strip() if isinstance(content, str) else ""
            m = CMD_NAME.match(text)
            if m:   # a slash command is saved as tags: show it the way it was typed
                args = CMD_ARGS.search(text)
                text = m.group(1) + (" " + args.group(1).strip() if args and args.group(1).strip() else "")
            if text and not text.startswith("<"):
                out.append(self._add("user", text))
        else:
            for b in content or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append(self._add("assistant", b["text"].strip()))
                elif b.get("type") == "tool_use":
                    out.append(self._add("tool", _tool_line(b)))
        return out

    def read(self):
        """Events appended since the last read. None: the file shrank or was replaced, start over
        (the caller sends the whole history again)."""
        if not self.path and self.session:
            self.path = session_file(self.session)   # a new session's file appears a moment after it starts
        if not self.path or not os.path.exists(self.path):
            return []
        if os.stat(self.path).st_size < self.offset:
            self.__init__(self.path, self.session)
            return None
        with open(self.path, "rb") as f:
            f.seek(self.offset)
            chunk = f.read()
        if not chunk:
            return []
        self.offset += len(chunk)
        lines = (self.rest + chunk).split(b"\n")
        self.rest = lines.pop()   # the last piece has no newline yet (empty when the file ends with one)
        out = []
        for raw in lines:
            out.extend(self._record(raw.decode("utf-8", "replace")))
        return out


def parse_transcript(path):
    """Claude Code's saved session -> ([{role, text}], title)."""
    t = Tail(path)
    t.read()
    return t.items, t.title


def transcript(session_id):
    """Cached by file mtime+size, so polling titles every second costs one stat()."""
    path = session_file(session_id)
    if not path:
        return [], None
    st = os.stat(path)
    key = (st.st_mtime, st.st_size)
    hit = _transcripts.get(path)
    if not hit or hit[0] != key:
        hit = (key,) + parse_transcript(path)
        _transcripts[path] = hit
    return hit[1], hit[2]


def build_state(workspaces, tabs, agents):
    """`agents` is herdr's pane list: every pane, with `agent` None for a plain shell."""
    tab_label = {t["tab_id"]: t.get("label") for t in tabs}
    out = []
    for w in workspaces:
        ags = [a for a in agents if a.get("workspace_id") == w["workspace_id"]]
        out.append({
            "id": w["workspace_id"],
            "label": w.get("label") or str(w.get("number", "")),
            "path": tilde(ags[0].get("cwd")) if ags else None,
            "agents": [{
                "pane_id": a["pane_id"],
                "tab_id": a.get("tab_id"),
                "tab_label": tab_label.get(a.get("tab_id")),
                "title": tab_label.get(a.get("tab_id")) or a.get("terminal_title_stripped") or _session_title(a) or a.get("agent") or a["pane_id"],
                "agent": a.get("agent"),
                "status": a.get("agent_status", "unknown"),
                "session": (a.get("agent_session") or {}).get("value"),   # Claude Code session id: its transcript file
                "cwd": tilde(a.get("cwd")),
                "menu": None,
            } for a in ags],
        })
    return out


def _session_title(agent):
    try:
        return transcript((agent.get("agent_session") or {}).get("value"))[1]
    except OSError:
        return None


def parse_menu(text):
    """Find Claude Code's numbered choice list near the bottom of the screen.

    A line '1. x' starts a new list; only the next number extends it; other lines
    (descriptions, blanks) are skipped. The last list with 2+ options wins.
    """
    found = []
    for raw in text.splitlines()[-40:]:
        line = ANSI.sub("", raw).strip().strip("│").strip()
        m = MENU_LINE.match(line)
        if not m:
            continue
        n = int(m.group(1))
        if n == 1:
            found = []
        if n == len(found) + 1:
            found.append({"n": n, "label": m.group(2).strip()})
    return found if len(found) >= 2 else None


def read_screen(pane, source="recent_unwrapped", fmt="ansi", lines=400):
    params = {"pane_id": pane, "source": source, "format": fmt, "lines": lines}
    if fmt == "ansi":
        params["strip_ansi"] = False
    return call("pane.read", params)["read"]["text"]


# Terminal mode: herdr's own client (`herdr session attach`) in a pseudo-terminal, streamed to the phone.
# launchd's PATH has no ~/.local/bin, so look there ourselves.
HERDR_BIN = shutil.which("herdr") or os.path.join(HOME, ".local/bin/herdr")
HERDR_SESSION = "default"
TERM_IDLE = 10          # seconds without a stream before a term is closed
TERM_MAX = 3            # terms per bridge; the oldest is closed when exceeded
TERMS = {}              # term_id -> Term
TERMS_LOCK = threading.Lock()
TERM_OPS = threading.RLock()   # held for a whole open/close, so one pane's zoom on/off never interleave
TERM_ARGV_OVERRIDE = None   # tests only: run this instead of herdr's client


def _winsize(cols, rows):
    return struct.pack("HHHH", rows, cols, 0, 0)


class Term:
    """One command in a pty. Output chunks land in `q` (None = ended). `streams` counts open SSE readers."""
    def __init__(self, pane, cols, rows, argv=None):
        self.id = uuid.uuid4().hex[:12]
        self.pane = pane
        self.q = queue.Queue()
        self.alive = True
        self.closed = False
        self.lock = threading.Lock()   # guards fd: the pump closes it, write/resize must never use a stale number
        self.streams = 0
        self.last_stream = time.time()
        argv = argv or [HERDR_BIN, "session", "attach", HERDR_SESSION]
        # drop herdr's "you are inside a pane" variables (the bridge may run in one), built before fork
        env = {k: v for k, v in os.environ.items() if not k.startswith("HERDR_")}
        env["TERM"] = "xterm-256color"
        self.pid, self.fd = pty.fork()
        if self.pid == 0:   # child: size the terminal, run the client
            try:
                fcntl.ioctl(0, termios.TIOCSWINSZ, _winsize(cols, rows))
                os.execvpe(argv[0], argv, env)
            finally:
                os._exit(127)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        try:
            while True:
                data = os.read(self.fd, 65536)
                if not data:
                    break
                self.q.put(data)
        except OSError:
            pass    # EIO: the child is gone
        self.alive = False
        with self.lock:
            try:
                os.close(self.fd)   # closed here, not in close(), so a read never lands on a reused fd number
            except OSError:
                pass
            self.fd = -1
        self.q.put(None)

    def write(self, data):
        with self.lock:
            if self.fd < 0:
                raise OSError(errno.EBADF, "terminal closed")
            while data:   # os.write may take only part of it
                data = data[os.write(self.fd, data):]

    def resize(self, cols, rows):
        with self.lock:
            if self.fd < 0:
                raise OSError(errno.EBADF, "terminal closed")
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, _winsize(cols, rows))

    def close(self):
        """Hang up, then kill if it is still there after 2 s; always reap (also after it exited by itself)."""
        if self.closed:
            return
        self.closed, self.alive = True, False
        for sig in (signal.SIGHUP, signal.SIGKILL):
            try:
                os.kill(self.pid, sig)
            except OSError:
                pass
            for _ in range(20):
                try:
                    if os.waitpid(self.pid, os.WNOHANG)[0]:
                        break
                except ChildProcessError:
                    break
                time.sleep(0.1)
            else:
                continue
            break
        self.q.put(None)


def open_term(pane, cols, rows, argv=None):
    """Attach herdr's client in a pty sized to the phone, looking at `pane` (focused and zoomed in herdr,
    so the phone is not filled with the sidebar and the other panes). The Mac window follows: accepted."""
    # ponytail: one lock for all terms (a close can take 2 s); fine for at most 3 terms on one phone
    with TERM_OPS:
        with TERMS_LOCK:
            stale = [t for t in TERMS.values() if t.pane == pane]   # one term per pane
            others = [t for t in TERMS.values() if t.pane != pane]  # oldest first (dicts keep insertion order)
            stale += others[:max(0, len(others) - TERM_MAX + 1)]    # room for the new one under the cap
        for t in stale:
            close_term(t.id)
        call("pane.focus", {"pane_id": pane})
        call("pane.zoom", {"pane_id": pane, "mode": "on"})
        try:
            t = Term(pane, cols, rows, argv or TERM_ARGV_OVERRIDE)
        except Exception:
            _unzoom(pane)
            raise
        with TERMS_LOCK:
            TERMS[t.id] = t
        return t


def _unzoom(pane):
    try:
        call("pane.zoom", {"pane_id": pane, "mode": "off"})
    except (HerdrError, OSError, ValueError):
        pass   # pane gone or herdr down: nothing to unzoom


def close_term(term_id):
    print("TMPLOG %.3f close_term %s" % (time.time(), term_id), file=sys.stderr, flush=True)
    with TERM_OPS:
        with TERMS_LOCK:
            t = TERMS.pop(term_id, None)
        if t is None:
            return False
        t.close()
        _unzoom(t.pane)
        return True


def close_all_terms():
    with TERMS_LOCK:
        ids = list(TERMS)
    for tid in ids:
        close_term(tid)


def reap_terms(now=None):
    """Close terms nobody has streamed for TERM_IDLE seconds (phone went away without DELETE), and dead ones."""
    now = now or time.time()
    with TERMS_LOCK:
        idle = [t.id for t in TERMS.values() if (t.streams == 0 and now - t.last_stream > TERM_IDLE) or not t.alive]
    for tid in idle:
        close_term(tid)


def reap_loop():
    while True:
        time.sleep(2)
        try:
            reap_terms()
        except Exception:   # must never die: an unreaped term keeps the Mac pane zoomed and phone-sized
            traceback.print_exc()


class Hub:
    """Holds the latest state; refreshes it from herdr; fans changes out to SSE clients."""

    def __init__(self):
        self.lock = threading.Lock()
        self.state = {"herdr": "offline", "workspaces": []}
        self.clients = set()
        self.wake = threading.Event()

    def snapshot(self):
        with self.lock:
            return self.state

    def subscribe(self, watch=None):
        q = queue.Queue()
        q.watch = watch
        with self.lock:
            self.clients.add(q)
        return q

    def kick(self, pane):
        """An action just went to this agent: tell its watchers to re-read the screen now."""
        with self.lock:
            for q in self.clients:
                if getattr(q, "watch", None) == pane:
                    q.put(("kick", None))

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)

    def publish(self, state):
        with self.lock:
            if state == self.state:
                return
            self.state = state
            for q in self.clients:
                q.put(("state", state))

    def refresh(self):
        try:
            ws = build_state(call("workspace.list")["workspaces"], call("tab.list")["tabs"], call("pane.list")["panes"])
            for w in ws:
                for a in w["agents"]:
                    if a["status"] == "blocked":
                        try:
                            a["menu"] = parse_menu(read_screen(a["pane_id"], source="visible", fmt="text"))
                        except HerdrError:
                            pass
            self.publish({"herdr": "online", "workspaces": ws})
        except (OSError, HerdrError, ValueError, KeyError):
            self.publish({"herdr": "offline", "workspaces": self.snapshot()["workspaces"]})

    def poke(self):
        """Refresh now (after an action) instead of waiting for the next tick."""
        self.wake.set()

    def run(self, interval=1.0):
        while True:
            self.wake.clear()  # clear before refreshing so a poke during refresh is not lost
            try:
                self.refresh()
            except Exception:  # unexpected herdr data must not kill the loop and freeze the state
                traceback.print_exc()
            self.wake.wait(interval)


def _clip_cmds():
    if platform.system() == "Darwin":
        return ["pbpaste"], ["pbcopy"]
    if shutil.which("wl-paste"):
        return ["wl-paste", "-n"], ["wl-copy"]
    return ["xclip", "-o", "-selection", "clipboard"], ["xclip", "-selection", "clipboard"]


def clip_get():
    return subprocess.run(_clip_cmds()[0], capture_output=True, text=True, timeout=3).stdout


def clip_set(text):
    subprocess.run(_clip_cmds()[1], input=text, text=True, timeout=3, check=True)


def _keys(keys):
    if not isinstance(keys, list):  # a bare string would otherwise be sent letter by letter
        raise ValueError("keys must be a list")
    return [str(k) for k in keys]


def _prompt(pane, b):
    """An agent gets herdr's prompt (it waits until the agent can take it); a plain shell gets the line typed and Enter."""
    if call("pane.get", {"pane_id": pane})["pane"].get("agent"):
        return call("agent.prompt", {"target": pane, "text": str(b["text"])})
    call("pane.send_text", {"pane_id": pane, "text": str(b["text"])})
    call("pane.send_keys", {"pane_id": pane, "keys": ["enter"]})


IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG")   # JPEG, PNG
MAX_IMAGE = 10 << 20


def _send(pane, b):
    """Pictures from the phone are saved as files and their paths typed into the agent with the text,
    then Enter: Claude Code opens image files from a typed path. Rejected: the Mac clipboard paste
    (an osascript plus a 0.5 s wait per picture, and it replaced the owner's clipboard)."""
    images = b.get("images") or []
    if not isinstance(images, list) or len(images) > 5:
        raise ValueError("images must be a list of at most 5")
    blobs = [base64.b64decode(enc, validate=True) for enc in images]
    for i, data in enumerate(blobs):
        if not data.startswith(IMAGE_MAGIC) or len(data) > MAX_IMAGE:
            raise ValueError("image %d is not a JPEG/PNG under 10 MB" % i)
    os.makedirs(UPLOADS, exist_ok=True)
    stamp, paths = int(time.time() * 1000), []
    for i, data in enumerate(blobs):
        path = os.path.join(UPLOADS, "%d-%d.%s" % (stamp, i, "png" if data.startswith(b"\x89") else "jpg"))
        with open(path, "wb") as f:
            f.write(data)
        paths.append(path)
    text = " ".join(paths + [str(b.get("text") or "").strip()]).strip()
    if not text:
        raise ValueError("nothing to send")
    call("pane.send_text", {"pane_id": pane, "text": text})
    call("pane.send_keys", {"pane_id": pane, "keys": ["enter"]})


# pane.* works on every pane; agent.* refuses a plain shell.
ACTIONS = {
    "keys": lambda pane, b: call("pane.send_keys", {"pane_id": pane, "keys": _keys(b["keys"])}),
    "text": lambda pane, b: call("pane.send_text", {"pane_id": pane, "text": str(b["text"])}),
    "prompt": _prompt,
    "send": _send,
    "answer": lambda pane, b: call("pane.send_keys", {"pane_id": pane, "keys": [str(int(b["choice"]))]}),
}


AGENT_KINDS = {"claude", "codex"}
TAB_KINDS = AGENT_KINDS | {"shell"}   # shell: a plain terminal, no agent started


def start_agent(pane, kind):
    """Start an agent in a fresh shell pane. The shell may still be booting, so retry briefly.
    agent_not_ready means it started but is showing a startup question: that is still a success."""
    if kind not in AGENT_KINDS:
        raise ValueError("kind must be one of %s" % sorted(AGENT_KINDS))
    name = "m%s" % os.urandom(4).hex()
    for attempt in range(6):
        try:
            call("agent.start", {"name": name, "kind": kind, "pane_id": pane}, timeout=40)
            return
        except HerdrError as e:
            if e.code == "agent_not_ready":
                return
            if attempt == 5:
                raise
            time.sleep(0.5)


def new_workspace(body):
    cwd = os.path.expanduser(str(body.get("cwd") or "~"))
    r = call("workspace.create", {"cwd": cwd, "label": body.get("label") or None, "focus": False})
    pane = r["root_pane"]["pane_id"]
    if body.get("kind", "claude") != "shell":
        start_agent(pane, body.get("kind", "claude"))
    return pane


def new_tab(hub, workspace_id, body):
    cwd = body.get("cwd")
    if not cwd:   # default: the folder this workspace's agents already work in
        ws = next((w for w in hub.snapshot()["workspaces"] if w["id"] == workspace_id), None)
        cwd = ws and ws["path"]
    r = call("tab.create", {"workspace_id": workspace_id, "cwd": os.path.expanduser(cwd) if cwd else None, "focus": False})
    pane = r["root_pane"]["pane_id"]
    if body.get("kind", "claude") != "shell":
        start_agent(pane, body.get("kind", "claude"))
    return pane


def session_of(hub, pane):
    for w in hub.snapshot()["workspaces"]:
        for a in w["agents"]:
            if a["pane_id"] == pane:
                return a.get("session")
    return None


def _size(body):
    cols, rows = int(body["cols"]), int(body["rows"])
    if not (10 <= cols <= 500 and 5 <= rows <= 300):
        raise ValueError("size out of range")
    return cols, rows


def make_handler(hub, owner):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            print("TMPLOG %.3f %s" % (time.time(), args[0] % args[1:]), file=sys.stderr, flush=True)

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, ecode, message):
            self._json(code, {"error": {"code": ecode, "message": message}})

        def _authed(self):
            # tailscale serve sets this from the caller's tailnet identity; the bridge only listens on
            # 127.0.0.1, so the header cannot come from anywhere else.
            if self.headers.get("Tailscale-User-Login") != owner:
                self.close_connection = True  # the request body was not read; do not reuse the connection
                self._err(403, "forbidden", "this bridge only answers its owner")
                return False
            return True

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._authed():
                return
            u = urlparse(self.path)
            if u.path == "/api/state":
                return self._json(200, hub.snapshot())
            if u.path == "/api/events":
                return self._events(parse_qs(u.query).get("watch", [None])[0])
            parts = u.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "history":
                try:
                    sid = (call("pane.get", {"pane_id": unquote(parts[2])})["pane"].get("agent_session") or {}).get("value")
                    items, title = transcript(sid)
                except HerdrError as e:
                    return self._err(409, e.code, str(e))
                limit = int(parse_qs(u.query).get("limit", ["500"])[0])
                return self._json(200, {"items": items[-limit:], "total": len(items), "title": title})
            if len(parts) == 4 and parts[:2] == ["api", "term"] and parts[3] == "stream":
                return self._term_stream(parts[2])
            if u.path == "/api/clipboard":
                try:
                    return self._json(200, {"text": clip_get()})
                except (OSError, subprocess.SubprocessError) as e:
                    return self._err(500, "clipboard", str(e))
            self._err(404, "not_found", u.path)

        def do_POST(self):
            if not self._authed():
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            try:
                body = self._body()
                if parts == ["api", "clipboard"]:
                    clip_set(str(body["text"]))
                    return self._json(200, {"ok": True})
                if parts == ["api", "workspaces"]:
                    if body.get("kind", "claude") not in TAB_KINDS:
                        raise ValueError("unknown agent kind")
                    pane = new_workspace(body)
                    hub.poke()
                    return self._json(200, {"ok": True, "pane_id": pane})
                if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "move":
                    call("workspace.move_block", {"workspace_ids": [unquote(parts[2])], "before_workspace_id": body.get("before")})
                    hub.poke()
                    return self._json(200, {"ok": True})
                if len(parts) == 4 and parts[:2] == ["api", "workspaces"] and parts[3] == "tabs":
                    if body.get("kind", "claude") not in TAB_KINDS:
                        raise ValueError("unknown agent kind")
                    pane = new_tab(hub, unquote(parts[2]), body)
                    hub.poke()
                    return self._json(200, {"ok": True, "pane_id": pane})
                if len(parts) == 4 and parts[:2] in (["api", "workspaces"], ["api", "tabs"]) and parts[3] == "close":
                    kind = parts[1][:-1]   # "workspace" or "tab"; closing ends whatever runs in it
                    call("%s.close" % kind, {"%s_id" % kind: unquote(parts[2])})
                    hub.poke()
                    return self._json(200, {"ok": True})
                if len(parts) == 4 and parts[:2] in (["api", "workspaces"], ["api", "tabs"]) and parts[3] == "rename":
                    label = str(body["label"]).strip()
                    if not label:
                        raise ValueError("label is empty")
                    kind = parts[1][:-1]   # "workspace" or "tab"
                    call("%s.rename" % kind, {"%s_id" % kind: unquote(parts[2]), "label": label})
                    hub.poke()
                    return self._json(200, {"ok": True})
                if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "term":
                    cols, rows = _size(body)
                    t = open_term(unquote(parts[2]), cols, rows)
                    return self._json(200, {"term_id": t.id})
                if len(parts) == 4 and parts[:2] == ["api", "term"] and parts[3] in ("input", "resize"):
                    with TERMS_LOCK:
                        t = TERMS.get(parts[2])
                    if t is None or not t.alive:
                        return self._err(404, "not_found", "no such terminal")
                    try:
                        if parts[3] == "input":
                            data = base64.b64decode(str(body["data"]))
                            t.write(data)
                        else:
                            size = _size(body)
                            t.resize(*size)
                    except OSError:   # the client ended between the lookup and the write
                        return self._err(404, "not_found", "no such terminal")
                    return self._json(200, {"ok": True})
                if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] in ACTIONS:
                    ACTIONS[parts[3]](unquote(parts[2]), body)
                    hub.kick(unquote(parts[2]))
                    hub.poke()
                    return self._json(200, {"ok": True})
                self._err(404, "not_found", self.path)
            except HerdrError as e:
                self._err(409, e.code, str(e))
            except (KeyError, ValueError, TypeError, OverflowError) as e:
                self._err(400, "bad_request", repr(e))
            except (OSError, subprocess.SubprocessError) as e:
                self._err(503, "herdr_offline", str(e))

        def do_DELETE(self):
            if not self._authed():
                return
            parts = urlparse(self.path).path.strip("/").split("/")
            if len(parts) == 3 and parts[:2] == ["api", "term"]:
                if close_term(parts[2]):
                    return self._json(200, {"ok": True})
                return self._err(404, "not_found", "no such terminal")
            self._err(404, "not_found", self.path)

        def _term_stream(self, term_id):
            with TERMS_LOCK:
                t = TERMS.get(term_id)
                ok = t is not None and t.alive
                if ok:
                    t.streams += 1
            if not ok:
                return self._err(404, "not_found", "no such terminal")
            try:
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                self.close_connection = True
                next_ping = time.time() + 5   # we talk to the local tailscale serve proxy: a clean close is seen
                # within 0.5 s (_hung_up); a phone that just vanishes only once tailscaled drops its side
                while True:
                    try:
                        chunk = t.q.get(timeout=0.5)
                    except queue.Empty:
                        if self._hung_up():
                            return
                        if time.time() > next_ping:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            next_ping = time.time() + 5
                        continue
                    if chunk is None:
                        t.q.put(None)   # leave the end mark for any other reader
                        self._sse("exit", {})
                        return
                    # drain what else is queued so one event carries a whole redraw
                    while True:
                        try:
                            more = t.q.get_nowait()
                        except queue.Empty:
                            break
                        if more is None:
                            t.q.put(None)
                            break
                        chunk += more
                    self._sse("out", {"data": base64.b64encode(chunk).decode()})
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with TERMS_LOCK:
                    t.streams -= 1
                    t.last_stream = time.time()
                    last = t.streams == 0
                if last:   # the phone left: free the Mac pane now (already gone after DELETE: returns False)
                    close_term(t.id)

        def _hung_up(self):
            """True once the other end closed the connection (a GET sends nothing more, so readable = EOF).
            Seen within 0.5 s, instead of waiting for a ping write to fail."""
            try:
                r = select.select([self.connection], [], [], 0)[0]
                return bool(r) and not self.connection.recv(1, socket.MSG_PEEK)
            except OSError:
                return True

        def _sse(self, event, data):
            self.wfile.write(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())
            self.wfile.flush()

        def _events(self, watch):
            # No Content-Length: the body runs until the connection closes, which is valid HTTP/1.1.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            q = hub.subscribe(watch)
            last, next_ping = None, time.time() + 15
            tail, session = None, False   # False: not looked up yet (None is a real answer: a plain shell)

            def full():
                self._sse("history_full", {"pane_id": watch, "items": tail.items[-500:], "title": tail.title})

            try:
                self._sse("state", hub.snapshot())
                while True:
                    if watch:
                        # herdr has no "screen changed" event, so the watched agent is read five
                        # times a second and sent only when the text changed. Read first, so a
                        # freshly opened agent shows without waiting a tick.
                        try:
                            text = read_screen(watch)
                        except (HerdrError, OSError, ValueError, KeyError):
                            text = None
                        if text is not None and text != last:
                            last = text
                            self._sse("screen", {"pane_id": watch, "text": text})
                        # Claude Code appends each message to its transcript the moment it happens. Follow
                        # that file (one stat per tick, on this computer; nothing crosses the network unless
                        # it grew) and push new entries at once, instead of the phone re-fetching on timers.
                        try:
                            sid = session_of(hub, watch)
                            if tail is None or sid != session:
                                session, tail = sid, Tail(session=sid)
                                tail.read()
                                full()
                            else:
                                new = tail.read()
                                if new is None:   # file replaced or truncated: send it whole again
                                    tail.read()
                                    full()
                                else:
                                    for ev in new:
                                        self._sse("history", {"pane_id": watch, "item": ev["item"], "replace_last": ev["replace_last"]})
                        except (BrokenPipeError, ConnectionResetError):
                            raise   # the phone went away: stop the stream
                        except OSError:
                            pass    # transcript file mid-rename or unreadable: try again next tick
                    try:
                        kind, data = q.get(timeout=0.2)
                        if kind == "kick":
                            time.sleep(0.03)   # give Claude a moment to draw the key, then re-read at once
                            continue
                        self._sse(kind, data)
                    except queue.Empty:
                        pass
                    if time.time() > next_ping:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        next_ping = time.time() + 15
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

    return Handler


def make_server(hub, owner, port=8795):
    srv = ThreadingHTTPServer(("127.0.0.1", port), make_handler(hub, owner))
    srv.daemon_threads = True
    return srv


def owner_from_status(status):
    """The Tailscale login signed in on this computer, from `tailscale status --json`, or None."""
    uid = (status.get("Self") or {}).get("UserID")
    if uid is None:
        return None
    return ((status.get("User") or {}).get(str(uid)) or {}).get("LoginName")


def qr_lines(grid, border=2):
    """Text lines for a QR grid (grid[y][x] True = dark), two grid rows per line, on a light border.
    Each character is drawn dark-on-light: space = both light, upper/lower half = one dark, full block = both dark."""
    w = len(grid[0]) + 2 * border
    rows = [[False] * w for _ in range(border)] + [[False] * border + list(r) + [False] * border for r in grid] \
        + [[False] * w for _ in range(border)]
    if len(rows) % 2:
        rows.append([False] * w)
    return ["".join(" \u2580\u2584\u2588"[2 * b + a] for a, b in zip(rows[i], rows[i + 1]))
            for i in range(0, len(rows), 2)]


def print_qr(text):
    """Print a QR code in the terminal. Colours are set explicitly (black on white) so it reads in dark and light terminals."""
    from qrcodegen import QrCode  # only needed here, so the bridge itself never depends on it
    qr = QrCode.encode_text(text, QrCode.Ecc.MEDIUM)
    grid = [[qr.get_module(x, y) for x in range(qr.get_size())] for y in range(qr.get_size())]
    for line in qr_lines(grid):  # write UTF-8 bytes: a terminal set to plain C/ASCII would make print() fail
        sys.stdout.buffer.write(("\033[30;47m" + line + "\033[0m\n").encode("utf-8"))
    sys.stdout.flush()


CONFIG = os.path.join(HOME, ".herdr-remote", "config.json")


def main():
    ap = argparse.ArgumentParser(description="Herdr Remote bridge")
    ap.add_argument("--owner", help="your Tailscale login, e.g. you@gmail.com (saved to config)")
    ap.add_argument("--port", type=int, help="local port (default 8795)")
    ap.add_argument("--owner-from-status", action="store_true",
                    help="read `tailscale status --json` on stdin, print the signed-in login, exit")
    ap.add_argument("--qr", metavar="TEXT", help="print TEXT as a QR code in the terminal, exit")
    args = ap.parse_args()
    if args.qr:
        print_qr(args.qr)
        return
    if args.owner_from_status:
        owner = owner_from_status(json.load(sys.stdin))
        if not owner:
            sys.exit(1)
        print(owner)
        return
    cfg = {}
    if os.path.exists(CONFIG):
        with open(CONFIG) as f:
            cfg = json.load(f)
    if args.port:
        cfg["port"] = args.port
    if args.owner:   # only then is the config saved: `--port` alone is for this run (a test bridge must not move the service)
        cfg["owner"] = args.owner
        os.makedirs(os.path.dirname(CONFIG), exist_ok=True)
        with open(CONFIG, "w") as f:
            json.dump(cfg, f, indent=2)
    if not cfg.get("owner"):
        sys.exit("Set your Tailscale login once: herdr_remote.py --owner you@gmail.com")
    try:
        info = call("ping")
        if info.get("protocol") not in (None, 20):
            print("warning: herdr protocol %s, bridge was built for 20" % info.get("protocol"), file=sys.stderr)
    except (OSError, HerdrError) as e:
        print("warning: herdr not reachable yet (%s); will keep retrying" % e, file=sys.stderr)
    hub = Hub()
    threading.Thread(target=hub.run, daemon=True).start()
    threading.Thread(target=reap_loop, daemon=True).start()
    port = cfg.get("port", 8795)
    srv = make_server(hub, cfg["owner"], port)
    print("herdr-remote bridge on 127.0.0.1:%d for %s" % (port, cfg["owner"]), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
