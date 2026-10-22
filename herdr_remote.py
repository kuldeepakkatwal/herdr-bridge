#!/usr/bin/env python3
"""Herdr Remote bridge: turns herdr's unix socket into a small HTTP + SSE API for the iPhone app.

Stdlib only. Binds 127.0.0.1; reach it through `tailscale serve`, which adds the
Tailscale-User-Login header this bridge checks.
"""
import argparse
import base64
import datetime
import errno
import fcntl
import glob
import json
import os
import platform
import pty
import queue
import re
import secrets
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
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

HOME = os.path.expanduser("~")
SOCK = os.path.join(HOME, ".config/herdr/herdr.sock")
UPLOADS = os.path.join(HOME, ".herdr-remote", "uploads")   # pictures sent from the phone
SCREEN_SOCK = os.path.join(HOME, ".herdr-remote", "screen.sock")   # Herdr Screen helper (Mac only)


class HerdrError(Exception):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def call(method, params=None, timeout=5, sock=None):
    """One request per connection: herdr speaks newline-delimited JSON."""
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(timeout)
    try:
        s.connect(sock or SOCK)
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


def screen(method, params=None):
    """Ask the Herdr Screen helper (mac-screen/). Missing helper -> {"status": "not_installed"}."""
    if sys.platform != "darwin":   # the helper is macOS-only for now (Kuldeepak, 2026-10-17)
        return {"status": "unsupported", "message": "Remote desktop on Linux is coming soon. Chats work as usual."}
    if not os.path.exists(SCREEN_SOCK):
        return {"status": "not_installed"}
    try:
        return call(method, params, timeout=5, sock=SCREEN_SOCK)
    except (ConnectionRefusedError, FileNotFoundError):
        return {"status": "not_running"}


ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")
MENU_LINE = re.compile(r"^(?:[❯›>↓↑]\s*)?(\d{1,2})[.)]\s+(.+?)$")   # ↓ ↑: a scrolled list's edge row


def tilde(path):
    return "~" + path[len(HOME):] if path and path.startswith(HOME) else path


CLAUDE_PROJECTS = os.path.join(HOME, ".claude", "projects")
CODEX_SESSIONS = os.path.join(HOME, ".codex", "sessions")   # YYYY/MM/DD/rollout-<time>-<session id>.jsonl
CODEX_INDEX = os.path.join(HOME, ".codex", "session_index.jsonl")   # {id, thread_name}: Codex's chat titles
MUSE_SESSIONS = os.path.join(HOME, ".local", "share", "muse", "sessions")   # YYYY/MM/DD/<session id>/session.jsonl
CODEX_FILE = re.compile(r"^rollout-.*-([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl$")
_tails = {}   # (projects dir, session id) -> Tail
_tails_lock = threading.Lock()


def session_file(session_id):
    if not session_id or "/" in session_id:
        return None
    hits = glob.glob(os.path.join(CLAUDE_PROJECTS, "*", session_id + ".jsonl"))
    if not hits:   # a Codex tab: herdr reports its thread id, the rollout file ends with it
        hits = glob.glob(os.path.join(CODEX_SESSIONS, "*", "*", "*", "rollout-*-%s.jsonl" % glob.escape(session_id)))
        if not hits:   # a Muse tab: the bridge found its session id itself (muse_panes)
            hits = glob.glob(os.path.join(MUSE_SESSIONS, "*", "*", "*", glob.escape(session_id), "session.jsonl"))
        return hits[0] if hits else None
    # Claude Code can move a conversation to a new session file (it copies it over) and ends the old
    # file with a continued-in record. herdr keeps reporting the old id, so follow the record.
    with open(hits[0], "rb") as f:
        f.seek(max(0, os.path.getsize(hits[0]) - 1024))
        last = f.read().rstrip(b"\n").rsplit(b"\n", 1)[-1]
    if b'"continued-in"' in last:
        try:
            nxt = json.loads(last).get("continuedInSessionId")
        except ValueError:
            nxt = None
        if nxt and nxt != session_id:
            return session_file(nxt) or hits[0]
    return hits[0]


def _tool_line(block):
    i = block.get("input") or {}
    arg = i.get("command") or i.get("file_path") or i.get("pattern") or i.get("description") or ""
    if i.get("file_path"):
        arg = os.path.basename(arg)
    return _clip(block.get("name", "tool"), arg)


def _clip(name, arg):
    """One short tool line: "<name> <arg>", the arg on one line and cut at 80 characters."""
    arg = " ".join(str(arg).split())
    return "%s %s" % (name, arg[:80] + ("…" if len(arg) > 80 else ""))


def _codex_tool(it):
    """A Codex item_completed tool item -> a tool line like Claude's, or None (reasoning and the rest)."""
    t = it.get("type")
    if t == "CommandExecution":
        c = it.get("command") or []
        if isinstance(c, str):
            return _clip("Bash", c)
        # argv like ["/bin/zsh", "-lc", "<cmd>"]: the shell wrapper is noise, show the command itself
        return _clip("Bash", c[-1] if len(c) == 3 and c[1] in ("-c", "-lc") else " ".join(map(str, c)))
    if t == "FileChange":
        return _clip("Edit", ", ".join(os.path.basename(f) for f in it.get("changes") or {}))
    if t == "McpToolCall":
        title = (it.get("arguments") or {}).get("title") if isinstance(it.get("arguments"), dict) else None
        return _clip("%s.%s" % (it.get("server"), it.get("tool")), title or "")
    if t == "ImageView":
        p = it.get("path") or ""
        return _clip("View", p[len("file://"):] if p.startswith("file://") else p)
    return None


def codex_title(session_id):
    """Codex's name for the chat from its session index (tiny); the last line for an id wins."""
    title = None
    try:
        with open(CODEX_INDEX, encoding="utf-8") as f:
            for line in f:
                if session_id not in line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                if d.get("id") == session_id:
                    title = d.get("thread_name") or title
    except OSError:
        pass
    return title


def _muse_tool(call):
    """A Muse tool call {name, args (a JSON string)} -> a tool line like Claude's."""
    raw = call.get("args") or ""
    try:
        a = json.loads(raw)
    except (TypeError, ValueError):
        a = None
    if not isinstance(a, dict):
        return _clip(call.get("name", "tool"), raw)
    # else the first text argument (read_skill {"name": ...}), else the raw JSON
    arg = (a.get("command") or a.get("path") or a.get("pattern") or a.get("query")
           or next((v for v in a.values() if isinstance(v, str) and v), raw))
    if arg == a.get("path"):
        arg = os.path.basename(str(arg))
    return _clip(call.get("name", "tool"), arg)


