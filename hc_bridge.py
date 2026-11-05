#!/usr/bin/env python3
"""Herdpad hc bridge: herdr-core (the engine of the new Herdr desktop app) for the iPhone app.

Answers /api/state and /api/events in the same shape as herdr_remote.py, so the app's sidebar works unchanged,
and adds /api/term/<t>/ws: a WebSocket carrying one terminal's raw bytes (hc's attach stream) both ways.
Stdlib only. Binds 127.0.0.1; reach it through `tailscale serve`, which sets Tailscale-User-Login.
"""
import argparse, base64, contextlib, hashlib, json, os, platform, queue, re, shutil, socket, struct, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HC_SOCK = os.environ.get("HC_SOCKET") or os.path.expanduser("~/.local/state/herdr-core/default/default.sock")
CONFIG = os.path.expanduser("~/.herdr-remote/config.json")   # shared with herdr_remote.py; read only here
APP = os.environ.get("HERDR_APP") or os.path.expanduser("~/.local/bin/herdr-app")   # the Herdr app's own CLI
APP_OFFLINE = "Open the Herdr app on your Mac to create, rename or close"
APP_SLOW = "Herdr app on your Mac didn't answer: open it and try again"
UPLOADS = os.path.expanduser("~/.herdr-remote/uploads")   # pictures from the phone (same folder as herdr_remote.py)
SCREEN_SOCK = os.path.expanduser("~/.herdr-remote/screen.sock")   # Herdr Screen helper (Mac only)
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
DATA, CREDIT, CLOSE, RESIZE = 0, 1, 2, 3
MAX_WS = 1 << 20   # phone input: keys and pastes, never near 1 MB
PING_S, WS_IDLE = 20, 60   # ping a quiet phone every 20 s; give up after 60 s with nothing from it


def frame(ch, kind, payload=b""):
    return struct.pack("<IBI", ch, kind, len(payload)) + payload


def read_exact(f, n):
    b = f.read(n)
    if len(b) < n:
        raise EOFError
    return b


def read_frame(f):
    ch, kind, n = struct.unpack("<IBI", read_exact(f, 9))
    return ch, kind, read_exact(f, n) if n else b""


def ws_accept(key):
    return base64.b64encode(hashlib.sha1((key + WS_GUID).encode()).digest()).decode()


def ws_frame(data, op=0x2):
    n = len(data)
    if n < 126:
        head = bytes([0x80 | op, n])
    elif n < 65536:
        head = bytes([0x80 | op, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | op, 127]) + struct.pack(">Q", n)
    return head + data


def ws_read(f, partial=None):
    """One whole message from the phone: (opcode, bytes). Continuation frames are joined. A control frame
    (ping/pong/close) between fragments is returned on its own; the half-read message waits in `partial`
    ([op, bytes], one per connection) for the next call."""
    partial = partial if partial is not None else [None, b""]
    while True:
        b0, b1 = read_exact(f, 2)
        n = b1 & 0x7F
        if n == 126:
            n = struct.unpack(">H", read_exact(f, 2))[0]
        elif n == 127:
            n = struct.unpack(">Q", read_exact(f, 8))[0]
        if n > MAX_WS or len(partial[1]) + n > MAX_WS:
            raise ValueError("websocket message over 1 MB")
        mask = read_exact(f, 4) if b1 & 0x80 else b""
        data = read_exact(f, n)
        if mask:
            data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
        op = b0 & 0x0F
        if op >= 0x8:
            return op, data
        if op:
            partial[0], partial[1] = op, b""
        partial[1] += data
        if b0 & 0x80:
            msg = (partial[0], partial[1])
            partial[0], partial[1] = None, b""
            return msg


STATUS = {"idle": "idle", "working": "working", "blocked": "blocked", "done": "done", "error": "blocked"}


def tree_terminals(node):
    if not isinstance(node, dict):
        return []
    t = (node.get("content") or {}).get("terminal")
    out = [t] if t else []
    for c in node.get("children") or []:
        out += tree_terminals(c)
    return out


