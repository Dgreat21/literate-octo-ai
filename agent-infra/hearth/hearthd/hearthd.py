#!/usr/bin/env python3
"""hearthd — read-mostly aggregator of harness state on top of the herdr socket API.

Serves the Hearth PWA (../app) and a small JSON API for the phone.

Design invariant (INFRA-7 §3): the app must never lie about the link.
Every JSON response carries `as_of`; when herdr is unreachable the daemon
answers 503 with an explicit error instead of the last known good state.
Nothing here is cached across a failure — a stale answer would be a lie.

Stdlib only. Python 3.9+.
"""
from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

APP_DIR = Path(__file__).resolve().parent.parent / "app"
TOKEN_FILE = Path.home() / ".hearth" / "token"
DSH_URL = "http://127.0.0.1:3081"   # the dsh web server; overridden by --dsh
DSH_TIMEOUT = 6.0
HERDR_TIMEOUT = 4.0          # seconds; herdr is local, anything slower is a fault
SNAPSHOT_TTL = 1.0           # seconds; de-bounce polling, never survives an error
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][^\x07]*\x07|\x1b[()][B0]")

# herdr agent lifecycle states -> what the phone shows
STATUS_LABEL = {
    "working": "working",
    "idle": "idle",
    "blocked": "blocked",
    "done": "done",
    "unknown": "unknown",
}


