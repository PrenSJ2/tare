"""A local console for the whole brain: capabilities, memory, and agents.

## Why this exists alongside agent-flow

agent-flow renders live orchestration and does it well, so harness keeps it as
an upstream dependency rather than forking it. This serves the parts it cannot:

- **the capability graph** — 209 nodes and their `routes-to` edges, out of
  `harness.db`, which agent-flow has no knowledge of
- **usage memory** — gaps, per-project usage, token weight
- **history** — agent-flow starts empty and shows only what arrives after it
  launches. This reconstructs every agent that has ever run from the
  transcripts already on disk, so opening it later still tells you everything.

And it needs **no hooks**, so unlike a hook-driven view it costs nothing per
tool call.

## Bound to loopback, no authentication

It reads a developer's private working history: transcripts, file paths, shell
commands. It must never be reachable off this machine, so it binds 127.0.0.1
and there is no flag to change that.

## Cost of a refresh

Every poll re-walks the recent transcripts and re-queries SQLite. That is a few
hundred milliseconds on a real corpus and it is why the page polls on an
interval rather than per keystroke -- and why the payload is capped rather than
streaming the raw transcripts.
"""

from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import audit as audit_mod
from . import db, memory, paths

HOST = "127.0.0.1"
DEFAULT_PORT = 4242

# How many agents to describe per project. The record is complete on disk; the
# page is capped so a poll stays fast, and it says how many it left out.
AGENTS_PER_PROJECT = 40


def _reader():
    """swarm's transcript reader, if it is installed.

    Optional on purpose: harness must work without swarm, and the console
    degrades to its own two views rather than failing outright.
    """
    try:
        from swarm import reader  # noqa: PLC0415 - optional dependency, probed at call time
        return reader
    except Exception:
        return None


def _graph(conn: sqlite3.Connection) -> dict:
    nodes = [dict(r) for r in conn.execute(
        "SELECT id,kind,name,origin,state,est_tokens,purpose_line,provider_plugin FROM nodes")]
    usage = {r["node_id"]: r["invocations"]
             for r in conn.execute("SELECT node_id, invocations FROM usage")}
    ids = {n["id"] for n in nodes}
    edges = [(r["src"], r["dst"])
             for r in conn.execute("SELECT src, dst FROM edges WHERE type = 'routes-to'")]
    return {
        "nodes": [{"i": n["id"], "n": n["name"], "k": n["kind"], "o": n["origin"],
                   "s": n["state"], "t": n["est_tokens"] or 0, "u": usage.get(n["id"], 0),
                   "p": (n["purpose_line"] or "")[:140], "pl": n["provider_plugin"]}
                  for n in nodes],
        # Drop edges whose endpoints are gone: a prune can outrun an edge rebuild,
        # and a dangling edge would silently vanish a node from the layout.
        "edges": [{"s": s, "d": d} for s, d in edges if s in ids and d in ids],
    }


def _orchestration(redact: bool) -> dict:
    """The dispatch tree, file attention and tool mix for the newest sessions.

    This is the part modelled on agent-flow -- branching, a file heatmap, a
    tool distribution -- rebuilt from transcripts so it works retroactively and
    without hooks. Scoped to a few recent sessions because the tree walk reads
    every subagent transcript it reaches.
    """
    reader = _reader()
    if reader is None:
        return {"edges": [], "files": [], "tools": [], "depth": {}}
    edges, files, tools, depth = [], {}, {}, {}
    # Take the newest sessions that actually dispatched something. Simply
    # slicing the newest N catches idle sessions and returns an empty tree
    # while a rich one sits just outside the window.
    kept = 0
    for _project, session in reader.all_sessions()[:14]:
        if kept >= 4:
            break
        try:
            orch = reader.orchestration(session, redact=redact)
        except Exception:
            continue
        if not orch.depth:
            continue
        kept += 1
        for child, parent in orch.parent_of.items():
            edges.append({"p": parent or session[:8], "c": child[:10]})
        for path_str, count in orch.files:
            files[path_str] = files.get(path_str, 0) + count
        for name, count in orch.tools:
            tools[name] = tools.get(name, 0) + count
        for agent_id, d in orch.depth.items():
            depth[agent_id[:10]] = d
    home = str(Path.home())
    return {
        "edges": edges[:400],
        "files": sorted(((p.replace(home, "~"), c) for p, c in files.items()),
                        key=lambda kv: -kv[1])[:20],
        "tools": sorted(tools.items(), key=lambda kv: -kv[1]),
        "depth": depth,
    }