def agent_row(t, tab_id, tab_label):
    st, fg = t.get("status") or {}, t.get("foreground") or {}
    app = st.get("app") or None
    if not app:   # Codex reports nothing until its first turn: name it from the program hc started
        prog = os.path.basename((t.get("argv") or [""])[0])
        app = prog if prog in ("claude", "codex") else None
    return {"pane_id": t["address"], "tab_id": tab_id, "tab_label": tab_label,
            "title": t.get("title") or fg.get("name") or t["address"],
            "agent": "claude" if app == "claude-code" else app,
            "status": STATUS.get(st.get("state"), "unknown"),
            "cwd": fg.get("cwd") or t.get("cwd")}


def build_state(layout, terminals):
    """The Herdr app's spaces and tabs, with hc's live terminals in them, shaped like herdr_remote's state."""
    live = {t["address"]: t for t in terminals if t.get("state") == "live"}
    home, placed, workspaces = os.path.expanduser("~"), set(), []
    for sp in (layout or {}).get("spaces", []):
        folder = sp.get("folder") or ""
        agents = []
        for i, tab in enumerate(sp.get("tabs", [])):
            for addr in tree_terminals(tab.get("root")):
                if addr in live and addr not in placed:
                    placed.add(addr)
                    agents.append(agent_row(live[addr], tab["id"], tab.get("name") or str(i + 1)))
        # Like the Herdr app: an unnamed space follows its first terminal's current folder, else its saved one.
        shown = (agents[0]["cwd"] if agents else None) or folder
        label = sp.get("name") or ("~" if shown.rstrip("/") in ("", home) else os.path.basename(shown.rstrip("/")))
        workspaces.append({"id": sp["id"], "label": label, "path": folder, "agents": agents})
    rest = [agent_row(t, None, t.get("name") or a) for a, t in live.items() if a not in placed]
    if rest:   # started with `hc run`, not in any space: the app shows them as peeks
        workspaces.append({"id": "hc-other", "label": "Other terminals", "path": None, "agents": rest})
    return {"herdr": "online", "engine": "hc", "workspaces": workspaces}


class HcError(Exception):
    pass


def _rpc(i, method, params):
    return json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}})


def hc_call(method, params=None, timeout=5, sock=None):
    """One JSON-RPC call on its own plain-mode connection (also the screen helper's: same line format)."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(sock or HC_SOCK)
        s.sendall((_rpc(1, method, params) + "\n").encode())
        f = s.makefile("rb")
        while True:
            line = f.readline()
            if not line:
                raise HcError("hc closed the connection")
            msg = json.loads(line)
            if msg.get("id") == 1:
                break
        if "error" in msg:
            raise HcError(msg["error"].get("message", "hc error"))
        return msg["result"]
    finally:
        s.close()


class HcStream:
    """One framed connection to hc, attached to one terminal."""

    def __init__(self, term, cols, rows):
        self.ended = b""   # hc's CLOSE payload (a JSON reason) once the stream ends
        self.sock = socket.socket(socket.AF_UNIX)
        try:
            self._attach(term, cols, rows)
        except BaseException:
            self.sock.close()
            raise

    def _attach(self, term, cols, rows):
        self.sock.settimeout(10)
        self.sock.connect(HC_SOCK)
        self.f = self.sock.makefile("rb")
        self.lock = threading.Lock()
        self.early = []   # stream bytes that came before attach's reply named the channel
        self.sock.sendall((_rpc(1, "hello", {"framed": True}) + "\n").encode())
        reply = json.loads(self.f.readline() or b"null")
        if not reply or "error" in reply:
            raise HcError("hello failed: %r" % (reply,))
        self.sock.sendall(frame(0, DATA, _rpc(2, "terminal.attach", {"terminal": term, "cols": cols, "rows": rows, "history": 1000}).encode()))
        while True:
            ch, kind, body = read_frame(self.f)
            if ch == 0 and kind == DATA:
                m = json.loads(body)
                if m.get("id") == 2:
                    if "error" in m:
                        raise HcError(m["error"].get("message", "attach failed"))
                    ch = (m.get("result") or {}).get("channel")
                    if not isinstance(ch, int):
                        raise HcError("attach reply has no channel: %r" % (m,))
                    self.ch = ch
                    break
            elif kind == DATA:
                self.early.append((ch, body))
        self.early = [b for c, b in self.early if c == self.ch]
        self.sock.settimeout(None)

    def send(self, kind, payload=b""):
        with self.lock:
            self.sock.sendall(frame(self.ch, kind, payload))

    def next_data(self):
        """The next bytes of the terminal's output; None when hc ends the stream."""
        if self.early:
            return self.early.pop(0)
        while True:
            ch, kind, body = read_frame(self.f)
            if ch != self.ch:
                continue
            if kind == DATA:
                self.send(CREDIT, struct.pack("<I", len(body)))
                return body
            if kind == CLOSE:
                self.ended = body
                return None

    def close(self):
        try:
            self.send(CLOSE)
        except OSError:
            pass
        try:
            self.sock.shutdown(socket.SHUT_RDWR)   # wakes a reader blocked on hc even if hc hangs
        except OSError:
            pass
        self.f.close()
        self.sock.close()


