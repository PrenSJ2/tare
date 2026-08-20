"""What the harness learns from being used.

Every other memory system for coding agents remembers *facts* -- project
decisions, conversation history, code patterns. Surveyed in August 2026, none
of them tie memory to a capability graph, and all of them report the same
unsolved problem: the store accretes, nothing prunes, and stale facts sit
beside current ones with no signal which is which.

This remembers **use**, which is a different thing and rots differently. A
lookup that happened is permanently true. An activation that happened is
permanently true. They lose relevance with age rather than becoming wrong, so
decay is sufficient where fact-memory needs contradiction detection nobody has
made work.

Three signals, all previously thrown away:

    lookup      a search, and what it returned
    miss        a search that surfaced nothing relevant -- a capability gap
    activation  a shelved capability pulled back -- a shelving decision that
                was wrong

## These rows are durable, unlike `invocation`

`mine` rebuilds `kind='invocation'` from transcripts on every run, and Claude
Code expires those transcripts on its own schedule -- so invocation history is
a cache with a horizon. The kinds here are written once and never rebuilt.
`mine`'s delete is scoped to `kind = 'invocation'` precisely so this is safe;
do not widen it.

## Why this does not feed back automatically (yet)

`suggestions()` reports what the record implies -- capabilities to stop
shelving, gaps worth filling -- and stops there. Letting usage silently
re-rank search results would make the ranking unexplainable the first time it
surprised someone, and the same restraint applies here as to the swarm
monitor: watch what it would have done before letting it do it.
"""

from __future__ import annotations

import json
import math
import sqlite3
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# A relevance SCORE cannot tell a good answer from a bad one here, and the
# first version of this file assumed it could. Measured against the real index:
#
#   good  8.25 ai-engineer    8.53 content-marketer   12.53   19.08   45.17
#   bad   4.41 popups         6.71 remotion           8.36 systematic-debugging
#
# "quantum error correction" returns systematic-debugging at 8.36, between two
# genuinely good matches. The distributions overlap, so any threshold either
# discards real hits or accepts nonsense.
#
# So a miss is only recorded when the search returned NOTHING AT ALL, which is
# reliable ("underwater basket weaving" correctly returns none), and the real
# signal for "this search did not help" is behavioural: see UNHELPFUL_AFTER.
HELPED_WITHIN_SECONDS = 600

# A query asked this many times where nothing was ever activated afterwards is
# a capability gap -- the operator kept looking for something that is not here.
GAP_THRESHOLD = 2

# Half-life for weighting a signal by age, in days. Matches the decay already
# used for usage buckets so the two tell a consistent story.
HALF_LIFE_DAYS = 30.0




@dataclass
class Suggestion:
    kind: str          # "unshelve" | "gap" | "wasted"
    subject: str
    detail: str
    evidence: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def current_project() -> str:
    """The project this command is running in, keyed the way Claude Code keys
    its transcript directories, so harness's own events and mined invocations
    land in the same namespace.

    One machine's projects are not interchangeable: on this corpus `homelab`
    leans hard on code-reviewer (65 uses) while `api-service` barely touches
    it. Averaging them produces a picture that is true of no project.
    """
    return "-" + str(Path.cwd()).replace("/", "-").lstrip("-")


def _record(conn: sqlite3.Connection, kind: str, node_id: str | None, payload: dict) -> None:
    payload = {**payload, "project": current_project()}
    conn.execute(
        "INSERT INTO events (ts, kind, node_id, payload) VALUES (?, ?, ?, ?)",
        (_now(), kind, node_id, json.dumps(payload, ensure_ascii=False)),
    )
    conn.commit()


def record_lookup(conn: sqlite3.Connection, query: str, results: list) -> None:
    """One search. `results` are whatever `lookup.lookup` returned.

    A search that surfaced nothing relevant is recorded as a `miss` instead --
    that is the signal worth having, because it names something the operator
    wanted and the machine could not offer.
    """
    query = (query or "").strip()
    if not query:
        return

    if not results:
        # Nothing at all came back. This is the one unambiguous miss.
        _record(conn, "miss", None, {"query": query})
        return

    top = results[0]
    _record(conn, "lookup", getattr(top, "id", None), {
        "query": query,
        "top": getattr(top, "name", None),
        "state": getattr(top, "state", None),
        "returned": [getattr(r, "name", None) for r in results[:5]],
    })