def _fleet(redact: bool) -> dict:
    reader = _reader()
    if reader is None:
        return {"generated": datetime.now().astimezone().isoformat(), "projects": [],
                "unavailable": "swarm is not installed — agent history needs it"}
    now = datetime.now().astimezone()
    home = str(Path.home())
    projects = []
    for project, runs in sorted(reader.fleet(redact=redact, now=now).items(),
                                key=lambda kv: -len(kv[1])):
        agents = []
        for run in sorted(runs, key=lambda r: (r.status != "running", -(r.seconds or 0))):
            info = reader.detail(run.agent_id, redact=redact)
            agents.append({
                "id": run.agent_id[:10], "label": run.label, "type": run.agent_type,
                "model": run.model, "status": run.status,
                "secs": round(run.seconds) if run.seconds else None,
                "turns": info.turns if info else 0,
                "tools": info.tools if info else [],
                "files": [f.replace(home, "~") for f in (info.files[:6] if info else [])],
                "cmds": info.commands[:4] if info else [],
                "report": (info.report[:280] if info else ""),
            })
        projects.append({"name": project, "agents": agents})
    return {"generated": now.isoformat(), "projects": projects}


def payload(*, redact: bool = False) -> dict:
    """Everything the page needs, read fresh."""
    conn = db.connect()
    try:
        report = audit_mod.audit(conn)
        graph = _graph(conn)
        mem = {
            "learned": [{"kind": s.kind, "subject": s.subject, "detail": s.detail,
                         "evidence": s.evidence} for s in memory.suggestions(conn)],
            "projects": memory.by_project(conn, limit=8),
            "instructions": [{"tok": t, "lines": l, "proj": p, "file": f}
                             for t, l, p, f in report.instructions],
            "index_tokens": report.total_tokens,
            "event_counts": {r["kind"]: r["c"] for r in
                             conn.execute("SELECT kind, COUNT(*) c FROM events GROUP BY kind")},
        }
    finally:
        conn.close()

    return {
        "totals": {
            "live_tokens": report.total_tokens,
            "never_invoked_tokens": report.never_invoked_tokens,
            # The "before" figure is what the index cost prior to shelving; the
            # difference is what the vault is currently buying.
            "before": report.total_tokens + report.disabled_tokens
                      + sum(n["t"] for n in graph["nodes"] if n["s"] == "vaulted"),
        },
        "nodes": graph["nodes"],
        "edges": graph["edges"],
        "memory": mem,
        "fleet": _fleet(redact),
        "orch": _orchestration(redact),
    }


def _page() -> bytes:
    return (Path(__file__).parent / "web" / "console.html").read_bytes()


class _Handler(BaseHTTPRequestHandler):
    redact = False

    def _send(self, body: bytes, content_type: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
        try:
            if self.path.startswith("/api/timeline"):
                # Fetched on demand when a row is expanded, so the poll payload
                # stays small -- a timeline per agent for 229 agents would not.
                from urllib.parse import parse_qs, urlparse
                agent = (parse_qs(urlparse(self.path).query).get("agent") or [""])[0]
                reader = _reader()
                events = []
                if reader and agent:
                    # The payload truncates agent ids to 10 chars for display,
                    # so what comes back here is a PREFIX. Resolving it beats
                    # shipping full ids for 229 agents to serve the one that
                    # gets clicked.
                    full = agent if len(agent) > 12 else next(
                        (k for k in reader.subagent_files() if k.startswith(agent)), None)
                    if full:
                        events = reader.tool_timeline(full)
                self._send(json.dumps([
                    {"at": e.at.isoformat(), "tool": e.tool, "detail": e.detail}
                    for e in events]).encode(), "application/json; charset=utf-8")
            elif self.path.startswith("/api/data"):
                self._send(json.dumps(payload(redact=self.redact)).encode(),
                           "application/json; charset=utf-8")
            elif self.path in ("/", "/index.html"):
                self._send(_page(), "text/html; charset=utf-8")
            else:
                self.send_error(404)
        except BrokenPipeError:
            pass          # the browser navigated away mid-response; not a fault
        except Exception as exc:  # noqa: BLE001 - a viewer must not take itself down
            try:
                self.send_error(500, str(exc))
            except Exception:
                pass

    def log_message(self, *args) -> None:
        """Silence: this runs behind the operator's work, not in front of it."""


def is_up(port: int = DEFAULT_PORT) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.25)
        return probe.connect_ex((HOST, port)) == 0


def url(port: int = DEFAULT_PORT) -> str:
    return f"http://{HOST}:{port}/"


def serve(port: int = DEFAULT_PORT, *, redact: bool = False, open_browser: bool = False) -> int:
    handler = type("Handler", (_Handler,), {"redact": redact})
    try:
        httpd = ThreadingHTTPServer((HOST, port), handler)
    except OSError as exc:
        print(f"cannot bind {HOST}:{port} — {exc}", file=sys.stderr)
        return 1
    if open_browser:
        import webbrowser
        threading.Timer(0.4, lambda: webbrowser.open(url(port))).start()
    print(f"harness console at {url(port)}  (ctrl-c to stop)")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
    return 0


def ensure(port: int = DEFAULT_PORT) -> bool:
    """Start the console if nothing is on the port. Silent and non-blocking."""
    try:
        if is_up(port):
            return True
        subprocess.Popen(
            [sys.executable, "-m", "harness.console", "--port", str(port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        return True
    except Exception:
        return False


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(prog="harness.console")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--redact", action="store_true")
    parser.add_argument("--open", action="store_true")
    args = parser.parse_args()
    raise SystemExit(serve(args.port, redact=args.redact, open_browser=args.open))