def _muse_ts(us):
    """Muse's recorded_at (microseconds since 1970) -> the ISO time Claude's entries carry."""
    try:
        return datetime.datetime.fromtimestamp(us / 1e6, datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _muse_status(tail):
    """A Muse chat's status in herdr's words, from the turn and approval records it has read."""
    return "blocked" if tail.waiting else "working" if tail.running else "idle"


CMD_NAME = re.compile(r"<command-name>(.*?)</command-name>")
CMD_ARGS = re.compile(r"<command-args>(.*?)</command-args>")


PASTED = re.compile(r"\s*<pasted_content[^>]*>\n?(.*?)\n?</pasted_content[^>]*>\s*", re.S)


def _unpaste(text):
    """Claude Code saves a long message (a paste, or herdr's prompt) inside <pasted_content> tags: give
    back the text. Without this it started with "<" like a notification and vanished from the chat."""
    return PASTED.sub(lambda m: "\n\n" + m.group(1) + "\n\n", text).strip()


def _typed(text):
    """A slash command is saved as tags: give it back the way it was typed."""
    m = CMD_NAME.match(text)
    if not m:
        return text
    args = CMD_ARGS.search(text)
    return m.group(1) + (" " + args.group(1).strip() if args and args.group(1).strip() else "")


class Tail:
    """Follows one Claude Code transcript (jsonl, appended to as the conversation goes).

    read() parses only what was appended since the last call and returns it as events
    {"item": {role, text, ts[, state]}, "index": position in self.items, "replace_last": bool}.
    replace_last: the event replaces the last item (a tool line merged into it, or its state changed).
    An event with a lower index and replace_last False changed an earlier item (a queued message taken).
    Keeps your prompts (also the ones typed while Claude was busy), Claude's replies and short tool
    lines; skips thinking, tool results, meta/sidechain records and local command output.
    """

    def __init__(self, path=None, session=None, sidechain=False):
        self.path, self.session = path, session
        self.sidechain = sidechain   # True: a helper agent's own transcript, where every record is a sidechain one
        self.offset, self.rest = 0, b""
        self.items, self.title, self.custom = [], None, None
        self.expect = []   # texts of dequeued messages: Claude Code writes each once more as a user record
        self.queue = []    # Claude Code's queue as (text, item index or None for a hidden notification), oldest first
        self.moved = None  # session id from a continued-in record
        self.running, self.waiting = False, set()   # Muse: a turn is going on; approvals asked and not yet answered

    def _event(self, i):
        return {"item": dict(self.items[i]), "index": i, "replace_last": i == len(self.items) - 1}

    def _add(self, role, text, ts, state=None):
        if role == "tool" and self.items and self.items[-1]["role"] == "tool":
            self.items[-1]["text"] += "\n" + text
            return self._event(len(self.items) - 1)
        item = {"role": role, "text": text, "ts": ts}
        if state:
            item["state"] = state
        self.items.append(item)
        return dict(self._event(len(self.items) - 1), replace_last=False)

    def _mark(self, i, state):
        if i is None:
            return []
        self.items[i]["state"] = state
        return [self._event(i)]

    def _queue(self, d):
        # A message typed while Claude is busy is only an enqueue. Taken mid-turn (remove,
        # absorbed_mid_turn) it never becomes a user record; taken at turn start (dequeue) it does.
        # Hidden notifications stay in self.queue too, so a dequeue pops the same entry Claude Code did.
        raw = (d.get("content") or "").strip()
        op, text = d.get("operation"), _typed(_unpaste(raw))
        if op == "enqueue" and text:
            hidden = text.startswith("<") and "<pasted_content" not in raw   # something pasted is never a notification
            out = [] if hidden else [self._add("user", text, d.get("timestamp"), "queued")]
            self.queue.append((text, None if hidden else len(self.items) - 1))
            return out
        if op == "remove":
            hit = [k for k, (t, _) in enumerate(self.queue) if t == text]
            if not hit:
                return []
            taken = d.get("reason") in ("absorbed_mid_turn", "delivered_to_agent")
            return self._mark(self.queue.pop(hit[-1])[1], "sent" if taken else "dropped")   # dropped: cleared, never sent
        if op == "dequeue" and self.queue:
            text, i = self.queue.pop(0)
            if i is not None:
                self.expect.append(text)
            return self._mark(i, "sent")
        return []

    def _record(self, line):
        try:
            d = json.loads(line)
        except ValueError:
            return []
        if "payload_type" in d:
            return self._muse(d)   # Muse; Claude and Codex records never have payload_type
        t = d.get("type")
        p = d.get("payload")
        if t == "event_msg" and isinstance(p, dict) and p.get("type") == "item_completed":
            return self._codex(p.get("item") or {}, d.get("timestamp"))   # Codex; Claude records never look like this
        if t != "continued-in" and d.get("timestamp"):
            # Written after a continued-in: the old session was resumed (claude --resume), so this file is live
            # again and the pane shows it; following the move would show another conversation (jesse, 2026-10-19).
            self.moved = None
        if t == "custom-title":   # /rename: the user's name sticks, Claude's later automatic titles don't replace it
            self.custom = d.get("customTitle") or self.custom
            self.title = self.custom or self.title
            return []
        if t == "ai-title":
            self.title = self.custom or d.get("aiTitle") or self.title
            return []
        if t == "queue-operation":
            return self._queue(d)
        if t == "continued-in":
            self.moved = d.get("continuedInSessionId")
            return []
        if t not in ("user", "assistant") or d.get("isMeta") or (d.get("isSidechain") and not self.sidechain):
            return []
        content = (d.get("message") or {}).get("content")
        ts = d.get("timestamp")
        out = []
        if t == "user" and d.get("isCompactSummary"):
            # /compact (or auto-compact) saves its summary as a user message: one short line, not a giant bubble.
            return [self._add("note", "Conversation compacted", ts)]
        if t == "user":
            if isinstance(content, list):
                content = "\n".join(b.get("text", "") for b in content if b.get("type") == "text")
            raw = content.strip() if isinstance(content, str) else ""
            text = _typed(_unpaste(raw))
            note = text.startswith("<") and "<pasted_content" not in raw   # a notification; something pasted never is
            if text in self.expect:   # the dequeued message written again: it is already in the chat
                self.expect.remove(text)
                return []
            if text and not note:   # notifications arrive in between; only a real prompt ends the wait
                self.expect = []
            if text.startswith("[Request interrupted"):
                out.append(self._add("note", text, ts))
            elif text and not note:
                out.append(self._add("user", text, ts))
        else:
            for b in content or []:
                if b.get("type") == "text" and b.get("text", "").strip():
                    out.append(self._add("assistant", b["text"].strip(), ts))
                elif b.get("type") == "tool_use":
                    out.append(self._add("tool", _tool_line(b), ts))
        return out

    def _codex(self, it, ts):
        # Only Codex's item_completed records: its response_item user messages also carry the hidden
        # <environment_context> and AGENTS.md setup text.
        t = it.get("type")
        if t in ("UserMessage", "AgentMessage"):
            kind = "text" if t == "UserMessage" else "Text"
            text = "\n".join(b.get("text", "") for b in it.get("content") or [] if b.get("type") == kind).strip()
            return [self._add("user" if t == "UserMessage" else "assistant", text, ts)] if text else []
        line = _codex_tool(it)
        return [self._add("tool", line, ts)] if line else []

    def _muse(self, d):
        pt, p = d.get("payload_type"), d.get("payload")
        if not isinstance(p, dict):
            return []
        ts = _muse_ts(d.get("recorded_at"))
        if pt == "session.name.changed":
            self.title = p.get("new_name") or self.title
            return []
        if pt == "runtime.user_intent.accepted":   # the full text; the run's user_prompt_display shortens pastes
            text = "\n".join(b.get("text", "") for m in p.get("model_messages") or []
                             for b in m.get("content") or [] if b.get("kind") == "text").strip()
            return [self._add("user", text, ts)] if text else []
        if pt != "runtime.session":
            return []
        e = p.get("event") or {}
        k = e.get("kind")
        if p.get("kind") == "approval":
            if k == "requested":
                self.waiting.add(e.get("pending_action_id"))
            elif k == "decision_applied":
                self.waiting.discard(e.get("pending_action_id"))
            return []
        if p.get("kind") != "run":
            return []
        if k in ("started", "terminal"):
            self.running = k == "started"
            if k == "terminal":
                self.waiting.clear()   # an interrupted turn leaves its question unanswered
            return []
        if k == "assistant_message_committed" and (e.get("text") or "").strip():
            return [self._add("assistant", e["text"].strip(), ts)]
        if k == "assistant_tool_calls_committed":
            return [self._add("tool", _muse_tool(c), ts) for c in e.get("tool_calls") or []]
        return []

    def read(self):
        """Events appended since the last read. None: the file shrank or was replaced, start over
        (the caller sends the whole history again)."""
        if not self.path and self.session:
            self.path = session_file(self.session)   # a new session's file appears a moment after it starts
        if not self.path or not os.path.exists(self.path):
            return []
        size = os.stat(self.path).st_size
        if size < self.offset:
            self.__init__(self.path, self.session, self.sidechain)
            return None
        if size == self.offset:   # nothing new: skip the open
            return []
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
        m = CODEX_FILE.match(os.path.basename(self.path))
        if m:   # Codex keeps titles in its index, not the file; re-read on growth so a rename shows up
            self.title = codex_title(m.group(1)) or self.title
        if self.moved:   # the conversation went on in a new file that repeats it: read that one whole
            self.__init__(session=self.moved)
            self.read()
            return None
        return out


def parse_transcript(path):
    """Claude Code's saved session -> ([{role, text, ts[, state]}], title)."""
    t = Tail(path)
    t.read()
    return t.items, t.title


def transcript(session_id):
    """Claude's saved session -> (items, title). One Tail per session, so each call reads only what
    was appended since the last: a working agent's growing file is not re-parsed every refresh."""
    with _tails_lock:   # the refresh loop and HTTP threads share these Tails
        t = _tail(session_id)
        return (list(t.items), t.title) if t else ([], None)   # a copy: the refresh loop keeps appending to t.items


def session_title(session_id):
    """Only the title: the refresh asks for it per agent every second, so no copy of the items."""
    with _tails_lock:
        t = _tail(session_id)
        return t.title if t else None


def _tail(session_id):
    """The session's Tail, read up to now. Caller holds _tails_lock."""
    key = (CLAUDE_PROJECTS, session_id)
    t = _tails.get(key)
    if not t:
        path = session_file(session_id)
        if not path:
            return None
        t = _tails[key] = Tail(path)
    if t.read() is None:   # the file shrank or was replaced: Tail restarted, read it from the top
        t.read()
    return t


def prune_tails(keep):
    """Forget Tails of sessions not in `keep` (closed tabs, /clear), so _tails doesn't grow forever."""
    with _tails_lock:
        for k in [k for k in _tails if k[1] not in keep]:
            del _tails[k]


# Parsed Tails of watched chats, kept after a stream stops watching: building one re-reads the whole
# file (213 ms for an 86 MB transcript) and the phone opens a chat again and again.
# ponytail: count cap, not memory; items are ~0.2-0.5 MB each. Cap by size if huge chats pile up.
TAILS, TAILS_LOCK, TAILS_MAX = {}, threading.Lock(), 30


def take_tail(sid):
    """A Tail of this session for one stream alone, read up to now. Popped from the cache, so two
    streams never share one (a second stream on the same session builds its own)."""
    with TAILS_LOCK:
        t = TAILS.pop((CLAUDE_PROJECTS, sid), None)
    if t is not None and t.path != session_file(sid):   # moved on, or the old session was resumed: start fresh
        t = None
    if t is None:
        t = Tail(session=sid)
    if t.read() is None:   # the file shrank or was replaced: Tail restarted, read it from the top
        t.read()
    t.key = sid   # what it was taken for: after a continued-in move t.session is the new id
    return t


def give_tail(tail):
    """Back into the cache when a stream stops watching it; the oldest goes past TAILS_MAX."""
    if tail is None or getattr(tail, "key", None) is None:
        return
    with TAILS_LOCK:
        TAILS[(CLAUDE_PROJECTS, tail.key)] = tail
        while len(TAILS) > TAILS_MAX:
            del TAILS[next(iter(TAILS))]


PANE_ENV = re.compile(r"\sHERDR_PANE_ID=(\S+)")
MUSE_EVERY = 5   # seconds between looks for Muse processes (a ps and a glob): the refresh runs every second
_muse = {"at": 0, "panes": {}}   # pane id -> Muse session id, as last found (refresh thread only)
_muse_pid_of = {}   # Muse session file -> pid from its route_facts record (never changes)


def muse_pids(ps_out):
    """`ps axeww -o pid=,command=` output (command then environment) -> {Muse pid: herdr pane id}.
    herdr puts HERDR_PANE_ID in the environment of everything started in a pane."""
    out = {}
    for line in ps_out.splitlines():
        parts = line.split(None, 2)
        m = PANE_ENV.search(line)
        if len(parts) > 1 and parts[0].isdigit() and os.path.basename(parts[1]).startswith("muse-bin-") and m:
            out[int(parts[0])] = m.group(1)
    return out


def _muse_file_pid(path):
    """The Muse pid in a session file's route_facts record (near the top), cached; None if not there (yet)."""
    if path in _muse_pid_of:
        return _muse_pid_of[path]
    pid, lines = None, 0
    try:
        with open(path, "rb") as f:
            for line in f:
                lines += 1
                if b'"runtime.session.route_facts"' in line:
                    try:
                        pid = ((json.loads(line).get("payload") or {}).get("record") or {}).get("pid")
                    except (ValueError, AttributeError):
                        pass
                    break
                if lines == 30:
                    break
    except OSError:
        return None
    if pid is not None or lines == 30:   # a new file may not have it yet: look again next time
        _muse_pid_of[path] = pid
    return pid


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def muse_sessions(pids):
    """{pid: session file} for these Muse pids: the newest file whose route_facts names the pid
    (a Muse that starts a new session keeps its pid). Only the top-level files, not helpers' ones."""
    found = {}
    files = glob.glob(os.path.join(MUSE_SESSIONS, "*", "*", "*", "*", "session.jsonl"))
    for path in sorted(files, key=_mtime, reverse=True):
        if len(found) == len(pids):
            break
        pid = _muse_file_pid(path)
        if pid in pids and pid not in found:
            found[pid] = path
    return found


def muse_panes(now=None):
    """{pane id: Muse session id} for panes running Muse, which herdr does not know as an agent.
    Looked up at most every MUSE_EVERY seconds; the answer in between comes from the last look."""
    now = time.time() if now is None else now
    if now - _muse["at"] >= MUSE_EVERY:
        _muse["at"] = now
        try:
            out = subprocess.run(["ps", "axeww", "-o", "pid=,command="], capture_output=True, timeout=3).stdout
        except (OSError, subprocess.SubprocessError):
            out = b""
        pids = muse_pids(out.decode("utf-8", "replace"))
        files = muse_sessions(set(pids)) if pids else {}
        _muse["panes"] = {pane: os.path.basename(os.path.dirname(files[pid])) for pid, pane in pids.items() if pid in files}
    return _muse["panes"]


def muse_status(session_id):
    with _tails_lock:
        t = _tail(session_id)
        return _muse_status(t) if t else "unknown"


def build_state(workspaces, tabs, agents, muse=None):
    """`agents` is herdr's pane list: every pane, with `agent` None for a plain shell.
    `muse`: {pane id: Muse session id} (muse_panes); such a pane is shown as a "muse" agent."""
    muse = muse or {}
    # herdr 0.8.2 knows some Muse versions (agent "muse", its own status) but never their session: add it.
    # A Muse herdr does not know is a plain pane: the status comes from its session file too.
    agents = [a if a.get("pane_id") not in muse or a.get("agent") not in (None, "muse") or a.get("agent_session")
              else dict(a, agent="muse", agent_session={"value": muse[a["pane_id"]]},
                        agent_status=a.get("agent_status") if a.get("agent") else muse_status(muse[a["pane_id"]]))
              for a in agents]
    tab_label = {t["tab_id"]: t.get("label") for t in tabs}
    out = []
    for w in workspaces:
        ags = [a for a in agents if a.get("workspace_id") == w["workspace_id"]]
        titles = {a["pane_id"]: _session_title(a) for a in ags}   # once per agent: each takes the global lock
        out.append({
            "id": w["workspace_id"],
            "label": w.get("label") or str(w.get("number", "")),
            "path": tilde(ags[0].get("cwd")) if ags else None,
            "agents": [{
                "pane_id": a["pane_id"],
                "tab_id": a.get("tab_id"),
                "tab_label": tab_label.get(a.get("tab_id")),
                "title": tab_label.get(a.get("tab_id")) or a.get("terminal_title_stripped") or titles[a["pane_id"]] or a.get("agent") or a["pane_id"],
                "session_title": titles[a["pane_id"]] if a.get("agent") in ("claude", "codex", "muse") else None,   # the agent's conversation title
                "agent": a.get("agent"),
                "status": a.get("agent_status", "unknown"),
                "session": (a.get("agent_session") or {}).get("value"),   # Claude Code session id: its transcript file
                "cwd": tilde(a.get("cwd")),
                "last_active": _last_active(a) if a.get("agent_status") != "working" else None,   # a working agent's file changes every second
                "menu": None,
            } for a in ags],
        })
    return out


def _session_title(agent):
    sid = (agent.get("agent_session") or {}).get("value")
    try:
        return session_title(sid) if sid else None
    except OSError:
        return None


HELPER_ID = re.compile(r"^a[0-9a-f]{6,40}$")
HELPER_META = re.compile(r"^agent-(a[0-9a-f]{6,40})\.meta\.json$")


def _epoch(ts):
    """Claude Code's ISO timestamp ("2026-10-15T20:27:29.886Z") -> epoch seconds, None if unreadable."""
    try:
        return int(datetime.datetime.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=datetime.timezone.utc).timestamp()) if ts else None
    except (TypeError, ValueError):
        return None