def record_activation(conn: sqlite3.Connection, node_id: str, name: str, *, was: str | None) -> None:
    """A capability pulled back onto the load path.

    `was` is the state it was in. An activation from 'vaulted' is the
    interesting one: it says the shelving decision was wrong.
    """
    _record(conn, "activation", node_id, {"name": name, "was": was})


def _parse(ts: str | None) -> datetime | None:
    try:
        when = datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _decayed(rows: list[str], now: datetime) -> float:
    """Sum of exp(-age/half-life) over timestamps -- recent signals count more."""
    total = 0.0
    for ts in rows:
        try:
            when = datetime.fromisoformat(ts)
        except (TypeError, ValueError):
            continue
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        age_days = max(0.0, (now - when).total_seconds() / 86400)
        total += math.exp(-age_days / HALF_LIFE_DAYS)
    return total


# Where a project records how its tools are configured. harness points at
# these; it deliberately does not copy them. Order matters -- the first hit is
# reported as the primary place to look.
NOTE_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md", ".agents/product-marketing.md")


def resolve_project(key: str) -> Path | None:
    """Turn a transcript-directory key back into a real path, or None.

    Claude Code builds these keys by replacing "/" with "-", which is lossy:
    `-Users-you-Documents-Code-Some-Project--claude` could decode several ways
    because real directory names contain hyphens too. So candidates are walked
    against the filesystem, greedily preferring longer components, and EVERY
    part of the key must be consumed.

    Returning None is the correct answer for a project that has been moved or
    deleted. An earlier version returned the nearest existing ancestor, so an
    unknown key resolved to `/Users/you` and would have pointed the operator
    at a stranger's notes -- exactly the invented path this is meant to avoid.
    """
    parts = [p for p in key.lstrip("-").split("-") if p]
    if not parts:
        return None

    def walk(base: Path, remaining: list[str]) -> Path | None:
        if not remaining:
            return base
        # Longest component first: "Some-Project" must beat "Some".
        for take in range(len(remaining), 0, -1):
            nxt = base / "-".join(remaining[:take])
            if nxt.is_dir():
                found = walk(nxt, remaining[take:])
                if found is not None:
                    return found
        return None

    return walk(Path("/"), parts)


def project_notes(root: Path) -> list[tuple[str, int]]:
    """Which files in this project describe how its tools are configured.

    This is the pointer half of the second brain. "Use this browser profile
    with that extension here" is a fact about a project, and facts belong in
    the project's own files where they sit beside the code they describe and
    go stale visibly. Copying them into this database would make a third
    source of truth, and every memory system surveyed reports that the copy is
    what rots.
    """
    found = []
    for name in NOTE_FILES:
        path = root / name
        try:
            if path.is_file():
                found.append((name, len(path.read_text(encoding="utf-8", errors="replace").splitlines())))
        except OSError:
            continue
    return found


def by_project(conn: sqlite3.Connection, limit: int = 6) -> dict[str, list[tuple[str, int]]]:
    """Which capabilities each project actually leans on.

    Read from the mined `invocation` events, which carry the project they
    happened in. This is the "what do I use *here*" half of a second brain;
    the "how is it configured here" half belongs in that project's own
    CLAUDE.md, not in this database.
    """
    counts: dict[str, Counter] = defaultdict(Counter)
    for row in conn.execute("SELECT node_id, payload FROM events WHERE kind = 'invocation'"):
        try:
            data = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if not isinstance(data, dict):
            continue  # older rows stored a bare name string
        project, name = data.get("project"), data.get("name")
        if project and name:
            counts[project][name] += 1
    return {p: c.most_common(limit) for p, c in counts.items()}