class HerdrError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def herdr(*args: str, timeout: float = HERDR_TIMEOUT) -> str:
    """Run a herdr CLI command, returning stdout. Raises HerdrError on any fault."""
    try:
        proc = subprocess.run(
            ["herdr", *args],
            capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        raise HerdrError("herdr_missing", "herdr is not on PATH")
    except subprocess.TimeoutExpired:
        raise HerdrError("herdr_timeout", f"herdr {' '.join(args)} took >{timeout}s")
    out = (proc.stdout or "").strip()
    if proc.returncode != 0:
        raise HerdrError("herdr_failed", (proc.stderr or out or "").strip()[:400])
    return out


def herdr_json(*args: str, timeout: float = HERDR_TIMEOUT) -> dict:
    out = herdr(*args, timeout=timeout)
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        raise HerdrError("herdr_garbled", out[:200])
    if isinstance(data, dict) and "error" in data:
        err = data["error"]
        raise HerdrError(str(err.get("code", "herdr_error")), str(err.get("message", "")))
    return data


def strip_ansi(s: str) -> str:
    return ANSI.sub("", s).replace("\r", "")


class DshError(RuntimeError):
    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code
        self.detail = detail


def dsh_rpc(method: str, payload: dict, timeout: float = DSH_TIMEOUT) -> dict:
    """Call one dsh apiproxy method. Raises DshError on any fault.

    This is the half of the herd that herdr cannot see: sessions of `dsh web`
    live inside the server process, not in a pane. Sourcing only from herdr
    makes the phone report an idle pane while an agent works for half an hour.
    """
    body = json.dumps({
        "type": "client-request", "rpcId": f"hearthd-{int(time.time()*1000)}",
        "method": method, "payload": payload,
    }).encode()
    req = Request(f"{DSH_URL}/api/{method}", data=body,
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode())
    except HTTPError as e:
        raise DshError("dsh_http", f"{e.code} {e.reason}")
    except URLError as e:
        raise DshError("dsh_unreachable", str(e.reason)[:200])
    except (TimeoutError, socket.timeout):
        raise DshError("dsh_timeout", f">{timeout}s")
    except json.JSONDecodeError:
        raise DshError("dsh_garbled", "")
    result = data.get("result", {})
    if not result.get("ok"):
        err = result.get("error", {})
        raise DshError(str(err.get("code", "dsh_error")), str(err.get("message", ""))[:200])
    return result.get("value", {})


def _tok_s(stats: dict) -> float | None:
    ms = stats.get("decodeMs") or 0
    tok = stats.get("decodeTokens") or 0
    return round(tok / (ms / 1000), 1) if ms > 0 and tok else None


def read_sessions() -> list[dict]:
    """Running-first list of dsh sessions, flattened to what a phone can read."""
    items = dsh_rpc("session.list", {}).get("items", [])
    out = []
    for it in items:
        proj = (it.get("projections") or {}).get("values") or {}
        stats = proj.get("sessionStats") or {}
        usage = proj.get("tokenUsage") or {}
        press = proj.get("contextPressure") or {}
        window = press.get("contextWindow") or 0
        out.append({
            "kind": "session",
            "id": it.get("sessionId"),
            "title": (proj.get("title") or "").strip() or "(untitled)",
            "running": bool(it.get("running")),
            "blank": bool(it.get("blank")),
            "cwd": it.get("cwd") or "",
            "preset": it.get("agentPreset") or "",
            "updated_at": it.get("updatedAt"),
            "turns": stats.get("turns"),
            "steps": stats.get("steps"),
            "out_tokens": usage.get("outputTokens"),
            "tok_s": _tok_s(stats),
            "ctx_pct": (round(100 * press.get("pressureTokens", 0) / window)
                        if window else None),
        })
    out.sort(key=lambda s: (not s["running"], -(s.get("updated_at") or 0)))
    return out


def _text_of(message: dict) -> str:
    parts = []
    for c in (message.get("content") or []):
        if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
            parts.append(str(c["text"]))
    return " ".join(parts).strip()


def read_session_digest(session_id: str, steps: int) -> dict:
    """Step digest plus one raw line — variant 1b-iii, now that events exist.

    The review chose 1b-iii but noted it needs an event stream rather than log
    parsing. session.history is that stream: step/start, assistant/message,
    tool/call, tool/result. Nothing here is inferred from terminal text.
    """
    value = dsh_rpc("session.history", {"sessionId": session_id,
                                        "maxMessages": max(20, steps * 6)})
    events = [e.get("event", e) for e in (value.get("events") or [])]
    by_step: dict[tuple, dict] = {}
    order: list[tuple] = []
    raw_last = ""

    for ev in events:
        etype, data = ev.get("type"), (ev.get("data") or {})
        key = (data.get("turn"), data.get("step"))
        if etype in ("assistant/message", "tool/call", "step/start") and key[1] is not None:
            if key not in by_step:
                by_step[key] = {"turn": key[0], "step": key[1], "text": "", "tools": [],
                                "time": ev.get("time")}
                order.append(key)
        if etype == "assistant/message":
            said = _text_of(data.get("message") or {})
            if said:
                by_step[key]["text"] = said.splitlines()[0][:180]
        elif etype == "tool/call":
            name = data.get("name")
            if name:
                by_step[key]["tools"].append(name)
        elif etype == "tool/result":
            # One raw line of ground truth beside the digest (1b-iii): if the
            # digest is wrong, this is what the tool actually answered.
            for c in ((data.get("message") or {}).get("content") or []):
                got = c.get("content") if isinstance(c, dict) else None
                if isinstance(got, list):
                    got = " ".join(str(x.get("text", "")) for x in got
                                   if isinstance(x, dict) and x.get("type") == "text")
                if isinstance(got, str) and got.strip():
                    lines = [l for l in got.strip().splitlines() if l.strip()]
                    if lines:
                        raw_last = lines[-1].strip()[:200]
        elif etype == "user/message":
            said = _text_of(data)
            if said:
                raw_last = said.splitlines()[0][:200]

    digest = [by_step[k] for k in order][-steps:]
    return {"digest": digest, "raw_last": raw_last, "has_more": bool(value.get("hasMore"))}


class Snapshot:
    """Herd state with a one-second de-bounce. A failure is never cached."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: dict | None = None
        self._at = 0.0

    def get(self) -> dict:
        with self._lock:
            now = time.time()
            if self._value is not None and now - self._at < SNAPSHOT_TTL:
                return self._value
            value = self._read()
            self._value, self._at = value, time.time()
            return value

    @staticmethod
    def _read() -> dict:
        # `pane list` over `agent list`: it shows every pane, with an agent label
        # only where herdr actually detected one. Listing only detected agents
        # would quietly hide panes the phone is meant to be a window onto.
        data = herdr_json("pane", "list")
        raw = data.get("result", {}).get("panes", [])
        agents = []
        for a in raw:
            status = a.get("agent_status", "unknown")
            agents.append({
                "pane_id": a.get("pane_id"),
                "workspace": a.get("workspace_id"),
                "tab": a.get("tab_id"),
                "agent": a.get("agent") or "shell",
                "status": status,
                "label": STATUS_LABEL.get(status, status),
                "title": (a.get("terminal_title_stripped") or a.get("terminal_title") or "").strip(),
                "cwd": a.get("foreground_cwd") or a.get("cwd") or "",
                "focused": bool(a.get("focused")),
                "seq": a.get("state_change_seq"),
            })
        order = {"blocked": 0, "working": 1, "idle": 2, "unknown": 3, "done": 4}
        agents.sort(key=lambda x: (order.get(x["status"], 9), x["pane_id"] or ""))
        summary = {k: 0 for k in ("working", "blocked", "idle", "done", "unknown")}
        for a in agents:
            summary[a["status"]] = summary.get(a["status"], 0) + 1
        return {"agents": agents, "summary": summary}


SNAPSHOT = Snapshot()


def short_path(p: str) -> str:
    home = str(Path.home())
    return "~" + p[len(home):] if p.startswith(home) else p


def read_tail(pane_id: str, lines: int) -> list[str]:
    out = herdr("pane", "read", pane_id, "--lines", str(lines), "--format", "text")
    return [l.rstrip() for l in strip_ansi(out).split("\n")]


class Handler(BaseHTTPRequestHandler):
    server_version = "hearthd/0.1"
    token = ""

    # ---------- plumbing ----------

    def log_message(self, fmt, *args):  # quieter than the default one-line-per-hit
        if self.server.verbose:  # type: ignore[attr-defined]
            sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store" if ctype.startswith("application/json") else "no-cache")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        now = time.time()
        payload.setdefault("as_of", round(now, 3))
        payload.setdefault("as_of_iso", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now)))
        self._send(code, json.dumps(payload, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _authorised(self, query: dict) -> bool:
        if not self.token:
            return True
        header = (self.headers.get("X-Hearth-Token") or "").strip()
        supplied = header or (query.get("t", [""])[0])
        return secrets.compare_digest(supplied, self.token)

    # ---------- routing ----------

    def do_GET(self) -> None:
        self._route()

    def do_HEAD(self) -> None:
        self._route()

    def do_POST(self) -> None:
        self._route()

    def _route(self) -> None:
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        if path.startswith("/api/"):
            if not self._authorised(query):
                return self._json(401, {"error": "unauthorised"})
            try:
                return self._api(path, query)
            except HerdrError as e:
                # Honest unreachable beats a cached lie.
                return self._json(503, {"error": e.code, "detail": e.detail})
            except Exception as e:  # noqa: BLE001 - surface, never swallow
                return self._json(500, {"error": "hearthd_fault", "detail": repr(e)[:300]})
        return self._static(path)

    def _api(self, path: str, query: dict) -> None:
        parts = [unquote(p) for p in path.split("/") if p][1:]  # drop "api"; pane ids carry ":"

        if parts == ["health"]:
            return self._json(200, {"ok": True, "host": socket.gethostname()})

        if parts == ["state"]:
            # Two independent sources. Either may be down on its own, and the
            # phone is told WHICH — half the herd rendered as the whole herd is
            # the same lie as stale numbers rendered as fresh.
            sources, agents, summary, sessions = {}, [], {}, []
            try:
                snap = SNAPSHOT.get()
                agents = [{**a, "cwd_short": short_path(a["cwd"])} for a in snap["agents"]]
                summary = snap["summary"]
                sources["herdr"] = "ok"
            except HerdrError as e:
                sources["herdr"] = f"{e.code}: {e.detail}"[:160]
            try:
                sessions = [{**x, "cwd_short": short_path(x["cwd"])} for x in read_sessions()]
                sources["dsh"] = "ok"
            except DshError as e:
                sources["dsh"] = f"{e.code}: {e.detail}"[:160]

            if "ok" not in sources.values():
                return self._json(503, {"error": "no_sources", "sources": sources})
            return self._json(200, {
                "host": socket.gethostname(),
                "sources": sources,
                "agents": agents,
                "summary": summary,
                "sessions": sessions,
                "running": [s for s in sessions if s["running"]],
            })

        if len(parts) >= 2 and parts[0] == "session":
            session_id = parts[1]
            if len(parts) == 2 and self.command in ("GET", "HEAD"):
                try:
                    steps = max(3, min(60, int(query.get("steps", ["14"])[0])))
                except ValueError:
                    steps = 14
                try:
                    meta = next((x for x in read_sessions() if x["id"] == session_id), None)
                    if meta is None:
                        return self._json(404, {"error": "unknown_session", "detail": session_id})
                    body = read_session_digest(session_id, steps)
                except DshError as e:
                    return self._json(503, {"error": e.code, "detail": e.detail})
                return self._json(200, {**meta, "cwd_short": short_path(meta["cwd"]), **body})

            if parts[2:] == ["cancel"] and self.command == "POST":
                # Same discipline as the pane stop: ask, then report what dsh
                # says now. Never assume the cancel landed.
                try:
                    dsh_rpc("session.cancel", {"sessionId": session_id})
                    fresh = next((x for x in read_sessions() if x["id"] == session_id), None)
                except DshError as e:
                    return self._json(503, {"error": e.code, "detail": e.detail})
                return self._json(202, {
                    "sent": "session.cancel", "session_id": session_id,
                    "running_now": bool(fresh and fresh["running"]),
                    "settled": bool(fresh and not fresh["running"]),
                })

        if len(parts) >= 2 and parts[0] == "agent":
            pane_id = parts[1]
            known = {a["pane_id"]: a for a in SNAPSHOT.get()["agents"]}
            if pane_id not in known:
                return self._json(404, {"error": "unknown_pane", "detail": pane_id})
            agent = known[pane_id]

            if len(parts) == 2 and self.command in ("GET", "HEAD"):
                try:
                    lines = max(5, min(200, int(query.get("lines", ["60"])[0])))
                except ValueError:
                    lines = 60
                tail = read_tail(pane_id, lines)
                return self._json(200, {
                    **agent,
                    "cwd_short": short_path(agent["cwd"]),
                    "tail": tail,
                    "tail_source": "raw",  # 1b-iii (digest) needs an event stream — not in v0
                })

            if parts[2:] == ["stop"] and self.command == "POST":
                # Fire the interrupt, then report what herdr says *now*.
                # The phone must not assume the run died: it waits for the status to move.
                try:
                    herdr("agent", "send-keys", pane_id, "ctrl+c")
                    route = "agent"
                except HerdrError:
                    herdr("pane", "send-keys", pane_id, "ctrl+c")
                    route = "pane"
                time.sleep(0.35)
                fresh = next((a for a in Snapshot._read()["agents"] if a["pane_id"] == pane_id), None)
                return self._json(202, {
                    "sent": "ctrl+c", "via": route, "pane_id": pane_id,
                    "status_before": agent["status"],
                    "status_now": (fresh or {}).get("status", "unknown"),
                    "settled": bool(fresh and fresh["status"] != "working"),
                })

        return self._json(404, {"error": "no_such_route", "detail": path})

    def _static(self, path: str) -> None:
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (APP_DIR / rel).resolve()
        if not str(target).startswith(str(APP_DIR.resolve())) or not target.is_file():
            return self._send(404, b"not found", "text/plain; charset=utf-8")
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if target.suffix == ".webmanifest":
            ctype = "application/manifest+json"
        if ctype.startswith("text/") or "javascript" in ctype or "json" in ctype:
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)


def resolve_token(arg: str | None) -> str:
    if arg == "":
        return ""  # explicit --token '' disables auth (loopback only, your call)
    if arg:
        return arg
    if TOKEN_FILE.exists():
        return TOKEN_FILE.read_text().strip()
    token = secrets.token_urlsafe(12)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(token + "\n")
    TOKEN_FILE.chmod(0o600)
    return token


def lan_ip() -> str | None:
    for iface in ("en0", "en1"):
        try:
            out = subprocess.run(["ipconfig", "getifaddr", iface],
                                 capture_output=True, text=True, timeout=2).stdout.strip()
            if out:
                return out
        except Exception:  # noqa: BLE001
            pass
    return None


def main() -> int:
    global DSH_URL
    ap = argparse.ArgumentParser(description="hearthd — harness state for the phone")
    ap.add_argument("--host", default="127.0.0.1",
                    help="bind address; use 0.0.0.0 to reach it from the phone on the same wifi")
    ap.add_argument("--port", type=int, default=8788)
    ap.add_argument("--token", default=None,
                    help="shared secret; defaults to ~/.hearth/token, '' disables auth")
    ap.add_argument("--dsh", default=DSH_URL, help=f"dsh web server base URL (default {DSH_URL})")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    try:
        herdr("status", "server", timeout=5)
    except HerdrError as e:
        print(f"warning: herdr not answering ({e.code}: {e.detail}) — "
              f"hearthd will start and report 'unreachable' honestly", file=sys.stderr)

    DSH_URL = args.dsh.rstrip("/")
    try:
        n = len(read_sessions())
        print(f"dsh at {DSH_URL}: {n} session(s)", flush=True)
    except DshError as e:
        print(f"warning: dsh not answering at {DSH_URL} ({e.code}) — "
              f"sessions will show as unavailable, panes still work", file=sys.stderr)

    Handler.token = resolve_token(args.token)
    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.verbose = args.verbose  # type: ignore[attr-defined]
    httpd.daemon_threads = True

    frag = f"#t={Handler.token}" if Handler.token else ""
    print(f"hearthd on http://{args.host}:{args.port}{frag}", flush=True)
    if args.host == "0.0.0.0":
        ip = lan_ip()
        if ip:
            print(f"  from the phone: http://{ip}:{args.port}{frag}", flush=True)
    print(f"  serving {APP_DIR}", flush=True)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nhearthd stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