def helpers_dir(session_id):
    """Where Claude Code keeps a chat's helper agents: <proj>/<session>/subagents next to <session>.jsonl."""
    path = session_file(session_id)
    return os.path.join(path[:-len(".jsonl")], "subagents") if path else None


def helpers(session_id, now=None):
    """The chat's helper agents (subagents), working first, then the most recently active. Max 50."""
    d = helpers_dir(session_id)
    try:
        names = os.listdir(d) if d else []
    except OSError:
        return []
    now = time.time() if now is None else now
    found = []
    for n in names:
        m = HELPER_META.match(n)
        try:
            st = m and os.stat(os.path.join(d, "agent-%s.jsonl" % m.group(1)))
        except OSError:
            continue
        if st:
            found.append((st.st_mtime, m.group(1), st))
    out = []
    for mtime, hid, st in sorted(found, reverse=True)[:50]:
        base = os.path.join(d, "agent-" + hid)
        try:
            with open(base + ".meta.json") as f:
                meta = json.load(f)
            with open(base + ".jsonl", "rb") as f:
                first = f.readline()
            ts = json.loads(first).get("timestamp") if first.strip() else None
        except (OSError, ValueError, AttributeError):
            continue
        if not isinstance(meta, dict):
            continue
        out.append({"id": hid, "description": meta.get("description"), "model": meta.get("model"),
                    "agent_type": meta.get("agentType"), "started": _epoch(ts) or int(st.st_ctime),
                    "last_active": int(mtime),
                    # ponytail: mtime within 30 s = working; a finished helper can read "working" up to 30 s.
                    "working": now - mtime < 30})
    out.sort(key=lambda h: (not h["working"], -h["last_active"]))
    return out