def close_payload(reason, code=1000):
    """WebSocket close body: the code (1000 = the program ended, 1011 = lost hc), then hc's reason cut to
    the 123 bytes a close frame allows. Always carries the code: an empty body reads as 1005 on the phone."""
    return struct.pack(">H", code) + (reason or b"")[:123].decode("utf-8", "ignore").encode()


def relay(conn, rfile, stream):
    """Pump one terminal both ways until either side ends. hc -> phone: binary messages.
    Phone -> hc: binary = keys and pastes; text = JSON control, {"resize": [cols, rows]}."""
    wlock, done, closed = threading.Lock(), threading.Event(), [False]

    def send(data, op=0x2):
        with wlock:
            if closed[0]:
                return   # nothing may follow a close frame, and the phone gets exactly one
            closed[0] = op == 0x8
            conn.sendall(ws_frame(data, op))

    def down():
        code = 1000
        try:
            while True:
                data = stream.next_data()
                if data is None:
                    break
                send(data)
        except (OSError, EOFError, ValueError):
            code = 1011   # hc's side broke, not the program: the phone offers Retry
        try:
            send(close_payload(getattr(stream, "ended", b""), code), 0x8)
        except OSError:
            pass
        try:
            conn.shutdown(socket.SHUT_RDWR)   # don't wait for a phone that may never answer
        except OSError:
            pass

    def pinger():
        while not done.wait(PING_S):
            try:
                send(b"", 0x9)
            except OSError:
                return

    threading.Thread(target=down, daemon=True).start()
    threading.Thread(target=pinger, daemon=True).start()
    conn.settimeout(WS_IDLE)   # socket.timeout is an OSError: a vanished phone ends the session below
    partial = [None, b""]
    try:
        while True:
            op, data = ws_read(rfile, partial)
            if op == 0x8:
                try:
                    send(data[:2] if len(data) >= 2 else b"", 0x8)
                except OSError:
                    pass
                break
            if op == 0x9:
                send(data, 0xA)
            elif op == 0x2:
                stream.send(DATA, data)
            elif op == 0x1:
                try:
                    msg = json.loads(data)
                except ValueError:
                    continue
                size = msg.get("resize") if isinstance(msg, dict) else None
                if (isinstance(size, list) and len(size) == 2 and all(isinstance(x, int) for x in size)
                        and 2 <= size[0] <= 1000 and 2 <= size[1] <= 1000):
                    stream.send(RESIZE, struct.pack("<HH", *size))
    except (OSError, EOFError, ValueError):
        pass
    finally:
        done.set()
        stream.close()


