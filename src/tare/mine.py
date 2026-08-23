"""Mine Claude Code's own session transcripts for capability usage.

This is the signal everything downstream depends on: what ranks highly in
`lookup`, what `shelve` is allowed to shelve, and what the reachability guard
protects. Getting it wrong doesn't crash anything -- it just quietly lies.

`usage` and the `invocation` events are a REBUILT CACHE, exactly like db.py's
module docstring says: `mine` deletes and re-derives both on every run, inside
the connection's existing transaction, with one `conn.commit()` at the end.
Do NOT set `isolation_level=None` here or anywhere else -- that would turn the
delete-then-repopulate into two separate autocommitted statements, so a crash
between them would leave `usage` empty with the old events still deleted (or
vice versa). Anything that must survive a run where mining fails outright, or
where a capability's transcripts have simply aged out of Claude Code's own
retention, must be a row of some OTHER `events.kind` -- `mine` only ever
touches `kind = 'invocation'`.

Transcripts are JSONL, one object per line, and they are being written by live
sessions while this reads them -- including the current one. Two consequences:
a trailing line can be a truncated partial write (invalid JSON, not a bug),
and `open()` can raise OSError for reasons that have nothing to do with the
content (permissions, a file removed mid-scan). Every failure mode below is
counted, never silently swallowed, so an operator told "905 invocations" can
also be told what was dropped and why.

Known limitation: `tare tag` shells out to `claude -p`, which writes a
transcript per call into this exact corpus -- there is no way to stop that
short of not shelling out. Those transcripts are excluded by a structural
signature: a `type: "user"` message whose text opens with TAG_PROMPT_SIGNATURE,
in a session that contains no assistant `tool_use` at all (a tagging call
only ever gets a text reply). This still has a false-positive edge: a genuine
session whose first message happens to open with that exact line, and in
which the assistant never once calls a tool, would also be excluded. That
trade was made deliberately -- a bare substring test over the whole file, the
previous version's approach, misses nothing but also excludes any session
that merely *mentions* the line anywhere, e.g. while debugging this file.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from . import paths

# The opening line of the prompt `tag.py` sends to `claude -p`. Kept here,
# not duplicated in tag.py, because a drift between the two would silently
# stop exclusion from working -- tag.py must import this constant rather than
# hardcode its own copy of the string.
TAG_PROMPT_SIGNATURE = (
    "You are tagging a single Claude Code capability for a local search index."
)

# The same problem, one caller further on: `harvest` also shells out to
# `claude -p`, so it also writes a transcript into the corpus this reads. Every
# such prompt must open with one of these, or tare starts mining its own
# exhaust and the usage signal drifts upward with every run.
HARVEST_PROMPT_SIGNATURE = (
    "You are extracting a durable engineering finding for a local knowledge base."
)

OWN_PROMPT_SIGNATURES = (TAG_PROMPT_SIGNATURE, HARVEST_PROMPT_SIGNATURE)

# Tool names that record a capability invocation, and which `input` key on
# that tool_use block carries the invoked name. "Task" is included alongside
# "Agent" because different Claude Code builds have used both names for the
# same sub-agent-launching tool; either can appear in a real corpus.
_AGENT_TOOL_NAMES = ("Agent", "Task")


@dataclass
class MineResult:
    transcripts: int
    invocations: int
    unmatched: int  # DISTINCT names that matched no installed capability
    malformed: int  # lines that were not valid JSON
    unreadable: int  # files that could not be opened at all
    excluded: int  # tare's own tagging exhaust
    unmatched_names: tuple = ()  # the distinct names themselves, for reporting


def mine(conn) -> MineResult:
    """Rebuild `usage` and `kind='invocation'` events from live transcripts."""
    skill_ids, agent_ids = _installed_names(conn)

    transcripts = 0
    invocations = 0
    unmatched_names: set[str] = set()
    malformed = 0
    unreadable = 0
    excluded = 0

    # Accumulated in memory first, written to the DB only once at the end --
    # see the module docstring on why the delete and the repopulate must be
    # one transaction rather than interleaved with the file scan.
    usage_acc: dict[str, dict] = {}
    event_rows: list[tuple[str, str, str, str]] = []
    edit_targets = _edit_targets(conn)
    edit_rows: list[tuple[str, str, str]] = []

    for path in _transcript_paths():
        transcripts += 1
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                raw_lines = fh.readlines()
        except OSError:
            # Written by a live session; can vanish or become unreadable
            # between the directory listing and the open(). Counted, not
            # skipped silently -- an unreadable file is still a fact the
            # operator needs, not a rounding error.
            unreadable += 1
            continue

        parsed = []
        for raw_line in raw_lines:
            line = raw_line.strip()
            if not line:
                continue
            # No substring pre-filter here on purpose. A previous version
            # tested a content substring *before* calling json.loads, which
            # meant every line that passed the substring test also happened
            # to be valid JSON -- `malformed` was dead code that never fired.
            # json.loads is the only thing allowed to decide "not valid JSON".
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                malformed += 1
                continue
            if not isinstance(obj, dict):
                # Valid JSON (e.g. a bare number or array) but not a
                # transcript entry shape. Not malformed -- it parsed fine --
                # just not something with a "type" to dispatch on.
                continue
            parsed.append(obj)

        if _is_tag_exhaust(parsed):
            excluded += 1
            continue

        session_id = str(path)
        # Claude Code stores each project's transcripts under a directory named
        # after its path, so the project a capability was used in is already
        # encoded in the corpus -- it just has to be carried through. Without
        # it, one machine's very different projects average into a single
        # meaningless usage picture.
        project = _project_of(path)
        for obj in parsed:
            for kind_hint, name, ts in _invocations_in(obj):
                invocations += 1
                node_id = (skill_ids if kind_hint == "skill" else agent_ids).get(name)
                if node_id is None:
                    # Matches nothing currently installed -- reported, not
                    # dropped, because the capability may have been uninstalled
                    # since, or be a BUILT-IN agent type (general-purpose,
                    # Explore, fork) which is not a ~/.claude capability at all.
                    #
                    # Counted as DISTINCT names, not occurrences: the real
                    # corpus reports 844 occurrences of about six built-in
                    # names, and printing 844 reads as a matching failure
                    # rather than the handful of expected names it really is.
                    unmatched_names.add(name)
                    continue
                acc = usage_acc.setdefault(
                    node_id, {"invocations": 0, "sessions": set(), "last_used": ""}
                )
                acc["invocations"] += 1
                acc["sessions"].add(session_id)
                if ts and ts > acc["last_used"]:
                    acc["last_used"] = ts
                event_rows.append((ts, node_id, name, project))
            # Same parsed line, second question: did this turn CHANGE a
            # capability rather than call one? Folded into the existing walk
            # because a second pass would mean re-reading 1,588 transcripts to
            # learn something already in hand.
            for file_path, tool, ts in _edits_in(obj):
                fs_name = _fs_name(file_path)
                node_id = edit_targets.get(fs_name) if fs_name else None
                if node_id is None:
                    continue
                edit_rows.append((ts, node_id, json.dumps({
                    "tool": tool, "project": project, "file": file_path,
                    "session": Path(session_id).stem,
                })))

    # Rebuild the cache: delete then repopulate, inside the connection's
    # existing transaction, one commit at the end. Never insert a usage row
    # for a node with zero invocations -- "no row" is how downstream code
    # (LEFT JOIN usage + COALESCE(invocations, 0)) tells a genuinely
    # never-invoked capability apart from one this run simply didn't touch.
    conn.execute("DELETE FROM usage")
    conn.execute("DELETE FROM events WHERE kind = 'invocation'")
    conn.execute("DELETE FROM events WHERE kind = 'edit'")
    for node_id, acc in usage_acc.items():
        conn.execute(
            "INSERT INTO usage (node_id, invocations, sessions, last_used) "
            "VALUES (?, ?, ?, ?)",
            (node_id, acc["invocations"], len(acc["sessions"]), acc["last_used"] or None),
        )
    for ts, node_id, name, project in event_rows:
        conn.execute(
            "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'invocation', ?, ?)",
            (ts, node_id, json.dumps({"name": name, "project": project})),
        )
    for ts, node_id, payload in edit_rows:
        conn.execute(
            "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, 'edit', ?, ?)",
            (ts, node_id, payload),
        )
    conn.commit()

    return MineResult(
        transcripts=transcripts,
        invocations=invocations,
        unmatched=len(unmatched_names),
        unmatched_names=tuple(sorted(unmatched_names)),
        malformed=malformed,
        unreadable=unreadable,
        excluded=excluded,
    )


def _transcript_paths():
    """Every transcript under the corpus, including sub-agent transcripts.

    `projects_dir()` nests a `subagents/*.jsonl` directory per session
    alongside the top-level session file -- both are real transcripts that
    can each carry tool_use invocations, so both are walked.
    """
    return sorted(paths.projects_dir().rglob("*.jsonl"))


def _project_of(path) -> str:
    """The project directory a transcript belongs to.

    `~/.claude/projects/-Users-you-Documents-Code-homelab/<session>.jsonl` belongs
    to the homelab project; subagent transcripts sit one level deeper, so walk up
    until the parent is `projects/` itself.
    """
    current = path.parent
    while current.name in ("subagents",) and current.parent != current:
        current = current.parent
    return current.name


def _installed_names(conn):
    """name -> node id, split by kind so a skill and an agent that happen to
    share a name can never cross-match each other's invocations.

    A plugin skill is invoked in transcripts by its QUALIFIED name --
    `superpowers:brainstorming` -- while the graph stores the leaf name
    (`brainstorming`) with the plugin recorded separately. Matching only the
    bare name silently loses every plugin invocation, which on the real corpus
    meant 10 usage rows instead of 35 and dropped the eight most-used
    capabilities in the configuration. Since `shelve`'s protection guard seeds
    from used nodes, losing them makes it under-protect exactly the suites that
    are used most.

    So each plugin skill is registered under both its leaf name and
    `<plugin>:<leaf>`. The qualified form is registered first and never
    overwritten, so it wins over a same-named user skill; the bare form uses
    setdefault, so a user-authored capability keeps the unqualified name.
    """
    skill_ids: dict[str, str] = {}
    agent_ids: dict[str, str] = {}
    rows = conn.execute("SELECT id, kind, name, provider_plugin, origin FROM nodes").fetchall()

    for row in rows:
        if row["kind"] == "skill" and row["provider_plugin"]:
            skill_ids[f"{row['provider_plugin']}:{row['name']}"] = row["id"]

    for row in rows:
        target = skill_ids if row["kind"] == "skill" else agent_ids if row["kind"] == "agent" else None
        if target is not None:
            target.setdefault(row["name"], row["id"])
    return skill_ids, agent_ids


def _fs_name(file_path: str) -> str | None:
    """The capability's filesystem name for any path inside its directory.

    `.../skills/hyperframes-core/reference.md` and
    `.../vault/skills/hyperframes-core/SKILL.md` both give `hyperframes-core`;
    `.../agents/rust-pro.md` gives `rust-pro`. One rule covers the live tree,
    the vault and a plugin cache, which is what makes an edit recorded against
    the old location still attributable after a move.

    Deliberately the FILESYSTEM name, not the frontmatter name. The two
    diverge, and every previous place that assumed otherwise had to be fixed.
    """
    parts = [p for p in file_path.split("/") if p]
    for i in range(len(parts) - 2, -1, -1):
        if parts[i] in ("skills", "agents") and i + 1 < len(parts):
            leaf = parts[i + 1]
            return leaf[:-3] if leaf.endswith(".md") else leaf
    return None


def _edit_targets(conn) -> dict[str, str]:
    """Filesystem name -> node id, for attributing edits.

    A name held by more than one node is dropped rather than guessed at: a
    misattributed edit history is worse than a short one, because it reads as
    fact.
    """
    seen: dict[str, set[str]] = {}
    for row in conn.execute("SELECT id, path FROM nodes WHERE path IS NOT NULL"):
        name = _fs_name(row["path"])
        if name:
            seen.setdefault(name, set()).add(row["id"])
    return {name: next(iter(ids)) for name, ids in seen.items() if len(ids) == 1}


def _invocations_in(obj):
    """Yield (kind_hint, name, timestamp) for each capability invocation in
    one already-parsed transcript line."""
    if obj.get("type") != "assistant":
        return
    content = obj.get("message", {}).get("content")
    if not isinstance(content, list):
        return
    ts = obj.get("timestamp") or ""
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        tool_name = block.get("name")
        if tool_name == "Skill":
            name = tool_input.get("skill")
            if isinstance(name, str) and name:
                yield "skill", name, ts
        elif tool_name in _AGENT_TOOL_NAMES:
            name = tool_input.get("subagent_type")
            if isinstance(name, str) and name:
                yield "agent", name, ts


# The tools that can change a capability's source. NotebookEdit is here for
# completeness rather than because a skill has ever been a notebook.
_EDIT_TOOL_NAMES = {"Write", "Edit", "MultiEdit", "NotebookEdit"}


def _edits_in(obj):
    """Yield (file_path, tool, timestamp) for each edit to a capability file.

    This is the only retroactive evidence that a session changed a skill. It
    is evidence of *presence*, never of absence: transcripts are deleted,
    rotated and excluded as tagging exhaust, and a capability edited outside a
    session (or in one whose transcript is gone) leaves nothing here. Anything
    built on this must say "no recorded session edit", not "hand-written".
    """
    if obj.get("type") != "assistant":
        return
    content = obj.get("message", {}).get("content")
    if not isinstance(content, list):
        return
    ts = obj.get("timestamp") or ""
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        if block.get("name") not in _EDIT_TOOL_NAMES:
            continue
        tool_input = block.get("input")
        if not isinstance(tool_input, dict):
            continue
        file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if isinstance(file_path, str) and file_path:
            yield file_path, block["name"], ts


def _user_text(obj) -> str:
    """Flatten a user message's content to plain text, whichever shape it's
    in -- a bare string for typed input, or a list of content blocks when the
    turn also carries tool_result blocks alongside real text."""
    content = obj.get("message", {}).get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _is_tag_exhaust(parsed: list[dict]) -> bool:
    """True if this transcript is one of tare's own `claude -p` calls.

    Structural, not a bare substring scan of the raw file: the signature must
    open a `type: "user"` message, AND the session must contain no assistant
    `tool_use` anywhere -- these calls only ever get a text reply back.
    See the module docstring for the known false-positive edge this accepts.

    Checks EVERY signature tare uses, not just tagging's. A new caller that
    shells out and forgets to register its opening line pollutes the usage
    signal silently, which is why the constants live here rather than beside
    each caller.
    """
    has_signature = False
    has_tool_use = False
    for obj in parsed:
        obj_type = obj.get("type")
        if obj_type == "user":
            text = _user_text(obj).strip()
            if any(text.startswith(sig) for sig in OWN_PROMPT_SIGNATURES):
                has_signature = True
        elif obj_type == "assistant":
            content = obj.get("message", {}).get("content")
            if isinstance(content, list) and any(
                isinstance(block, dict) and block.get("type") == "tool_use"
                for block in content
            ):
                has_tool_use = True
    return has_signature and not has_tool_use