def _last_active(agent):
    """When the last real message (yours or Claude's) was written, epoch seconds. Not the file's mtime: Claude
    Code appends housekeeping records (mode, permission-mode, ...) to idle chats, which looked like activity."""
    sid = (agent.get("agent_session") or {}).get("value")
    with _tails_lock:
        t = _tails.get((CLAUDE_PROJECTS, sid))
        ts = next((i.get("ts") for i in reversed(t.items) if i.get("ts")), None) if t else None
    return _epoch(ts)


def parse_menu(text):
    """Find Claude Code's numbered choice list near the bottom of the screen.

    A line '1. x' starts a new list; only the next number extends it. Indented lines right under an
    option are its description ("detail"); other lines are skipped. The last list with 2+ options wins.
    """
    found, last = [], None   # last: the option the next indented lines describe
    for raw in text.splitlines()[-40:]:
        x = ANSI.sub("", raw).rstrip().rstrip("│").rstrip()
        if x.lstrip().startswith("│"):   # inside a box: indentation counts from the border
            x = x.lstrip()[1:]
        line = x.strip()
        m = MENU_LINE.match(line)
        if not m:
            if last and line and x[:1].isspace() and not RULE.match(line) and not line.startswith("…"):
                last["detail"] = (last.get("detail", "") + " " + line).strip()
            else:
                last = None
            continue
        n = int(m.group(1))
        if n == 1:
            found = []
        last = None
        if n == len(found) + 1:
            last = {"n": n, "label": re.sub(r" {3,}", "  ", m.group(2).strip())}   # columns padded for the Mac width
            found.append(last)
    return found if len(found) >= 2 else None


