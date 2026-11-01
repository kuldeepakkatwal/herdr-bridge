#!/usr/bin/env python3
"""herdr_screen.py: shows this Linux (X11) desktop in the Herdr iPhone app and takes its pointer and keys.

Linux twin of mac-screen/ (Swift). Same control socket (~/.herdr-remote/screen.sock, one JSON line in, one out)
and the same UDP wire format v1 (mac-screen/Core/ScreenWire.swift; golden vectors in test_herdr_screen.py).
Runs as a systemd user service with DISPLAY=:0. Runtime needs python3-gi + GStreamer (vaapi or x264) and
python3-xlib; those are imported only by the runtime classes at the bottom, so the pure parts test anywhere.
Cursor shapes whose PNG is over 1160 bytes (one datagram) are skipped; the phone keeps its last arrow.
"""
import base64, hmac, ipaddress, json, os, secrets, socket, struct, subprocess, sys, threading, time, zlib

VERSION = 1
MAX_DGRAM = 1200          # Tailscale MTU 1280 minus IPv6+UDP headers, with margin
HDR = 8
VIDEO_CHUNK = 1176        # MAX_DGRAM - HDR - 16 (video header)
MAX_CURSOR_PNG = 1160
KEY_LEN = 16
MAX_FRAGS = 2048
VAAPI_KEYFRAME_PERIOD = 4294967295   # gst-inspect max on the Acer; keyframes come from request_keyframe

T_VIDEO, T_CURSOR, T_CURSOR_SHAPE, T_STATUS, T_PONG, T_DISPLAYS, T_CARET = 0x01, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08
(T_HELLO, T_KEYFRAME, T_MOVE, T_BUTTON, T_SCROLL, T_KEY, T_TEXT, T_PING, T_SETTINGS, T_BYE,
 T_SHOW) = range(0x10, 0x1B)
PATH_UNKNOWN, PATH_DIRECT, PATH_PEER_RELAY, PATH_DERP = 0, 1, 2, 3


# ---- host -> phone messages: (type, flags, body) ----

def msg_video(frame_id, idx, count, capture_us, key, data):
    return T_VIDEO, 1 if key else 0, struct.pack(">IHHQ", frame_id, idx, count, capture_us) + data

def msg_cursor(x, y, visible, shape_id):
    return T_CURSOR, 0, struct.pack(">ffBI", x, y, 1 if visible else 0, shape_id)

def msg_cursor_shape(shape_id, hot_x, hot_y, w, h, png):
    return T_CURSOR_SHAPE, 0, struct.pack(">IHHHH", shape_id, hot_x, hot_y, w, h) + png

def msg_status(w, h, kbps, input_ok, path, pipeline_us):
    return T_STATUS, 0, struct.pack(">HHIBBI", w, h, kbps, 1 if input_ok else 0, path, pipeline_us)

def msg_pong(phone_us, host_us):
    return T_PONG, 0, struct.pack(">QQ", phone_us, host_us)

def msg_displays(current, front_on, names, front_app):
    return (T_DISPLAYS, 0, struct.pack(">BB", current, 0xFF if front_on is None else front_on)
            + "\n".join(names).encode() + b"\x00" + front_app.encode())

def msg_caret(x, y, h, visible):
    return T_CARET, 0, struct.pack(">fffB", x, y, h, 1 if visible else 0)

def encode(seq, msg):
    t, flags, body = msg
    return struct.pack(">BBBBI", VERSION, t, flags, 0, seq & 0xFFFFFFFF) + body


# ---- phone -> host decode: (seq, type, fields) or None for anything malformed ----

_FIXED = {T_HELLO: KEY_LEN, T_KEYFRAME: 0, T_MOVE: 8, T_SCROLL: 8, T_KEY: 9, T_PING: 8, T_SETTINGS: 5, T_BYE: 0,
          T_SHOW: 3}

def decode(d):
    if not HDR <= len(d) <= MAX_DGRAM:
        return None
    v, t, _flags, _r, seq = struct.unpack_from(">BBBBI", d)
    b = bytes(d[HDR:])
    if v != VERSION or (t in _FIXED and len(b) != _FIXED[t]):
        return None
    if t == T_HELLO:
        f = (b,)
    elif t in (T_KEYFRAME, T_BYE):
        f = ()
    elif t in (T_MOVE, T_SCROLL):
        f = struct.unpack(">ff", b)
    elif t == T_PING:
        f = struct.unpack(">Q", b)
    elif t == T_SETTINGS:
        f = struct.unpack(">IB", b)
    elif t == T_SHOW:
        f = struct.unpack(">HB", b)
    elif t == T_KEY:
        e, kind, val, down, mods = struct.unpack(">HBIBB", b)
        if kind > 1:
            return None
        f = (e, kind, val, down != 0, mods)
    elif t == T_BUTTON:
        if len(b) not in (5, 6):   # 6th byte (mods) only when a modifier is held
            return None
        e, btn, down, clicks = struct.unpack_from(">HBBB", b)
        f = (e, btn, down != 0, clicks, b[5] if len(b) == 6 else 0)
    elif t == T_TEXT:
        if len(b) < 2:
            return None
        try:
            f = (struct.unpack_from(">H", b)[0], b[2:].decode("utf-8"))
        except UnicodeDecodeError:
            return None
    else:
        return None
    return seq, t, f


