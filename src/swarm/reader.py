"""Reconstruct a session's agent runs from Claude Code's own transcripts.

## Why not the hook stream

Sub-project A captures `subagent_start`/`subagent_stop` via hooks. Measured on
a real 106-dispatch session, that stream yields 79 stop records of which **7**
can be resolved to anything at all, and 72 of 80 carry `agent_type: null`. It
has no label, no model, and no duration. A view built on it is a list of
anonymous timestamps.

The transcripts already on disk carry all of it, and join cleanly:

    main transcript, tool_use   -> description, subagent_type, model
    main transcript, tool_result-> agentId
    subagents/agent-<id>.jsonl  -> first/last timestamp = real start and end

On the same session that resolves **106 of 106**. So this reads transcripts and
leaves the hook stream to whatever else wants it.

## What this reads, and what it deliberately does not keep

It reads authored text -- the `description` a dispatch was given ("Review Task
8"). That is the whole point: an unlabelled view is useless. Nothing leaves the
machine, and `redact=True` drops labels for a shared screen or a work machine.

`read_session` keeps only the dispatch label, agent type, model, timestamps and
a line count.

`detail()` goes further on purpose, because a fleet view that cannot say what
an agent actually DID is just a list of names: it summarises the tools an agent
reached for, the files it touched, the commands it ran, and the final report it
returned. It never keeps prompts or file contents, truncates the report, and
`redact=True` reduces files, commands and report to bare counts for a shared
screen or a work machine.
"""

from __future__ import annotations

import json
import re
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import paths

_AGENT_TOOLS = ("Agent", "Task")
_AGENT_ID_RE = re.compile(r"agentId:\s*([a-z0-9]{8,})")
# How long a subagent transcript may sit untouched before a run with no result
# is presumed dead rather than still working.
STALE_AFTER_SECONDS = 900


@dataclass
class AgentRun:
    agent_id: str
    label: str
    agent_type: str | None
    model: str | None
    dispatched: datetime | None
    started: datetime | None
    ended: datetime | None
    lines: int
    status: str  # "running" | "done" | "unknown"

    @property
    def seconds(self) -> float | None:
        if self.started and self.ended:
            return (self.ended - self.started).total_seconds()
        return None