class Hub:
    """Keeps the state fresh: hc's event feed marks it dirty. It carries terminal changes and `doc` events
    (the Herdr app saving its layout; seen live 2026-10-30), so status and tab renames show at once.
    The 2 s re-read is only a safety net for a dropped event feed."""

    def __init__(self):
        self.lock, self.clients, self.state = threading.Lock(), set(), None
        self.dirty = threading.Event()

    def refresh(self):
        try:
            layout = (hc_call("doc.get", {"name": "herdr-app"}) or {}).get("value")
            state = build_state(layout, hc_call("terminal.list")["terminals"])
        except Exception as e:   # any odd shape from hc must not kill Hub.run: show offline with the reason
            state = {"herdr": "offline", "engine": "hc", "workspaces": [], "error": "%s: %s" % (type(e).__name__, e)}
        with self.lock:
            if state == self.state:
                return
            self.state = state
            for q in self.clients:
                q.put(state)

    def run(self):
        threading.Thread(target=self.watch, daemon=True).start()
        while True:
            self.refresh()
            self.dirty.wait(2)
            self.dirty.clear()
            time.sleep(0.15)   # a burst of events -> one refresh

    def watch(self):
        """hc's live event feed (plain mode): any terminal change marks the state dirty."""
        while True:
            try:
                with socket.socket(socket.AF_UNIX) as s:
                    s.connect(HC_SOCK)
                    s.sendall((_rpc(1, "events.subscribe", {}) + "\n").encode())
                    with s.makefile("rb") as f:
                        for line in f:
                            if b'"method":"event"' in line.replace(b" ", b""):
                                self.dirty.set()
            except OSError:
                pass
            time.sleep(2)   # hc down or restarting: try again

    def subscribe(self):
        q = queue.Queue()
        with self.lock:
            self.clients.add(q)
            if self.state:
                q.put(self.state)
        return q

    def unsubscribe(self, q):
        with self.lock:
            self.clients.discard(q)


class AppOffline(Exception):
    pass


def app(*argv):
    """Run `herdr-app <argv>` against the open Herdr window and parse its JSON. Seen live (2026-10-30):
    exit 0 = the result, 1 = {"error": ...}, 2 = "no Herdr window" (the app isn't running; `do` never opens it)."""
    try:
        r = subprocess.run([APP] + list(argv), capture_output=True, text=True, timeout=10)
    except subprocess.TimeoutExpired:
        raise AppOffline(APP_SLOW)
    except OSError:   # not installed, not runnable
        raise AppOffline(APP_OFFLINE)
    if r.returncode == 2:   # "no Herdr window at ...": couldn't reach the app
        raise AppOffline(APP_OFFLINE)
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise HcError((r.stderr or r.stdout).strip() or "herdr-app exited %d" % r.returncode)
    if r.returncode or (isinstance(out, dict) and "error" in out):
        raise HcError(str(out.get("error") if isinstance(out, dict) else out))
    return out


def app_do(cmd, args):
    """One app command, its arguments as one JSON object (no --flag parsing of names)."""
    return app("do", cmd, json.dumps(args))


def keep_window(create):
    """space.new / tab.new switch the Mac window (seen live); the old herdr didn't (focus: false).
    Put the window back on the space and tab it showed. A failed restore is ignored: the create worked."""
    try:
        shown = app("status")
    except HcError:
        shown = {}
    if not isinstance(shown, dict):
        shown = {}
    made = create()
    try:
        if shown.get("space"):
            app_do("space.select", {"id": shown["space"]})
        if shown.get("tab"):   # a new tab in the shown space: space.select alone would stay on it
            app_do("tab.select", {"id": shown["tab"]})
    except (HcError, AppOffline):
        pass
    return made


# The app's ids, as `herdr-app ls` shows them (sp/tb + 32 hex). herdr-app takes a missing id as the
# focused space or tab, and close is confirmed: an empty or pseudo id ("hc-other") must never reach it.
APP_ID = {"workspace": re.compile(r"sp[0-9a-f]+"), "tab": re.compile(r"tb[0-9a-f]+")}


AGENT_ARGV = {"claude": ["claude"], "codex": ["codex"], "shell": None}   # shell: the plain terminal, no agent


def _kind(body):
    kind = body.get("kind", "claude")
    if kind not in AGENT_ARGV:
        raise ValueError("unknown agent kind")
    return AGENT_ARGV[kind]