SGR = re.compile(r"\x1b\[([0-9;]*)m")
RULE = re.compile(r"^\s*[─━]{9,}\s*$")
MENU_PICK = re.compile(r"^\s*❯\s*\d+\.\s")
SPINNER = re.compile(r"^\s*[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏✻✽✶✳✢·*]\s+(\S.*)$")
BLOCK_START = re.compile(r"^(?:[⏺⎿✻※╭]|[●○](?!─)|>\s)")   # "●─ Run 5" is a tool group inside a reply, not a new block
SUGGESTION = re.compile(r"^(?:❯\s*)?/[A-Za-z0-9:._-]+(?:\s+\([^)]*\))?(\s{2,}|$)")   # ❯: the selected row
NORM_DROP = re.compile(r"[⏺●○│╭╰─❯>┃·]")


def _has_bg(raw):
    """True when an SGR code sets a background colour (40-47, 100-107, 48;5;n, 48;2;r;g;b).
    Walks the parameters in order so a colour value that happens to be 48 is not mistaken for one."""
    for m in SGR.finditer(raw):
        ps = [int(p) if p else 0 for p in m.group(1).split(";")]
        i = 0
        while i < len(ps):
            p = ps[i]
            if 40 <= p <= 47 or 100 <= p <= 107 or p == 48:
                return True
            if p in (38, 58):   # foreground / underline colour: skip its values
                i += 3 if i + 1 < len(ps) and ps[i + 1] == 5 else 5 if i + 1 < len(ps) and ps[i + 1] == 2 else 1
                continue
            i += 1
    return False


def _undimmed(raw):
    """The visible text without the parts drawn dim (SGR 2): Claude's empty-bar placeholder is dim."""
    out, dim = [], False
    for tok in re.split(r"(\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07)", raw):
        m = SGR.fullmatch(tok)
        if m:
            for p in (m.group(1) or "0").split(";"):
                if p in ("", "0", "22"):
                    dim = False
                elif p == "2":
                    dim = True
        elif not tok.startswith("\x1b") and not dim:
            out.append(tok)
    return "".join(out)


def _norm(text):
    return re.sub(r"[^a-z0-9]", "", NORM_DROP.sub("", text).lower())[:24]