# ---- video ----

def fragments(frame_id, capture_us, key, data):
    n = max(1, -(-len(data) // VIDEO_CHUNK))
    return [msg_video(frame_id, i, n, capture_us, key, data[i * VIDEO_CHUNK:(i + 1) * VIDEO_CHUNK]) for i in range(n)]

def nal_units(data):
    """Annex-B bytes -> NAL units. A 4-byte start code leaves a 00 on the previous unit; NALs never end in 00."""
    starts, i = [], 0
    while (j := data.find(b"\x00\x00\x01", i)) >= 0:
        starts.append(j + 3)
        i = j + 3
    out = []
    for k, s in enumerate(starts):
        nal = data[s:starts[k + 1] - 3] if k + 1 < len(starts) else data[s:]
        nal = nal.rstrip(b"\x00")
        if nal:
            out.append(nal)
    return out

def to_avcc(annexb):
    """One access unit -> (keyframe, phone frame bytes), or None for a keyframe missing SPS/PPS."""
    sps = pps = None
    body, key = [], False
    for nal in nal_units(annexb):
        typ = nal[0] & 0x1F
        if typ == 7:
            sps = nal
        elif typ == 8:
            pps = nal
        elif typ in (1, 5):
            key |= typ == 5
            body.append(struct.pack(">I", len(nal)) + nal)
    out = b"".join(body)
    if key:
        if not (sps and pps):
            return None
        out = b"\x02" + struct.pack(">H", len(sps)) + sps + struct.pack(">H", len(pps)) + pps + out
    return key, out


# ---- session ----

_TAILNETS = (ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48"))

def is_tailnet(ip):
    try:
        a = ipaddress.ip_address(ip.split("%")[0])
    except ValueError:
        return False
    return any(a.version == n.version and a in n for n in _TAILNETS)

class Session:
    """The bridge (which checked the owner's Tailscale login) gives the phone a one-time key. WireGuard proves
    which tailnet device sent a packet, so locking to the phone's ip:port is enough after that."""

    def __init__(self, key, now, key_ttl=30, idle=10):
        self.key, self.created, self.key_ttl, self.idle = key, now, key_ttl, idle
        self.peer, self.last_seen, self.recent = None, now, []

    def expired(self, now):
        return now - self.created > self.key_ttl if self.peer is None else now - self.last_seen > self.idle

    def check(self, t, fields, frm, now):
        if self.expired(now):
            return "reject"
        if self.peer is not None:
            if frm != self.peer or (t == T_HELLO and not hmac.compare_digest(fields[0], self.key)):
                return "reject"
            self.last_seen = now
            return "accept"
        if t != T_HELLO or not is_tailnet(frm[0]) or not hmac.compare_digest(fields[0], self.key):
            return "reject"
        self.peer, self.last_seen = frm, now
        return "new_peer"

    def is_duplicate(self, event_id):
        if event_id in self.recent:
            return True
        self.recent.append(event_id)
        del self.recent[:-64]
        return False


# ---- pure helpers ----

def tailscale_path(status_json, peer_ip):
    """`tailscale status --json` -> how packets reach `peer_ip` (same rule as mac-screen Logic.swift)."""
    try:
        peers = json.loads(status_json)["Peer"].values()
    except Exception:
        return PATH_UNKNOWN
    for p in peers:
        if peer_ip in (p.get("TailscaleIPs") or []):
            if p.get("CurAddr"):
                return PATH_DIRECT
            return PATH_PEER_RELAY if p.get("PeerRelay") else PATH_DERP
    return PATH_UNKNOWN

def order_monitors(mons):
    """[(output, x, y, w, h)] -> left to right, the laptop panel named "Built-in" like on the Mac."""
    return [("Built-in" if n.startswith(("eDP", "LVDS", "DSI")) else n, x, y, w, h)
            for n, x, y, w, h in sorted(mons, key=lambda m: (m[1], m[2]))]

def to_root(mon, fx, fy):
    _, x, y, w, h = mon
    fx, fy = min(max(fx, 0.0), 1.0), min(max(fy, 0.0), 1.0)
    return x + round(fx * (w - 1)), y + round(fy * (h - 1))

def to_unit(mon, rx, ry):
    _, x, y, w, h = mon
    return (rx - x) / max(1, w - 1), (ry - y) / max(1, h - 1), x <= rx < x + w and y <= ry < y + h

NOTCH = 40.0   # ponytail: phone points per wheel click; tune on the phone if scrolling feels slow or fast

class Scroller:
    """Phone scroll points -> X wheel buttons (4 up, 5 down, 6 left, 7 right), carrying fractions."""

    def __init__(self):
        self.rest = [0.0, 0.0]

    def clicks(self, dx, dy):
        out = []
        for axis, d, pos, neg in ((1, dy, 4, 5), (0, dx, 6, 7)):
            self.rest[axis] += d
            n = int(self.rest[axis] / NOTCH)
            self.rest[axis] -= n * NOTCH
            out += [pos if n > 0 else neg] * min(abs(n), 20)
        return out

# Wire.NamedKey values -> X keysyms
NAMED_KEYSYMS = {1: 0xFF1B, 2: 0xFF09, 3: 0xFF0D, 4: 0xFF08, 5: 0xFFFF, 6: 0xFF51, 7: 0xFF53, 8: 0xFF52, 9: 0xFF54,
                 10: 0xFF50, 11: 0xFF57, 12: 0xFF55, 13: 0xFF56, 14: 0x20, **{21 + i: 0xFFBE + i for i in range(12)}}
# Phone modifier bits -> keysyms. Command maps to Control so the phone's ⌘C / ⌘V copy and paste as on the Mac.
_MOD_BITS = ((1, 0xFFE1), (2, 0xFFE3), (4, 0xFFE9), (8, 0xFFE3))

def mod_keysyms(mods):
    return sorted({ks for bit, ks in _MOD_BITS if mods & bit})

def char_keysym(cp):
    if cp in (0x0A, 0x0D):
        return 0xFF0D
    if cp == 0x09:
        return 0xFF09
    if 0x20 <= cp <= 0x7E or 0xA0 <= cp <= 0xFF:
        return cp
    return 0x01000000 | cp

def argb_to_rgba(pixels):
    """XFixes cursor pixels (premultiplied ARGB u32) -> straight RGBA bytes."""
    out = bytearray()
    for p in pixels:
        a, r, g, b = (p >> 24) & 255, (p >> 16) & 255, (p >> 8) & 255, p & 255
        if 0 < a < 255:
            r, g, b = (min(255, c * 255 // a) for c in (r, g, b))
        out += bytes((r, g, b, a))
    return bytes(out)

def png_rgba(w, h, rgba):
    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + rgba[y * w * 4:(y + 1) * w * 4] for y in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


# ---- transport ----

class Transport:
    """UDP on the tailnet address. One peer at a time; seq numbers per direction."""

    def __init__(self, on_packet):
        self.on_packet, self.sock, self.peer = on_packet, None, None
        self.seq = self.frame_id = self.gen = 0
        self.lock = threading.Lock()

    def bind(self, ip, port):
        if self.sock and self.sock.getsockname()[0] == ip and (port == 0 or self.sock.getsockname()[1] == port):
            return self.sock.getsockname()[1]
        self.gen += 1   # the old receiver thread sees this within its 1 s timeout and exits
        if self.sock:
            self.sock.close()
            self.sock = None
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 << 20)
            s.bind((ip, port))
        except OSError:
            s.close()
            raise
        s.settimeout(1)   # close() does not wake recvfrom on Linux
        self.sock = s
        threading.Thread(target=self._rx, args=(s, self.gen), daemon=True, name="herdr.rx").start()
        return s.getsockname()[1]

    def _rx(self, s, gen):
        while gen == self.gen:
            try:
                d, a = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError as e:
                if s.fileno() == -1:
                    return
                print(f"receive failed: {e!r}", flush=True)
                continue
            try:
                p = decode(d)
                if p:
                    self.on_packet(p, (a[0], a[1]))
            except Exception as e:   # one bad packet must not leave the helper "ready" with no receiver
                print(f"packet failed: {e!r}", flush=True)

    def send(self, msg):
        with self.lock:
            if not (self.sock and self.peer):
                return
            self.seq = (self.seq + 1) & 0xFFFFFFFF
            try:
                self.sock.sendto(encode(self.seq, msg), self.peer)
            except OSError:
                pass   # a full buffer drops the packet; the phone asks for a keyframe

    def send_frame(self, annexb, capture_us):
        """Returns False when the frame was unusable and a keyframe should be requested."""
        r = to_avcc(annexb)
        if r is None:
            return False
        key, data = r
        if len(data) > VIDEO_CHUNK * MAX_FRAGS:
            return False
        self.frame_id = (self.frame_id + 1) & 0xFFFFFFFF
        # ponytail: no send pacing; add a token bucket if keyframe bursts lose packets on DERP (same as the Mac).
        for m in fragments(self.frame_id, capture_us, key, data):
            self.send(m)
        return True


# ---- control socket ----

class ControlError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code

def serve_control(path, handle, background=False):
    """herdr-style: one request per connection, one JSON line in, one line out."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.bind(path)
    os.chmod(path, 0o600)
    s.listen(8)

    def loop():
        while True:
            c, _ = s.accept()
            with c:
                c.settimeout(5)
                try:
                    line = c.makefile("rb").readline(65536)
                    req = json.loads(line)
                    reply = handle(req) if isinstance(req, dict) else None
                except (OSError, ValueError):
                    reply = None
                if reply is None:
                    reply = {"id": None, "error": {"code": "bad_request", "message": "expected one JSON object per line"}}
                try:
                    c.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass

    if background:
        threading.Thread(target=loop, daemon=True, name="herdr.control").start()
    else:
        loop()


# ---- host ----

def _clamp(v, lo, hi):
    return min(max(v, lo), hi)

def _micros():
    return time.monotonic_ns() // 1000

class Host:
    """One screen session at a time. All state behind self.lock (the Swift version uses one queue).
    Slow calls (X11 check, Tailscale IP, monitor query) run outside the lock so packets are never held up."""

    MAX_FPS = 30   # ponytail: the Acer is a 4-core laptop already 70% busy; raise for stronger machines

    def __init__(self, port, capture, input_, cursor, tailnet_ip, monitors, x11_ok, allow_any_peer=False,
                 unsupported=None):
        self.port, self.capture, self.input, self.cursor = port, capture, input_, cursor
        self.unsupported = unsupported   # why capture/input could not load (missing gi, xlib, XTEST)
        self.query_ip, self.list_monitors, self.x11_ok = tailnet_ip, monitors, x11_ok
        self.allow_any_peer = allow_any_peer   # tests only: 127.0.0.1 is not a tailnet address
        self.lock = threading.RLock()
        self.transport = Transport(self._packet)
        self.session, self.live, self.ip = None, False, None
        self.kbps, self.fps = 4000, self.MAX_FPS
        self.monitors, self.shown, self.shown_mon, self.path, self.ticks = [], 0, None, PATH_UNKNOWN, 0
        threading.Thread(target=self._timer, daemon=True, name="herdr.tick").start()

    def tailnet_ip(self):
        if not self.ip:   # the Tailscale address does not change; ask again only until we have one
            self.ip = self.query_ip()
        return self.ip

    # control methods (control thread)
    def request(self, req):
        rid, params = req.get("id"), req.get("params") or {}
        try:
            m = req.get("method")
            if m in ("screen.start", "screen.status"):
                x11, ip = self.x11_ok(), self.tailnet_ip()
            with self.lock:
                if m == "screen.start":
                    result = self._start(params, x11, ip)
                elif m == "screen.stop":
                    self._end()
                    result = {"ok": True}
                elif m == "screen.status":
                    result = self._status(x11, ip)
                else:
                    raise ControlError("unknown_method", str(m))
            return {"id": rid, "result": result}
        except ControlError as e:
            return {"id": rid, "error": {"code": e.code, "message": str(e)}}
        except Exception as e:   # never take the helper down for one bad request
            return {"id": rid, "error": {"code": "error", "message": repr(e)}}

    def _start(self, params, x11, ip):
        self._end()
        self.kbps = _clamp(int(params.get("bitrate_kbps", 4000)), 500, 20000)
        self.fps = _clamp(int(params.get("fps", self.MAX_FPS)), 1, self.MAX_FPS)
        if self.unsupported or not x11:
            return {"status": "unsupported", "message": self.unsupported
                    or "This computer has no X11 desktop on :0. Wayland is not supported yet."}
        if not ip:
            raise ControlError("no_tailnet", "no Tailscale 100.x address on this computer")
        try:
            port = self.transport.bind(ip, self.port)
        except OSError as e:
            raise ControlError("bind_failed", str(e))
        key = secrets.token_bytes(KEY_LEN)
        self.session = Session(key, time.monotonic())
        if self.allow_any_peer:
            self.session.check = _any_peer_check(self.session)
        print(f"session ready on {ip}:{port} ({self.kbps} kbps, {self.fps} fps)", flush=True)
        return {"status": "ready", "host": ip, "port": port, "key": base64.b64encode(key).decode(), "input": True}

    def _status(self, x11, ip):
        r = {"live": self.live, "port": self.port, "input": True, "host": ip,
             "status": "ready" if x11 and not self.unsupported else "unsupported"}
        if self.unsupported:
            r["message"] = self.unsupported
        if self.live:
            r["video"] = list(self.capture.size)
        return r

    # packets (rx thread)
    def _packet(self, p, frm):
        seq, t, f = p
        with self.lock:
            s = self.session
            if not s:
                return
            verdict = s.check(t, f, frm, time.monotonic())
            if verdict == "reject":
                return
            try:
                self._dispatch(s, verdict, t, f, frm)
            except Exception as e:   # no half-live state: a failed start ends the session, the next one starts clean
                print(f"session failed: {e!r}", flush=True)
                self._end()

    def _dispatch(self, s, verdict, t, f, frm):
        if verdict == "new_peer":
            self._begin_live(frm)
        if t in (T_HELLO, T_KEYFRAME):
            if self.capture.size[0]:
                self.capture.request_keyframe()
        elif t == T_PING:
            self.transport.send(msg_pong(f[0], _micros()))
        elif t == T_SETTINGS:
            self.kbps, self.fps = _clamp(f[0], 500, 20000), _clamp(f[1], 1, self.MAX_FPS)
            self._start_capture()   # ponytail: restart to change rate; first frame is a keyframe anyway
        elif t == T_BYE:
            self._end()
        elif t == T_SHOW:
            if not s.is_duplicate(f[0]) and 0 <= f[1] < len(self.monitors) and f[1] != self.shown:
                self.shown = f[1]
                self._start_capture()
        elif t in (T_MOVE, T_SCROLL):
            self.input.handle(t, f)
        elif t in (T_BUTTON, T_KEY, T_TEXT):
            if not s.is_duplicate(f[0]):
                self.input.handle(t, f)

    def _begin_live(self, peer):
        self.live, self.ticks = True, 0
        self.transport.peer = peer
        print(f"phone {peer[0]}:{peer[1]} connected", flush=True)
        self.input.reset()
        self.monitors = self._query_monitors() or self.monitors   # once per connect; the timer polls unlocked
        self.shown = 0
        if self.cursor:
            self.cursor.start(self)
        threading.Thread(target=self._lookup_path, args=(peer[0],), daemon=True).start()
        self._start_capture()

    def _start_capture(self):
        if not self.monitors:
            return
        self.shown = min(self.shown, len(self.monitors) - 1)
        mon = self.shown_mon = self.monitors[self.shown]
        self.input.mon = mon
        self.capture.start(mon, self.kbps, self.fps)
        self._send_status()

    def _restart_capture(self):
        """For the timer thread, which has no _packet try/except around it: a failed start must not leave
        live=True with no pipeline (a frozen screen)."""
        try:
            self._start_capture()
        except Exception as e:
            print(f"capture restart failed: {e!r}", flush=True)
            self._end()

    def _end(self):
        self.session = None
        if not self.live:
            return
        print("session ended", flush=True)
        self.live = False
        self.transport.peer = None
        self.capture.stop()
        if self.cursor:
            self.cursor.stop()
        self.input.release_all()

    def _send_status(self):
        w, h = self.capture.size
        if not w:
            return
        self.transport.send(msg_status(w, h, self.kbps, True, self.path, 0))   # ponytail: pipeline time not measured
        self.transport.send(msg_displays(self.shown, None, [m[0] for m in self.monitors], ""))

    def _query_monitors(self):
        try:
            return order_monitors(self.list_monitors())
        except Exception as e:
            print(f"monitor list failed: {e!r}", flush=True)
            return None

    def _poll_monitors(self):
        """Query outside the lock, then follow the shown monitor by its name (its index moves when one is
        plugged in on its left) and restart capture only when its position or size changed."""
        mons = self._query_monitors()
        if not mons:
            return
        with self.lock:
            if not self.live:
                return
            self.monitors = mons
            old = self.shown_mon
            names = [m[0] for m in mons]
            self.shown = names.index(old[0]) if old and old[0] in names else 0
            if mons[self.shown] != old:
                self._restart_capture()
            else:
                self._send_status()

    def _lookup_path(self, ip):
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, timeout=5).stdout
        except Exception:
            return
        with self.lock:
            self.path = tailscale_path(out, ip)

    def _timer(self):
        while True:
            time.sleep(1)
            try:
                self._tick()
            except Exception as e:   # the timer is what expires sessions: it must never die
                print(f"timer failed: {e!r}", flush=True)

    def _tick(self):
        with self.lock:
            s = self.session
            if not s:
                return
            if s.expired(time.monotonic()):
                print("session expired", flush=True)
                self._end()
                return
            if not self.live:
                return
            self.ticks += 1
            err = self.capture.poll_error()
            if err:
                msg, retry = err
                print(f"capture error: {msg}", flush=True)
                if retry:   # the GPU encoder failed: same session, now on x264
                    self._restart_capture()
                else:
                    self._end()
                return
            poll = self.ticks % 2 == 0
            if self.ticks % 5 == 0 and s.peer:
                threading.Thread(target=self._lookup_path, args=(s.peer[0],), daemon=True).start()
        if poll:
            self._poll_monitors()

def _any_peer_check(session):
    real = session.check
    def check(t, f, frm, now):
        if session.peer is None and t == T_HELLO and hmac.compare_digest(f[0], session.key):
            session.peer, session.last_seen = frm, now
            return "new_peer"
        return real(t, f, frm, now)
    return check


# ---- runtime: X11 ----

def x11_monitors(d):
    root = d.screen().root
    return [(d.get_atom_name(m.name), m.x, m.y, m.width_in_pixels, m.height_in_pixels)
            for m in root.xrandr_get_monitors().monitors]

def x11_monitors_fresh():
    """Own connection per call: the timer and the receiver both ask, and python-xlib is not thread-safe."""
    from Xlib import display
    d = display.Display()
    try:
        return x11_monitors(d)
    finally:
        d.close()

def x11_ok():
    try:
        from Xlib import display
        display.Display().close()
        return True
    except Exception:
        return False

def tailnet_ipv4():
    try:
        out = subprocess.run(["tailscale", "ip", "-4"], capture_output=True, text=True, timeout=5).stdout.split()
        return out[0] if out else None
    except Exception:
        return None


# ---- runtime: GStreamer capture ----

def vaapi_qp(kbps):
    """This Intel driver only does constant-QP (CBR/VBR need HuC firmware, off on the Acer), so the bitrate
    becomes a quantizer: about +6 QP per halving, 26 at 4000 kbps."""
    import math
    return int(_clamp(round(26 + 6 * math.log2(4000 / kbps)), 20, 40))

def _from_factory(obj, name):
    """True when a bus message source, or something it sits inside, was made by GStreamer factory `name`."""
    while obj is not None:
        f = obj.get_factory() if hasattr(obj, "get_factory") else None   # pads have no factory
        if f is not None and f.get_name() == name:
            return True
        obj = obj.get_parent()
    return False

class Capture:
    """ximagesrc -> H.264 (VAAPI on the Intel GPU, else x264) -> Annex-B access units to on_frame.
    The phone's decoder needs 4:2:0 and no B-frames; both encoders are pinned to that."""

    def __init__(self, on_frame):
        self.on_frame, self.p, self.size = on_frame, None, (0, 0)
        import gi
        gi.require_version("Gst", "1.0")
        gi.require_version("GstVideo", "1.0")
        from gi.repository import Gst, GstVideo
        Gst.init(None)
        self.Gst, self.GstVideo = Gst, GstVideo
        self.vaapi = bool(Gst.ElementFactory.find("vaapih264enc")) and not os.environ.get("HERDR_SCREEN_X264")
        self.running_vaapi = False

    def encoder(self, kbps):
        if self.vaapi:
            # rate-control=cbr and vbr both fail "not-negotiated" on the Acer's HD 620 (iHD driver): cqp only.
            return (f"vaapih264enc rate-control=cqp init-qp={vaapi_qp(kbps)} keyframe-period={VAAPI_KEYFRAME_PERIOD} "
                    f"max-bframes=0 ! video/x-h264,profile=main")
        # Without format=I420 x264 picks 4:4:4 from the RGB source (High 4:4:4, which the phone cannot decode).
        return (f"video/x-raw,format=I420 ! x264enc tune=zerolatency speed-preset=ultrafast bitrate={kbps} "
                f"key-int-max=100000 bframes=0 sliced-threads=true ! video/x-h264,profile=main")

    def start(self, mon, kbps, fps):
        self.stop()
        _, x, y, w, h = mon
        # appsink drop=false: a dropped encoded frame would break decoding without a frame-id gap to tell the phone.
        desc = (f"ximagesrc startx={x} starty={y} endx={x + w - 1} endy={y + h - 1} use-damage=false "
                f"show-pointer=false ! video/x-raw,framerate={fps}/1 ! videoconvert ! {self.encoder(kbps)} "
                f"! h264parse config-interval=-1 ! video/x-h264,stream-format=byte-stream,alignment=au "
                f"! appsink name=sink emit-signals=true sync=false max-buffers=2 drop=false")
        p = self.Gst.parse_launch(desc)
        p.get_by_name("sink").connect("new-sample", self._sample)
        self.p, self.size, self.running_vaapi = p, (w, h), self.vaapi
        if p.set_state(self.Gst.State.PLAYING) == self.Gst.StateChangeReturn.FAILURE:
            err, retry = self.poll_error() or ("pipeline would not start", False)
            self.stop()
            if retry:   # VAAPI is now off, so this recurses at most once
                return self.start(mon, kbps, fps)
            raise RuntimeError(err)

    def _sample(self, sink):
        s = sink.emit("pull-sample")
        buf = s.get_buffer()
        ok, info = buf.map(self.Gst.MapFlags.READ)
        if ok:
            data = bytes(info.data)
            buf.unmap(info)
            try:
                self.on_frame(data, _micros())
            except Exception as e:
                print(f"frame failed: {e!r}", flush=True)
        return self.Gst.FlowReturn.OK

    def request_keyframe(self):
        if self.p:
            ev = self.GstVideo.video_event_new_upstream_force_key_unit(self.Gst.CLOCK_TIME_NONE, True, 0)
            self.p.get_by_name("sink").send_event(ev)

    def poll_error(self):
        """None, or (message, retry). retry: the VAAPI encoder itself failed, so restart now on x264."""
        if not self.p:
            return None
        m = self.p.get_bus().pop_filtered(self.Gst.MessageType.ERROR)
        if not m:
            return None
        err = m.parse_error()[0].message
        if self.running_vaapi and self.vaapi and _from_factory(m.src, "vaapih264enc"):
            self.vaapi = False
            print(f"VAAPI encoder failed ({err}); switching to x264", flush=True)
            return err, True
        return err, False

    def stop(self):
        if self.p:
            self.p.set_state(self.Gst.State.NULL)
            self.p = None
        self.size = (0, 0)


# ---- runtime: input and cursor ----

class Input:
    """Fakes pointer and keys with XTest. Own X connection: python-xlib is not thread-safe.
    If that connection dies (logout, X crash) the helper exits and systemd starts a fresh one."""

    def __init__(self, x=None):
        if x is None:   # tests pass (X, xtest, display, closed error) fakes
            from Xlib import X, display, error
            from Xlib.ext import xtest
            x = X, xtest, display.Display(), error.ConnectionClosedError
        self.X, self.xtest, self.d, self.closed = x
        self.mon, self.scroll, self.buttons, self.spare, self.spare_ks = None, Scroller(), set(), None, None

    def handle(self, t, f):
        try:
            self._drain()
            if t == T_MOVE:
                self._move(*f)
            elif t == T_SCROLL:
                for b in self.scroll.clicks(*f):
                    self._fake(self.X.ButtonPress, b)
                    self._fake(self.X.ButtonRelease, b)
            elif t == T_BUTTON:
                _, btn, down, _clicks, mods = f   # X counts double-clicks itself from the timing
                if btn <= 1:
                    xb = 1 if btn == 0 else 3
                    if down:
                        self._mods(mods, True)
                        self._fake(self.X.ButtonPress, xb)
                        self.buttons.add(xb)
                    else:
                        self._fake(self.X.ButtonRelease, xb)
                        self.buttons.discard(xb)
                        self._mods(mods, False)
            elif t == T_KEY:
                # The phone only ever sends a press as a down/up pair (ScreenClient.press), never a hold. So the
                # down is the whole tap and the up is ignored: a lost or late up can never leave X auto-repeating.
                _, kind, value, down, mods = f
                ks = NAMED_KEYSYMS.get(value) if kind == 0 else char_keysym(value)
                if ks is not None and down:
                    self._mods(mods, True)
                    self._tap(ks)
                    self._mods(mods, False)
            elif t == T_TEXT:
                for ch in f[1]:
                    self._tap(char_keysym(ord(ch)))
            self.d.sync()
        except Exception as e:   # a bad key must not kill the session
            self._fail("input", e)

    def _fail(self, what, e):
        if isinstance(e, self.closed):
            print(f"X connection lost ({e!r}); exiting so systemd restarts the helper", flush=True)
            os._exit(1)
        print(f"{what} failed: {e!r}", flush=True)

    def _drain(self):
        """Keymap changes (ours, setxkbmap, a new keyboard) arrive as MappingNotify; without this python-xlib's
        keymap cache goes stale and events pile up unread."""
        for _ in range(self.d.pending_events()):
            e = self.d.next_event()
            if e.type == self.X.MappingNotify:
                self.d.refresh_keyboard_mapping(e)
                if self.spare is not None and self.d.keycode_to_keysym(self.spare, 0) != self.spare_ks:
                    self.spare_ks = None   # someone else changed the spare key

    def _move(self, fx, fy):
        if self.mon:
            x, y = to_root(self.mon, fx, fy)
            self.xtest.fake_input(self.d, self.X.MotionNotify, x=x, y=y)   # detail 0 = absolute

    def _fake(self, kind, detail):
        if detail:   # keycode 0 = keysym not on this keyboard
            self.xtest.fake_input(self.d, kind, detail)

    def _mods(self, mods, down):
        for ks in (mod_keysyms(mods) if down else reversed(mod_keysyms(mods))):
            self._fake(self.X.KeyPress if down else self.X.KeyRelease, self.d.keysym_to_keycode(ks))

    def _tap(self, ks):
        """Press and release the key that makes `ks`; Shift if it is the shifted symbol; a spare key otherwise."""
        code, shift = self._code(ks)
        if not code:
            return
        shift_code = self.d.keysym_to_keycode(0xFFE1)
        if shift:
            self._fake(self.X.KeyPress, shift_code)
        self._fake(self.X.KeyPress, code)
        self._fake(self.X.KeyRelease, code)
        if shift:
            self._fake(self.X.KeyRelease, shift_code)

    def _code(self, ks):
        # The cache may list the spare key under a keysym it no longer makes, so the spare is never taken from it.
        for code, index in self.d.keysym_to_keycodes(ks):
            if index in (0, 1) and code != self.spare:
                return code, index == 1
        if self.spare is None:   # a keycode with nothing on it, borrowed for characters with no key (emoji, accents)
            lo = self.d.display.info.min_keycode
            hi = self.d.display.info.max_keycode
            for i, syms in enumerate(self.d.get_keyboard_mapping(lo, hi - lo + 1)):
                if not any(syms):
                    self.spare = lo + i
            if self.spare is None:
                return None, False
        if ks != self.spare_ks:
            self.d.change_keyboard_mapping(self.spare, [(ks, ks)])
            self.d.sync()
            self.spare_ks = ks
            time.sleep(0.01)   # let clients see the MappingNotify before the key arrives
        return self.spare, False

    def release_all(self):
        """A lost "up" must not leave the desktop dragging or holding a modifier."""
        try:
            for b in list(self.buttons):
                self._fake(self.X.ButtonRelease, b)
            self.buttons.clear()
            for ks in (0xFFE1, 0xFFE3, 0xFFE9):
                self._fake(self.X.KeyRelease, self.d.keysym_to_keycode(ks))
            self.d.sync()
        except Exception as e:
            self._fail("release", e)

    def reset(self):
        """New session: nothing held over from the last phone."""
        self.release_all()
        self.scroll = Scroller()


class Cursor:
    """Sends the pointer position (on change, at least every 0.5 s) and its shape (on change, and every 2 s)."""

    def __init__(self):
        self.thread, self.stop_flag = None, threading.Event()

    def start(self, host):
        self.stop()
        self.stop_flag = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(host, self.stop_flag), daemon=True, name="herdr.cursor")
        self.thread.start()

    def stop(self):
        self.stop_flag.set()

    def _run(self, host, stop):
        try:
            from Xlib import display
            d = display.Display()
        except Exception as e:
            print(f"cursor off: {e!r}", flush=True)
            return
        root = d.screen().root
        serial, last, last_sent, shape_sent, shape_err = 0, None, 0.0, 0.0, False
        try:
            d.xfixes_query_version()
        except Exception as e:
            print(f"cursor shapes off: {e!r}", flush=True)
            shape_err = True
        while not stop.is_set():
            try:
                now = time.monotonic()
                if not shape_err and now - shape_sent > 0.1:
                    try:   # shapes are optional: positions keep flowing if this fails
                        ci = d.xfixes_get_cursor_image(root)
                        if ci.cursor_serial != serial or now - shape_sent > 2:
                            png = png_rgba(ci.width, ci.height, argb_to_rgba(ci.cursor_image))
                            if len(png) <= MAX_CURSOR_PNG:
                                host.transport.send(msg_cursor_shape(ci.cursor_serial, ci.xhot, ci.yhot,
                                                                     ci.width, ci.height, png))
                            serial, shape_sent = ci.cursor_serial, now
                    except Exception as e:
                        print(f"cursor shapes off: {e!r}", flush=True)
                        shape_err = True
                mon = host.input.mon
                if mon:
                    p = root.query_pointer()
                    pos = to_unit(mon, p.root_x, p.root_y)
                    if pos != last or now - last_sent > 0.5:
                        host.transport.send(msg_cursor(pos[0], pos[1], pos[2], serial))
                        last, last_sent = pos, now
                time.sleep(1 / 60)
            except Exception as e:   # never die silently; slow down and keep trying
                print(f"cursor failed: {e!r}", flush=True)
                time.sleep(1)
        d.close()


# ---- main ----

def main():
    port, sock = 47990, os.path.expanduser("~/.herdr-remote/screen.sock")
    args = sys.argv[1:]
    while args:
        a = args.pop(0)
        if a == "--port":
            port = int(args.pop(0))
        elif a == "--socket":
            sock = args.pop(0)
        else:
            print(f"unknown argument {a}")
            sys.exit(2)
    ok = x11_ok()
    print(f"Herdr Screen (Linux): control {sock}, UDP port {port}, X11 {'ok' if ok else 'MISSING'}", flush=True)
    if not ok:   # answer "unsupported" (Wayland, or no desktop yet); restart with input once X11 is there
        def wait_for_x11():
            while not x11_ok():
                time.sleep(5)
            os._exit(1)   # systemd starts us again
        threading.Thread(target=wait_for_x11, daemon=True).start()
        serve_control(sock, Host(port, None, None, None, tailnet_ipv4, lambda: [], lambda: False).request)
        return
    host = None

    def on_frame(au, us):
        if host and not host.transport.send_frame(au, us):
            host.capture.request_keyframe()

    try:
        capture, input_ = Capture(on_frame), Input()
    except Exception as e:   # missing python3-gi / GStreamer / python-xlib / XTEST: say why instead of crash-looping
        print(f"cannot start capture or input: {e!r}", flush=True)
        serve_control(sock, Host(port, None, None, None, tailnet_ipv4, lambda: [], x11_ok,
                                 unsupported=f"Screen sharing could not start on this computer: {e}").request)
        return
    host = Host(port=port, capture=capture, input_=input_, cursor=Cursor(),
                tailnet_ip=tailnet_ipv4, monitors=x11_monitors_fresh, x11_ok=x11_ok)
    serve_control(sock, host.request)

if __name__ == "__main__":
    main()