def new_space(body):
    """Old POST /api/workspaces. Like herdr_remote: in ~ unless cwd. Seen live: the window switches to it."""
    args = {"folder": os.path.expanduser(str(body.get("cwd") or "~"))}
    if body.get("label"):
        args["name"] = str(body["label"])
    argv = _kind(body)
    if argv:
        args["argv"] = argv
    return keep_window(lambda: app_do("space.new", args))["terminal"]


def new_tab(hub, space, body):
    """Old POST /api/workspaces/<id>/tabs: in the space's folder unless cwd (the app's default is the
    focused terminal's, which may be another space's). Seen live: the window switches to the new tab."""
    argv, cwd = _kind(body), body.get("cwd")
    if not cwd:
        ws = next((w for w in (hub.state or {}).get("workspaces", []) if w["id"] == space), None)
        cwd = ws and ws["path"]
    args = {"space": space}
    if cwd:
        args["folder"] = os.path.expanduser(str(cwd))
    if argv:
        args["argv"] = argv
    return keep_window(lambda: app_do("tab.new", args))["terminal"]


def move_space(space, before):
    """Old move: before a space id (None = last). herdr-app wants the final position, 1 = first."""
    others = [s["id"] for s in app("ls")["spaces"] if s["id"] != space]
    # None, or not an app space (e.g. "hc-other", shown last on the phone): last
    app_do("space.move", {"id": space, "index": others.index(before) + 1 if before in others else len(others) + 1})


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


IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG")   # JPEG, PNG
MAX_IMAGE, MAX_VIDEO = 10 << 20, 200 << 20


def save_upload(src, n):
    """A picture or video uploaded on attach, so the send itself only carries its path. Read from `src` in
    1 MB pieces straight to the file (a 200 MB video never sits in memory); a wrong type, too big or a short
    body leaves no file. Videos are MP4/MOV (ISO-BMFF: "ftyp" at bytes 4-8), shrunk on the phone first."""
    head = src.read(max(0, min(n, 12)))
    if head.startswith(IMAGE_MAGIC):
        ext, limit = "png" if head.startswith(b"\x89") else "jpg", MAX_IMAGE
    elif head[4:8] == b"ftyp":
        ext, limit = "mov" if head[8:12] == b"qt  " else "mp4", MAX_VIDEO
    else:
        raise ValueError("not a JPEG/PNG under 10 MB or an MP4/MOV under 200 MB")
    if n > limit:
        raise ValueError("over %d MB" % (limit >> 20))
    os.makedirs(UPLOADS, exist_ok=True)
    path = os.path.join(UPLOADS, "%d-%s.%s" % (time.time() * 1000, os.urandom(2).hex(), ext))
    try:
        with open(path, "wb") as f:
            f.write(head)
            left = n - len(head)
            while left > 0:
                chunk = src.read(min(left, 1 << 20))
                if not chunk:
                    raise ValueError("body ended %d bytes short" % left)
                f.write(chunk)
                left -= len(chunk)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):   # open() itself may have failed: keep its error
            os.remove(path)
        raise
    return path


def _send(t, b):
    """Picture paths (from /api/uploads) then the text, as one paste + Enter: Claude opens image files from a path."""
    given = b.get("paths") or []
    if b.get("images"):
        raise ValueError("send pictures through /api/uploads")
    if not isinstance(given, list) or len(given) > 5 or not all(isinstance(p, str) for p in given):
        raise ValueError("paths must be a list of at most 5 strings")
    given = [os.path.realpath(p) for p in given]   # only files directly in UPLOADS (no ../, no symlink out)
    if any(not os.path.isfile(p) or os.path.dirname(p) != os.path.realpath(UPLOADS) for p in given):
        raise ValueError("paths must be files sent to /api/uploads")
    text = " ".join(given + [str(b.get("text") or "").strip()]).strip()
    if not text:
        raise ValueError("nothing to send")
    return ACTIONS["prompt"](t, {"text": text})