def suggestions(conn: sqlite3.Connection, *, now: datetime | None = None,
                project: str | None = None) -> list[Suggestion]:
    """What the usage record implies. Reports only; changes nothing.

    `project` narrows every signal to one project, so a gap in one codebase is
    not diluted by unrelated work in another.
    """
    reference = now or datetime.now(timezone.utc)
    out: list[Suggestion] = []

    # 1. Shelved, then pulled back. The shelving decision was wrong, and if it
    #    keeps happening the capability should stop being a candidate at all.
    pulled: dict[str, list[str]] = defaultdict(list)
    names: dict[str, str] = {}
    for row in conn.execute(
        "SELECT ts, node_id, payload FROM events WHERE kind = 'activation' AND node_id IS NOT NULL"
    ):
        try:
            data = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            data = {}
        if project and data.get("project") != project:
            continue
        if data.get("was") != "vaulted":
            continue
        pulled[row["node_id"]].append(row["ts"])
        names[row["node_id"]] = data.get("name") or row["node_id"]

    for node_id, stamps in sorted(pulled.items(), key=lambda kv: -len(kv[1])):
        weight = _decayed(stamps, reference)
        out.append(Suggestion(
            kind="unshelve",
            subject=names[node_id],
            detail=f"activated {len(stamps)}x after being shelved -- it is not "
                   f"never-invoked, the usage signal just could not see it",
            evidence=[f"activated {s[:19]}" for s in stamps[-4:]] +
                     [f"decayed weight {weight:.2f}"],
        ))

    # 2. The same thing searched for repeatedly with nothing to show. This is
    #    the only place the tool learns what the operator wanted and did NOT
    #    have -- every other signal is about what already exists.
    misses = Counter()
    for row in conn.execute("SELECT payload FROM events WHERE kind = 'miss'"):
        try:
            data = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if project and data.get("project") != project:
            continue
        query = data.get("query")
        if query:
            misses[query.strip().lower()] += 1

    # A search that returned plausible-looking results but never led to an
    # activation is the behavioural version of a miss -- and the only reliable
    # one, since the scores cannot be thresholded (see the note at the top).
    activations = sorted(
        r["ts"] for r in conn.execute("SELECT ts FROM events WHERE kind = 'activation'")
    )
    asked: Counter = Counter()
    helped: set[str] = set()
    for row in conn.execute("SELECT ts, payload FROM events WHERE kind = 'lookup'"):
        try:
            data = json.loads(row["payload"] or "{}")
        except json.JSONDecodeError:
            continue
        if project and data.get("project") != project:
            continue
        query = (data.get("query") or "").strip().lower()
        if not query:
            continue
        asked[query] += 1
        when = _parse(row["ts"])
        if when is None:
            continue
        for stamp in activations:
            other = _parse(stamp)
            if other is None:
                continue
            gap = (other - when).total_seconds()
            if 0 <= gap <= HELPED_WITHIN_SECONDS:
                helped.add(query)
                break

    for query, count in asked.items():
        if count >= GAP_THRESHOLD and query not in helped:
            misses[query] += 0  # surface it alongside the hard misses

    for query, count in misses.most_common():
        if count < GAP_THRESHOLD and asked.get(query, 0) < GAP_THRESHOLD:
            continue
        out.append(Suggestion(
            kind="gap",
            subject=query,
            detail=(f"searched {max(count, asked.get(query, 0))}x and never led to using "
                    f"anything -- likely nothing here covers it"),
            evidence=([f"{count} searches returned nothing at all"] if count else []) +
                     ([f"{asked[query]} searches returned results but none was activated"]
                      if asked.get(query) else []),
        ))

    # 3. Shelved capabilities nobody has ever looked for. Not a problem -- it
    #    is the vault working -- but it is the evidence that shelving them was
    #    right, and worth saying so where the operator can see it.
    shelved = conn.execute(
        "SELECT COUNT(*) c FROM nodes WHERE state = 'vaulted'"
    ).fetchone()["c"]
    if shelved and not pulled:
        out.append(Suggestion(
            kind="wasted",
            subject=f"{shelved} shelved capabilities",
            detail="none has been pulled back since it was shelved -- the vault is holding",
            evidence=[f"{len(misses)} distinct searches found nothing"] if misses else [],
        ))

    return out


def render(items: list[Suggestion]) -> str:
    if not items:
        return "nothing learned yet -- run `harness lookup` a few times and check back"
    lines = [f"{len(items)} thing(s) learned from use"]
    for item in items:
        lines.append("")
        lines.append(f"[{item.kind}] {item.subject}")
        lines.append(f"    {item.detail}")
        for note in item.evidence[:4]:
            lines.append(f"      - {note}")
    return "\n".join(lines)