def _ts(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _iter_json(path: Path):
    """Yield parsed objects, skipping unparseable lines.

    A transcript is being appended to by a live session while this reads it, so
    a truncated final line is normal rather than a fault, and the file can
    vanish between listing and open.
    """
    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    yield obj
    except OSError:
        return


def _blocks(obj):
    content = (obj.get("message") or {}).get("content")
    return content if isinstance(content, list) else []


def session_transcript(session: str) -> Path | None:
    """The main transcript for a session id, wherever it lives."""
    for candidate in paths.projects_dir().rglob(f"{session}.jsonl"):
        return candidate
    return None


# The directory listing is the hot path, not the parsing. `detail()`,
# `fleet()` and `orchestration()` each call subagent_files(), and it rglobs
# every subagent transcript on the machine -- 881 of them here. Called
# thousands of times to build one payload, that alone took the API past 60
# seconds and left the UI stuck on "reading" forever.
_FILES_CACHE: tuple[float, Path, dict[str, Path]] | None = None
FILES_TTL_SECONDS = 5.0


def subagent_files(*, fresh: bool = False) -> dict[str, Path]:
    """agent_id -> its own transcript. The id is in the filename, which is the
    only place the two sources reliably meet.

    Cached for a few seconds. A new agent appearing is visible within the TTL,
    which is far below the interval anything polls this at.
    """
    global _FILES_CACHE
    now = time.monotonic()
    root = paths.projects_dir()
    # Keyed on the root, not just time: the projects directory moves when
    # SWARM_HOME/CLAUDE_CONFIG_DIR changes, and serving one home's listing to
    # another would be wrong rather than merely stale.
    if (not fresh and _FILES_CACHE is not None
            and _FILES_CACHE[1] == root
            and now - _FILES_CACHE[0] < FILES_TTL_SECONDS):
        return _FILES_CACHE[2]

    out: dict[str, Path] = {}
    for path in root.rglob("subagents/agent-*.jsonl"):
        out[path.stem[len("agent-"):]] = path
    _FILES_CACHE = (now, root, out)
    return out


def _span(path: Path) -> tuple[datetime | None, datetime | None, int]:
    """(first timestamp, last timestamp, line count) for a subagent transcript.

    The dispatch's own tool_result is NOT the end: an async dispatch is
    acknowledged in milliseconds and the agent runs on well afterwards. The
    only honest end time is the last line this agent actually wrote.
    """
    first = last = None
    count = 0
    for obj in _iter_json(path):
        count += 1
        when = _ts(obj.get("timestamp"))
        if when is None:
            continue
        if first is None:
            first = when
        last = when
    return first, last, count


def read_session(session: str, *, redact: bool = False, now: datetime | None = None) -> list[AgentRun]:
    """Every agent dispatched by `session`, newest dispatch last."""
    transcript = session_transcript(session)
    if transcript is None:
        return []

    dispatches: dict[str, dict] = {}   # tool_use_id -> details
    agent_of: dict[str, str] = {}      # tool_use_id -> agent_id

    for obj in _iter_json(transcript):
        when = _ts(obj.get("timestamp"))
        for block in _blocks(obj):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "tool_use" and block.get("name") in _AGENT_TOOLS:
                data = block.get("input") or {}
                dispatches[block.get("id")] = {
                    "label": data.get("description") or "(no description)",
                    "agent_type": data.get("subagent_type"),
                    "model": data.get("model"),
                    "dispatched": when,
                }
            elif block.get("type") == "tool_result":
                use_id = block.get("tool_use_id")
                if use_id not in dispatches:
                    continue
                body = block.get("content")
                body = body if isinstance(body, str) else json.dumps(body)
                found = _AGENT_ID_RE.search(body)
                if found:
                    agent_of[use_id] = found.group(1)

    files = subagent_files()
    reference = now or datetime.now().astimezone()
    runs: list[AgentRun] = []

    for use_id, info in dispatches.items():
        agent_id = agent_of.get(use_id, use_id)
        started = ended = None
        lines = 0
        path = files.get(agent_id)
        if path is not None:
            started, ended, lines = _span(path)

        if ended is None:
            status = "unknown"
        elif (reference - ended).total_seconds() > STALE_AFTER_SECONDS:
            status = "done"
        else:
            # A transcript written to within the window is either still being
            # written or finished moments ago. Nothing in the data distinguishes
            # the two, so this is reported as "running" and allowed to be wrong
            # for at most STALE_AFTER_SECONDS -- claiming a precise end time we
            # cannot observe would be worse.
            status = "running"

        runs.append(
            AgentRun(
                agent_id=agent_id,
                label="(redacted)" if redact else info["label"],
                agent_type=info["agent_type"],
                model=info["model"],
                dispatched=info["dispatched"],
                started=started,
                ended=ended,
                lines=lines,
                status=status,
            )
        )

    runs.sort(key=lambda r: (r.dispatched or r.started or reference))
    return runs


def current_session() -> str | None:
    """The most recently written session transcript."""
    newest = None
    for path in paths.projects_dir().rglob("*.jsonl"):
        if "subagents" in path.parts:
            continue
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest[0]:
            newest = (stamp, path.stem)
    return newest[1] if newest else None


# ---------------------------------------------------------------------------
# Inside one agent's run
# ---------------------------------------------------------------------------

# Tool inputs that name a file, in the order worth trying.
_PATH_KEYS = ("file_path", "path", "notebook_path")


@dataclass
class AgentDetail:
    """What an agent actually did, read from its own transcript.

    Deliberately a summary rather than the transcript itself: the tools it
    reached for, the files it touched, the commands it ran, and the report it
    returned. That is enough to see how a run went without putting every
    prompt and every file body on screen.
    """
    agent_id: str
    tools: list           # (tool name, count), most used first
    files: list           # paths touched, deduplicated
    commands: list        # shell commands, truncated
    report: str           # the agent's final message
    turns: int


def _fingerprint(path: Path) -> str:
    try:
        st = path.stat()
        return f"{st.st_mtime_ns}:{st.st_size}"
    except OSError:
        return "?"


_DETAIL_CACHE: dict[str, "AgentDetail"] = {}


def detail(agent_id: str, *, redact: bool = False, max_items: int = 40) -> AgentDetail | None:
    """Read one agent's transcript. None if it has none (died at launch).

    Cached on the file's mtime+size. A finished agent's transcript never
    changes again, and a fleet view calls this once per agent -- uncached, a
    single request re-read thousands of files and took over a minute, which
    presented in the UI as a panel stuck on "reading" forever.
    """
    path = subagent_files().get(agent_id)
    if path is None:
        return None

    key = f"{path}|{_fingerprint(path)}|{redact}|{max_items}"
    hit = _DETAIL_CACHE.get(key)
    if hit is not None:
        return hit

    tools: Counter = Counter()
    files: list[str] = []
    commands: list[str] = []
    report = ""
    turns = 0

    for obj in _iter_json(path):
        if obj.get("type") == "assistant":
            turns += 1
        for block in _blocks(obj):
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text" and obj.get("type") == "assistant":
                text = block.get("text")
                if isinstance(text, str) and text.strip():
                    report = text
            elif kind == "tool_use":
                name = block.get("name") or "?"
                tools[name] += 1
                data = block.get("input") or {}
                for key in _PATH_KEYS:
                    value = data.get(key)
                    if isinstance(value, str) and value not in files:
                        files.append(value)
                        break
                command = data.get("command")
                if name == "Bash" and isinstance(command, str):
                    commands.append(" ".join(command.split())[:120])

    if redact:
        files = [f"<{len(files)} file(s)>"] if files else []
        commands = [f"<{len(commands)} command(s)>"] if commands else []
        report = "<redacted>" if report else ""

    result = AgentDetail(
        agent_id=agent_id,
        tools=tools.most_common(),
        files=files[:max_items],
        commands=commands[:max_items],
        report=report.strip()[:1200],
        turns=turns,
    )
    _DETAIL_CACHE[key] = result
    return result


def all_sessions() -> list[tuple[str, str]]:
    """(project, session id) for every session transcript, newest first."""
    out = []
    for path in paths.projects_dir().rglob("*.jsonl"):
        if "subagents" in path.parts:
            continue
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        out.append((stamp, path.parent.name, path.stem))
    out.sort(reverse=True)
    return [(project, session) for _, project, session in out]


def fleet(*, redact: bool = False, now: datetime | None = None,
          sessions: int = 25) -> dict[str, list[AgentRun]]:
    """Every agent across every project, grouped by project.

    Walks the most recent `sessions` transcripts rather than all of them --
    there are hundreds on a working machine and the old ones cannot contain
    anything running.
    """
    reference = now or datetime.now().astimezone()
    out: dict[str, list[AgentRun]] = {}
    for project, session in all_sessions()[:sessions]:
        runs = read_session(session, redact=redact, now=reference)
        if runs:
            out.setdefault(project, []).extend(runs)
    return out


# ---------------------------------------------------------------------------
# Orchestration: who dispatched whom, when, and what they touched
# ---------------------------------------------------------------------------

# Reading every subagent transcript on each call is too slow on a real corpus
# (881 files here), so parsed results are cached against file mtime+size.
_SPAN_CACHE: dict[str, tuple] = {}


@dataclass
class ToolEvent:
    at: datetime
    tool: str
    detail: str


@dataclass
class Orchestration:
    """The dispatch tree, flattened, plus what it touched.

    agent-flow reconstructs branching from a live hook stream. This gets the
    same structure from the transcripts already on disk, which means it works
    retroactively and costs nothing per tool call: an agent that dispatched
    sub-agents records their `agentId` in its OWN transcript, exactly as the
    main session records the agents it dispatched.
    """
    parent_of: dict          # child agent_id -> parent agent_id ("" = the session)
    children: dict           # agent_id -> [child agent_id]
    depth: dict              # agent_id -> nesting depth, 0 for a direct dispatch
    files: list              # (path, times touched), most touched first
    tools: list              # (tool name, count) across the whole run


def _dispatched_by(path: Path) -> list[str]:
    """agent_ids this transcript dispatched, cached on file identity."""
    key = f"{path}|{_fingerprint(path)}"
    hit = _SPAN_CACHE.get(key)
    if hit is not None:
        return hit
    found: list[str] = []
    for obj in _iter_json(path):
        for block in _blocks(obj):
            if isinstance(block, dict) and block.get("type") == "tool_result":
                body = block.get("content")
                body = body if isinstance(body, str) else json.dumps(body)
                match = _AGENT_ID_RE.search(body)
                if match and match.group(1) not in found:
                    found.append(match.group(1))
    _SPAN_CACHE[key] = found
    return found


def orchestration(session: str, *, redact: bool = False) -> Orchestration:
    """Reconstruct the dispatch tree for one session."""
    files = subagent_files()
    parent_of: dict[str, str] = {}
    children: dict[str, list[str]] = {}

    transcript = session_transcript(session)
    roots = _dispatched_by(transcript) if transcript else []
    for child in roots:
        parent_of[child] = ""
        children.setdefault("", []).append(child)

    # Walk outward from the roots. Bounded by the agents that actually exist,
    # so a malformed id cannot send this into an unbounded search.
    seen, queue = set(roots), list(roots)
    while queue:
        agent_id = queue.pop()
        path = files.get(agent_id)
        if path is None:
            continue
        for child in _dispatched_by(path):
            if child in seen:
                continue          # also breaks any cycle a bad id could create
            seen.add(child)
            parent_of[child] = agent_id
            children.setdefault(agent_id, []).append(child)
            queue.append(child)

    depth: dict[str, int] = {}
    for agent_id in seen:
        d, cursor = 0, agent_id
        while parent_of.get(cursor):
            cursor = parent_of[cursor]
            d += 1
            if d > 20:            # defensive: never loop on malformed data
                break
        depth[agent_id] = d

    file_counts: Counter = Counter()
    tool_counts: Counter = Counter()
    for agent_id in seen:
        info = detail(agent_id, redact=redact)
        if info is None:
            continue
        for name, count in info.tools:
            tool_counts[name] += count
        for path_str in info.files:
            file_counts[path_str] += 1

    return Orchestration(
        parent_of=parent_of,
        children=children,
        depth=depth,
        files=file_counts.most_common(25),
        tools=tool_counts.most_common(),
    )


def tool_timeline(agent_id: str, *, limit: int = 60) -> list[ToolEvent]:
    """Every tool call an agent made, in order. This is what turns a duration
    into a story -- where the time actually went inside a long run."""
    path = subagent_files().get(agent_id)
    if path is None:
        return []
    out: list[ToolEvent] = []
    for obj in _iter_json(path):
        when = _ts(obj.get("timestamp"))
        for block in _blocks(obj):
            if not isinstance(block, dict) or block.get("type") != "tool_use":
                continue
            data = block.get("input") or {}
            hint = ""
            for key in ("file_path", "path", "pattern", "query", "command", "description"):
                value = data.get(key)
                if isinstance(value, str):
                    hint = " ".join(value.split())[:70]
                    break
            if when:
                out.append(ToolEvent(at=when, tool=block.get("name") or "?", detail=hint))
    return out[:limit]