def screen(method, params=None):
    """Ask the Herdr Screen helper, like herdr_remote.py; the video then goes from it straight to the phone."""
    if sys.platform != "darwin":
        return {"status": "unsupported", "message": "Remote desktop on Linux is coming soon. Chats work as usual."}
    if not os.path.exists(SCREEN_SOCK):
        return {"status": "not_installed"}
    try:
        return hc_call(method, params, timeout=5, sock=SCREEN_SOCK)
    except (ConnectionRefusedError, FileNotFoundError):
        return {"status": "not_running"}


ACTIONS = {
    "keys": lambda t, b: hc_call("terminal.keys", {"terminal": t, "keys": [str(k) for k in b["keys"]]}),
    "prompt": lambda t, b: hc_call("terminal.send", {"terminal": t, "text": str(b["text"])}, timeout=30),
    "answer": lambda t, b: hc_call("terminal.keys", {"terminal": t, "keys": [str(int(b["choice"]))]}),
}
ACTIONS["send"] = _send


def make_server(hub, owner, port):
    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *a):
            sys.stderr.write("%s %s\n" % (time.strftime("%H:%M:%S"), fmt % a))

        def _json(self, code, obj):
            data = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _err(self, code, kind, msg):
            self._json(code, {"error": kind, "message": msg})

        def _authed(self):
            # same rule as herdr_remote.py: tailscale serve sets this header; we only listen on 127.0.0.1
            if owner is not None and self.headers.get("Tailscale-User-Login") != owner:
                self.close_connection = True
                self._err(403, "forbidden", "this bridge only answers its owner")
                return False
            return True

        def do_GET(self):
            if not self._authed():
                return
            u = urlparse(self.path)
            parts = u.path.strip("/").split("/")
            if u.path == "/api/state":
                return self._json(200, hub.state or {"herdr": "offline", "engine": "hc", "workspaces": []})
            if u.path == "/api/events":
                return self._events()
            if len(parts) == 4 and parts[:2] == ["api", "term"] and parts[3] == "ws":
                return self._ws(unquote(parts[2]), parse_qs(u.query))
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
            ok = {"ok": True}
            try:
                n = max(0, int(self.headers.get("Content-Length") or 0))
                if parts == ["api", "uploads"]:   # raw picture or video bytes, not JSON
                    try:
                        path = save_upload(self.rfile, n)
                    except Exception:   # refused, short, or a disk error mid-write
                        self.close_connection = True   # the body was not (all) read: never parse its rest
                        raise
                    return self._json(200, {"path": path})
                body = json.loads(self.rfile.read(n) or b"{}")
                if not isinstance(body, dict):
                    raise ValueError("body must be a JSON object")
                kind = parts[1][:-1] if len(parts) == 4 and parts[:2] in (["api", "workspaces"], ["api", "tabs"]) else None
                ident = unquote(parts[2]) if kind else None   # a space or tab id
                if kind and not APP_ID[kind].fullmatch(ident):
                    raise ValueError("no such %s" % ("space" if kind == "workspace" else "tab"))
                if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] in ACTIONS:
                    ACTIONS[parts[3]](unquote(parts[2]), body)
                elif parts == ["api", "clipboard"]:
                    clip_set(str(body["text"]))
                elif parts == ["api", "screen", "start"]:
                    kbps, fps = int(body.get("bitrate_kbps", 4000)), int(body.get("fps", 60))
                    if not (500 <= kbps <= 20000 and 1 <= fps <= 120):
                        raise ValueError("bitrate_kbps 500-20000, fps 1-120")
                    return self._json(200, screen("screen.start", {"bitrate_kbps": kbps, "fps": fps}))
                elif parts == ["api", "screen", "stop"]:
                    return self._json(200, screen("screen.stop"))
                elif parts == ["api", "workspaces"]:
                    ok = {"ok": True, "pane_id": new_space(body)}
                elif kind == "workspace" and parts[3] == "tabs":
                    ok = {"ok": True, "pane_id": new_tab(hub, ident, body)}
                elif kind == "workspace" and parts[3] == "move":
                    move_space(ident, body.get("before"))
                elif kind and parts[3] == "close":   # ends whatever runs in it; confirmed = no dialog on the Mac
                    app_do("space.close" if kind == "workspace" else "tab.close", {"id": ident, "confirmed": True})
                elif kind and parts[3] == "rename":
                    label = str(body["label"]).strip()
                    if not label:   # herdr-app would take "" as "back to the default name"; the old bridge refused it
                        raise ValueError("label is empty")
                    if kind == "workspace":   # space.rename moved the Mac window (seen 2026-10-30): keep it put
                        keep_window(lambda: app_do("space.rename", {"id": ident, "name": label}))
                    else:
                        app_do("tab.rename", {"id": ident, "name": label})
                else:   # api/watch, text, term...: not on hc. 404 makes the app use its fallbacks.
                    return self._err(404, "not_found", self.path)
                hub.dirty.set()
                return self._json(200, ok)
            except AppOffline as e:
                self._err(503, "herdr_offline", str(e))
            except HcError as e:
                self._err(409, "hc", str(e))
            except (KeyError, ValueError, TypeError, OverflowError, AttributeError) as e:
                self._err(400, "bad_request", str(e))
            except (OSError, subprocess.SubprocessError) as e:   # incl. herdr-app taking over 10 s
                self._err(503, "herdr_offline", str(e))

        def _events(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = hub.subscribe()
            try:
                while True:
                    try:
                        state = q.get(timeout=15)
                        self.wfile.write(b"event: state\ndata: " + json.dumps(state).encode() + b"\n\n")
                    except queue.Empty:
                        self.wfile.write(b"event: ping\ndata: {}\n\n")   # the app gives up after 35 s of silence
                    self.wfile.flush()
            except OSError:
                pass
            finally:
                hub.unsubscribe(q)
                self.close_connection = True

        def _ws(self, term, qs):
            key = self.headers.get("Sec-WebSocket-Key")
            if not key or (self.headers.get("Upgrade") or "").lower() != "websocket":
                return self._err(400, "bad_request", "websocket only")
            origin = self.headers.get("Origin")   # browsers don't apply CORS to WebSockets; the app sends no Origin
            if origin and urlparse(origin).netloc != self.headers.get("Host"):
                return self._err(403, "forbidden", "cross-site websocket")
            try:
                cols, rows = int(qs.get("cols", ["80"])[0]), int(qs.get("rows", ["24"])[0])
                stream = HcStream(term, max(2, min(cols, 1000)), max(2, min(rows, 1000)))
            except (OSError, HcError, EOFError, ValueError) as e:
                return self._err(409, "attach_failed", str(e))
            self.close_connection = True
            try:
                self.send_response(101)
                self.send_header("Upgrade", "websocket")
                self.send_header("Connection", "Upgrade")
                self.send_header("Sec-WebSocket-Accept", ws_accept(key))
                self.end_headers()
                self.wfile.flush()
            except OSError:
                stream.close()   # phone gone mid-handshake: don't leave the hc attach open
                return
            relay(self.connection, self.rfile, stream)

    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    return srv


def main():
    ap = argparse.ArgumentParser(description="Herdpad hc bridge")
    ap.add_argument("--port", type=int, default=8798)
    ap.add_argument("--dev", action="store_true", help="no Tailscale login check (127.0.0.1 only), for local tests")
    args = ap.parse_args()
    owner = None
    if not args.dev:
        try:
            with open(CONFIG) as f:
                owner = json.load(f)["owner"]
        except (OSError, KeyError, ValueError, TypeError):
            owner = None
        if not (isinstance(owner, str) and owner.strip()):
            sys.exit("No owner in %s: set it once with herdr_remote.py --owner you@gmail.com" % CONFIG)
    hub = Hub()
    threading.Thread(target=hub.run, daemon=True).start()
    srv = make_server(hub, owner, args.port)
    print("hc bridge on 127.0.0.1:%d for %s (hc at %s)" % (args.port, owner or "anyone (dev)", HC_SOCK), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