def live_view(text, items, status):
    """The part of Claude's screen the transcript cannot have yet: the block being written, the spinner,
    a menu, the text in Claude's input bar. Everything above that is already in the transcript.
    footer: Claude's input-bar region as drawn (suggestions, status block, bar, mode lines), or the whole
    menu; clients that draw their own bar or buttons ignore it.
    -> {"lines": [raw ANSI lines], "working": str|None, "menu": [{n, label[, detail]}]|None, "input": str,
        "footer": [raw ANSI lines], "suggestions": [{command, detail?, selected}]}"""
    raw = [l.rstrip("\r") for l in text.split("\n")]
    plain = [ANSI.sub("", l).strip() for l in raw]
    n = len(raw)
    menu, question, inp, foot, suggestions = None, [], "", n, []
    pick = next((i for i in range(n - 1, -1, -1) if MENU_PICK.match(plain[i].strip("│").strip())), None)
    if pick is not None and not any(p.startswith("❯") and not MENU_PICK.match(p) for p in plain[pick + 1:]):
        first = next((i for i in range(pick, -1, -1) if re.match(r"^(?:[❯↓↑]\s*)?1\.\s", plain[i].strip("│").strip())), pick)
        rule = next((i for i in range(first - 1, max(first - 13, -1), -1) if RULE.match(plain[i])), None)
        foot = first if rule is None else rule
        if rule is None:   # no rule (the /model dialog): take the title block above, one blank line allowed
            blanks = 0
            for i in range(first - 1, max(first - 13, -1), -1):
                if re.match(r"^[▔─━]{9,}$", plain[i]):
                    break
                if not plain[i]:
                    blanks += 1
                    if blanks > 1:
                        break
                    continue
                foot = i
        menu = parse_menu("\n".join(raw[foot:]))
        # the question above the options; the options themselves become buttons from `menu`
        question = [raw[i] for i in range(foot, first) if plain[i] and not RULE.match(plain[i])]
    else:
        prompt = next((i for i in range(n - 1, -1, -1) if plain[i].startswith("❯")), None)
        if prompt is not None:
            inp = _undimmed(raw[prompt]).strip()[1:].strip()
            foot = prompt - 1 if prompt and RULE.match(plain[prompt - 1]) else prompt
            i, gap = foot - 1, 0   # Claude Code's status block sits on coloured backgrounds, maybe with blank rows
            while i >= 0 and gap <= 2:
                if _has_bg(raw[i]):
                    foot, gap = i, 0
                else:
                    gap += 1
                i -= 1
            end, i = foot, foot - 1   # the slash-command list Claude draws above it, with its wrapped descriptions
            while i >= 0 and (SUGGESTION.match(plain[i]) or (plain[i] and re.match(r"^ {8,}\S", ANSI.sub("", raw[i])))):
                if SUGGESTION.match(plain[i]):
                    foot = i
                i -= 1
            for i in range(foot, end):   # one alias in parentheses belongs to the command: "/usage (stats)"
                m = re.match(r"^(❯\s*)?(/[A-Za-z0-9:._-]+(?: \([^)]*\))?)(.*)$", plain[i])
                if m and SUGGESTION.match(plain[i]):
                    suggestions.append({"command": m.group(2), "detail": m.group(3), "selected": bool(m.group(1))})
                elif suggestions:
                    suggestions[-1]["detail"] += " " + plain[i]
            for sg in suggestions:
                d = " ".join(sg.pop("detail").split())
                if d:
                    sg["detail"] = d
    body = list(range(foot))
    working = None
    for i in reversed(body):
        m = SPINNER.match(plain[i])
        if m and re.search(r"…|esc to interrupt| tokens|\d+[sm]\b", m.group(1)):
            w = re.sub(r"\(?\s*esc to interrupt\s*\)?", "", m.group(1))
            working = " ".join(w.strip().strip("()").split()) or None
            body.remove(i)
            break
    if status in ("idle", "done"):
        working = None   # "✻ Crunched for 40s" is a finished turn, not a spinner
    while body and not plain[body[-1]]:
        body.pop()
    starts = [k for k, i in enumerate(body) if BLOCK_START.match(plain[i])]
    block = body[starts[-1]:] if starts else body
    if block:
        last = next((it for it in reversed(items[-3:]) if it.get("role") in ("user", "assistant")), None)
        key = _norm(" ".join(plain[i] for i in block))
        if status in ("idle", "done") or (last and key and key == _norm(last.get("text", ""))):
            block = []
    footer = raw[foot:]
    while footer and not ANSI.sub("", footer[-1]).strip():
        footer.pop()
    return {"lines": [raw[i] for i in block] + question, "working": working, "menu": menu, "input": inp,
            "footer": footer, "suggestions": suggestions}


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


STREAMS = {}   # stream id from hello -> that /api/events stream's queue (under hub.lock)


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
            ws = build_state(call("workspace.list")["workspaces"], call("tab.list")["tabs"], call("pane.list")["panes"], muse_panes())
            for w in ws:
                for a in w["agents"]:
                    if a["status"] == "blocked":
                        try:
                            a["menu"] = parse_menu(read_screen(a["pane_id"], source="visible", fmt="text"))
                        except HerdrError:
                            pass
            # Keep Tails of sessions still open, and of any a phone watches (from the old state, before publish).
            with self.lock:
                watched = {q.watch for q in self.clients if getattr(q, "watch", None)}
            prune_tails({a["session"] for w in ws for a in w["agents"]} | {session_of(self, p) for p in watched})
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


def save_upload(data):
    """A picture uploaded on attach, so the send itself only carries its path."""
    if not data.startswith(IMAGE_MAGIC) or len(data) > MAX_IMAGE:
        raise ValueError("not a JPEG/PNG under 10 MB")
    os.makedirs(UPLOADS, exist_ok=True)
    path = os.path.join(UPLOADS, "%d-%s.%s" % (time.time() * 1000, os.urandom(2).hex(), "png" if data.startswith(b"\x89") else "jpg"))
    with open(path, "wb") as f:
        f.write(data)
    return path


def _send(pane, b):
    """Pictures from the phone are saved as files and their paths sent to the agent with the text, the
    way /prompt sends text (herdr's prompt for an agent): Claude opens image files from a path. Typed key
    by key instead, Claude Code dropped the start of a long message and ignored the Enter (2026-10-18).
    Rejected: the Mac clipboard paste (an osascript plus a 0.5 s wait per picture, and it replaced the
    owner's clipboard)."""
    images, given = b.get("images") or [], b.get("paths") or []
    if not isinstance(images, list) or not isinstance(given, list) or len(images) + len(given) > 5:
        raise ValueError("images and paths must be lists, at most 5 together")
    # paths come from /api/uploads; only files directly in UPLOADS may be typed (no ../, no symlink out)
    if not all(isinstance(p, str) for p in given):
        raise ValueError("paths must be strings")
    given = [os.path.realpath(p) for p in given]
    if any(not os.path.isfile(p) or os.path.dirname(p) != os.path.realpath(UPLOADS) for p in given):
        raise ValueError("paths must be files sent to /api/uploads")
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
    text = " ".join(paths + given + [str(b.get("text") or "").strip()]).strip()
    if not text:
        raise ValueError("nothing to send")
    return _prompt(pane, {"text": text})


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


def agent_of(hub, pane):
    for w in hub.snapshot()["workspaces"]:
        for a in w["agents"]:
            if a["pane_id"] == pane:
                return a
    return {}


def session_of(hub, pane):
    return agent_of(hub, pane).get("session")


def _size(body):
    cols, rows = int(body["cols"]), int(body["rows"])
    if not (10 <= cols <= 500 and 5 <= rows <= 300):
        raise ValueError("size out of range")
    return cols, rows


WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
WEB_TYPES = {".html": "text/html", ".js": "text/javascript", ".css": "text/css",
             ".webmanifest": "application/manifest+json", ".svg": "image/svg+xml",
             ".png": "image/png", ".json": "application/json"}


def make_handler(hub, owner):   # owner None = --dev: no login check
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def _json(self, code, obj):
            # bridge.log: agent actions, uploads and every error; never the text, the bytes or headers
            note, self._note = getattr(self, "_note", None), None   # one line per reply, not per connection
            if code >= 400 or note is not None:
                sys.stderr.write("%s  %s %s  %d%s\n" % (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                                 self.command, urlparse(self.path).path, code, "  " + note if note else ""))
                sys.stderr.flush()
            data = json.dumps(obj).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):   # the phone hung up; nothing to tell it
                self.close_connection = True

        def _err(self, code, ecode, message):
            self._json(code, {"error": {"code": ecode, "message": message}})

        def _authed(self):
            # tailscale serve sets this from the caller's tailnet identity; the bridge only listens on
            # 127.0.0.1, so the header cannot come from anywhere else.
            if owner is not None and self.headers.get("Tailscale-User-Login") != owner:
                self.close_connection = True  # the request body was not read; do not reuse the connection
                self._err(403, "forbidden", "this bridge only answers its owner")
                return False
            return True

        def _static(self, name):
            # plain file names only: no slashes, so no way out of the web folder
            path = os.path.join(WEB_DIR, name)
            if not re.match(r"^[A-Za-z0-9._-]+$", name) or name.startswith("..") or not os.path.isfile(path):
                return self._err(404, "not_found", name)
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Type", WEB_TYPES.get(os.path.splitext(name)[1], "application/octet-stream"))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-cache")   # the page updates with the bridge
            self.end_headers()
            self.wfile.write(data)

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            return json.loads(self.rfile.read(n) or b"{}")

        def do_GET(self):
            if not self._authed():
                return
            u = urlparse(self.path)
            if u.path in ("/", "/index.html"):
                return self._static("index.html")
            if u.path.startswith("/web/"):
                return self._static(u.path[5:])
            if u.path == "/api/state":
                return self._json(200, hub.snapshot())
            if u.path == "/api/events":
                qs = parse_qs(u.query)
                gz = qs.get("gz") == ["1"] and "gzip" in (self.headers.get("Accept-Encoding") or "")
                return self._events(qs.get("watch", [None])[0], qs.get("v") == ["2"], qs.get("screen") == ["1"], gz)
            parts = u.path.strip("/").split("/")
            if len(parts) == 4 and parts[:2] == ["api", "agents"] and parts[3] == "history":
                try:
                    sid = (call("pane.get", {"pane_id": unquote(parts[2])})["pane"].get("agent_session") or {}).get("value")
                    items, title = transcript(sid)
                except HerdrError as e:
                    return self._err(409, e.code, str(e))
                limit = int(parse_qs(u.query).get("limit", ["500"])[0])
                return self._json(200, {"items": items[-limit:], "total": len(items), "title": title})
            if len(parts) == 6 and parts[:2] == ["api", "agents"] and parts[3] == "helpers" and parts[5] == "events":
                pane, hid = unquote(parts[2]), unquote(parts[4])
                if not HELPER_ID.match(hid):
                    return self._err(400, "bad_id", "not a helper id")
                d = helpers_dir(session_of(hub, pane))   # only that pane's own session folder; no path from the phone
                path = os.path.join(d, "agent-%s.jsonl" % hid) if d else None
                if not path or not os.path.isfile(path):
                    return self._err(404, "not_found", "no such helper")
                return self._helper_stream(pane, hid, path)
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
                if parts == ["api", "uploads"]:   # raw picture bytes, not JSON
                    n = int(self.headers.get("Content-Length") or 0)
                    self._note = "bytes=%d" % n
                    if n > MAX_IMAGE:
                        self.close_connection = True  # the body was not read
                        raise ValueError("over 10 MB")
                    return self._json(200, {"path": save_upload(self.rfile.read(n))})
                body = self._body()
                if parts == ["api", "watch"]:   # the phone opened another chat: switch its stream, no reconnect
                    with hub.lock:
                        q = STREAMS.get(body.get("stream"))
                    if q is None:
                        return self._err(404, "no_stream", "no such event stream")
                    pane = body.get("pane")
                    q.put(("watch", str(pane) if pane else None))
                    return self._json(200, {})
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
                    self._note = " ".join("%s=%d%s" % (k, len(body[k]), "ch" if k == "text" else "")
                                          for k in ("text", "paths", "images") if isinstance(body, dict) and isinstance(body.get(k), (str, list)))
                    ACTIONS[parts[3]](unquote(parts[2]), body)
                    hub.kick(unquote(parts[2]))
                    hub.poke()
                    return self._json(200, {"ok": True})
                if parts == ["api", "screen", "start"]:
                    kbps, fps = int(body.get("bitrate_kbps", 4000)), int(body.get("fps", 60))
                    if not (500 <= kbps <= 20000 and 1 <= fps <= 120):
                        raise ValueError("bitrate_kbps 500-20000, fps 1-120")
                    return self._json(200, screen("screen.start", {"bitrate_kbps": kbps, "fps": fps}))
                if parts == ["api", "screen", "stop"]:
                    return self._json(200, screen("screen.stop"))
                self._err(404, "not_found", self.path)
            except HerdrError as e:
                self._err(409, e.code, str(e))
            except (KeyError, ValueError, TypeError, OverflowError, AttributeError) as e:
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

        def _helper_stream(self, pane, hid, path):
            """One helper agent's conversation, read-only: history_full, then history per new entry."""
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.close_connection = True
            tail, next_ping = Tail(path, sidechain=True), time.time() + 15

            def full():
                self._sse("history_full", {"pane_id": pane, "helper_id": hid, "items": tail.items[-500:]})

            try:
                tail.read()
                full()
                while not self._hung_up():
                    try:
                        new = tail.read()
                    except OSError:   # file mid-rename or unreadable: try again next tick
                        new = []
                    if new is None:   # file replaced or truncated: send it whole again
                        tail.read()
                        full()
                    for ev in new or []:
                        self._sse("history", dict(ev, pane_id=pane, helper_id=hid))
                    if time.time() > next_ping:
                        self.wfile.write(b": ping\n\n")
                        self.wfile.flush()
                        next_ping = time.time() + 15
                    time.sleep(0.5)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass

        def _hung_up(self):
            """True once the other end closed the connection (a GET sends nothing more, so readable = EOF).
            Seen within 0.5 s, instead of waiting for a ping write to fail."""
            try:
                r = select.select([self.connection], [], [], 0)[0]
                return bool(r) and not self.connection.recv(1, socket.MSG_PEEK)
            except OSError:
                return True

        def _out(self, data):
            z = getattr(self, "_gz", None)
            if z:   # a sync flush per write: the event leaves now instead of waiting for more to compress
                data = z.compress(data) + z.flush(zlib.Z_SYNC_FLUSH)
            self.wfile.write(data)
            self.wfile.flush()

        def _sse(self, event, data):
            self._out(("event: %s\ndata: %s\n\n" % (event, json.dumps(data))).encode())

        def _events(self, watch, v2=False, want_screen=False, gz=False):
            # v2 (web chat, iPhone app from Part 3): history carries the item's index, and `live` replaces
            # the raw screen, which is only sent when asked (screen=1): the spinner redraws it 5 times a
            # second, too much for mobile data. v1 (current iPhone app): history and screen as before.
            # No Content-Length: the body runs until the connection closes, which is valid HTTP/1.1.
            # gz (app asks with gz=1): one gzip stream for the whole connection, ~1/6 of the bytes.
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            if gz:
                self.send_header("Content-Encoding", "gzip")
            self.end_headers()
            self.close_connection = True
            self._gz = zlib.compressobj(6, zlib.DEFLATED, 31) if gz else None
            q = hub.subscribe(watch)
            stream = secrets.token_urlsafe(16)   # POST /api/watch names it to switch this stream's chat
            with hub.lock:
                STREAMS[stream] = q
            last, next_ping = None, time.time() + 15
            tail, session = None, False   # False: not looked up yet (None is a real answer: a plain shell)
            status, last_live = None, None
            last_helpers, next_helpers = [], 0   # [] = what the phone starts with: no event for a chat without helpers

            def full():   # the whole transcript: the phone shows the newest part and loads older on request
                self._sse("history_full", {"pane_id": watch, "items": tail.items, "title": tail.title})

            try:
                self._sse("hello", {"stream": stream})
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
                        changed = text is not None and text != last
                        if changed:
                            last = text
                            if not v2 or want_screen:
                                self._sse("screen", {"pane_id": watch, "text": text})
                        # Claude Code appends each message to its transcript the moment it happens. Follow
                        # that file (one stat per tick, on this computer; nothing crosses the network unless
                        # it grew) and push new entries at once, instead of the phone re-fetching on timers.
                        try:
                            sid = session_of(hub, watch)
                            if tail is None or sid != session:
                                give_tail(tail)
                                tail = None   # if take_tail fails, finally must not give back the old one twice
                                session, tail = sid, take_tail(sid)
                                full()
                                changed = True
                            else:
                                size = len(tail.items)
                                new = tail.read()
                                if new is None:   # file replaced or truncated: send it whole again
                                    tail.read()
                                    full()
                                    changed = True
                                else:
                                    for ev in new:
                                        changed = True   # the last item may now be what the screen shows
                                        append = ev["index"] == size
                                        size += append
                                        if v2:
                                            self._sse("history", dict(ev, pane_id=watch))
                                        elif append or ev["replace_last"]:   # v1 cannot place an earlier-item update
                                            self._sse("history", {"pane_id": watch, "item": ev["item"], "replace_last": ev["replace_last"]})
                        except (BrokenPipeError, ConnectionResetError):
                            raise   # the phone went away: stop the stream
                        except OSError:
                            pass    # transcript file mid-rename or unreadable: try again next tick
                        if time.time() >= next_helpers:   # a listdir + stats, at most every 2 s; sent only when it changed
                            next_helpers = time.time() + 2
                            hs = helpers(session) if session else []
                            # a working helper's last_active moves on every write; the phone shows only
                            # "working" for it, so that alone is no reason to resend ~10 KB every 2 s
                            key = [dict(h, last_active=None) if h["working"] else h for h in hs]
                            if key != last_helpers:
                                last_helpers = key
                                self._sse("helpers", {"pane_id": watch, "items": hs})
                        now = agent_of(hub, watch).get("status", "unknown")
                        if v2 and last is not None and (changed or now != status):
                            status = now
                            try:
                                live = dict(live_view(last, tail.items if tail else [], status), pane_id=watch)
                            except Exception:   # a screen layout we did not foresee must not end the stream
                                traceback.print_exc()
                                live = last_live
                            if live != last_live:
                                last_live = live
                                self._sse("live", live)
                    try:
                        kind, data = q.get(timeout=0.2)
                        if kind == "kick":
                            time.sleep(0.03)   # give Claude a moment to draw the key, then re-read at once
                            continue
                        if kind == "watch":   # start over on the new chat; the next tick sends its screen and history_full
                            t, tail = tail, None   # cleared first: a failed write below must not give it back twice
                            give_tail(t)
                            if last_helpers:   # the phone clears the old chat's helper list
                                self._sse("helpers", {"pane_id": watch, "items": []})
                            watch = q.watch = data or None
                            last, session = None, False
                            status, last_live = None, None
                            last_helpers, next_helpers = [], 0
                            continue
                        self._sse(kind, data)
                    except queue.Empty:
                        pass
                    if time.time() > next_ping:
                        self._out(b": ping\n\n")
                        next_ping = time.time() + 15
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with hub.lock:
                    STREAMS.pop(stream, None)
                hub.unsubscribe(q)
                give_tail(tail)
                if self._gz:   # the gzip trailer; the phone has usually gone already
                    try:
                        self.wfile.write(self._gz.flush())
                    except (OSError, ValueError, zlib.error):
                        pass
                    self._gz = None

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
    ap.add_argument("--dev", action="store_true",
                    help="no Tailscale login check (127.0.0.1 only), for testing the web client in a local browser")
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
    if not args.dev and not cfg.get("owner"):
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
    owner = None if args.dev else cfg["owner"]
    if args.dev:
        print("dev mode: no login check (127.0.0.1 only)", file=sys.stderr, flush=True)
    srv = make_server(hub, owner, port)
    print("herdr-remote bridge on 127.0.0.1:%d for %s" % (port, owner or "anyone (dev)"), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
