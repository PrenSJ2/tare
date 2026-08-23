"""Findings: what has been learned, as opposed to what can be run.

`lookup` answers "what capability does X". This answers "what do I already know
about X" -- a claim about how something behaves, learned the hard way, usually
at some cost.

## Why this is a separate index from capabilities

They are different questions and mixing them muddies both. A search for "docker"
should not rank a skill that mentions Docker against a finding that
`restart: unless-stopped` does not mean start-on-boot; one is a thing you might
invoke, the other is something you need to know before you act. Separate tables,
separate command, same FTS5 machinery.

## Scope is what makes it cross-project

A per-directory CLAUDE.md cannot say "this is true everywhere". That is the one
thing findings add:

- `universal` -- surfaces in every project
- `tool`      -- surfaces wherever that tool is in play
- `project`   -- surfaces only in the project it belongs to

So a finding about macOS caching negative DNS answers, learned while fixing a
camera network, is available while debugging something else entirely. That was
the whole reason to lift these out of the project file.

## Superseded findings are kept, not deleted

`superseded_by` points at whatever replaced a finding. The old one stays
searchable and is reported as superseded, because "we used to believe X, and
here is why we stopped" is the part that stops you re-deriving it. This is the
same job Zep's bi-temporal edges do, in a file you can read.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import paths

FRONTMATTER = re.compile(r"^---\n(.*?)\n---\n(.*)$", re.S)
WIKILINK = re.compile(r"\[\[([a-z0-9-]+)\]\]")

# Only these are recognised. An unknown scope is treated as `project`, the
# narrowest, so a typo hides a finding from other projects rather than leaking
# a project detail into all of them.
SCOPES = ("universal", "tool", "project")

# How much a finding from another project is discounted. Enough that local
# knowledge wins a tie, not enough to bury a far better match.
OUT_OF_SCOPE_WEIGHT = 0.7

# A superseded finding still answers "we used to believe this", so it is
# returned -- but must never outrank whatever replaced it.
SUPERSEDED_WEIGHT = 0.4


def brain_dir() -> Path:
    return paths.claude_home() / "brain" / "findings"


@dataclass
class Finding:
    name: str
    scope: str
    body: str
    summary: str
    confidence: str = "verified"
    tools: list[str] = field(default_factory=list)
    projects: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    superseded_by: str | None = None
    links: list[str] = field(default_factory=list)
    path: str = ""


def _scalar(front: str, key: str) -> str:
    found = re.search(rf"^{key}:\s*(.*)$", front, re.M)
    return found.group(1).strip() if found else ""


def _list(front: str, key: str) -> list[str]:
    raw = _scalar(front, key)
    if not raw or raw in ("[]", "null", "~"):
        return []
    return [p.strip() for p in raw.strip("[]").split(",") if p.strip()]


def parse(path: Path) -> Finding | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    found = FRONTMATTER.match(text)
    if not found:
        # A file without frontmatter is a note somebody dropped in, not a
        # finding. Skipped rather than guessed at.
        return None
    front, body = found.group(1), found.group(2).strip()
    scope = _scalar(front, "scope") or "project"
    superseded = _scalar(front, "superseded_by")
    return Finding(
        name=_scalar(front, "name") or path.stem,
        scope=scope if scope in SCOPES else "project",
        body=body,
        # First paragraph: the claim itself, which is what a result list wants.
        summary=re.sub(r"\s+", " ", body.split("\n\n")[0]).strip(),
        confidence=_scalar(front, "confidence") or "verified",
        tools=_list(front, "tools"),
        projects=_list(front, "projects"),
        tags=_list(front, "tags"),
        superseded_by=None if superseded in ("", "null", "~") else superseded,
        links=sorted(set(WIKILINK.findall(body))),
        path=str(path),
    )


def load() -> list[Finding]:
    directory = brain_dir()
    if not directory.is_dir():
        return []
    out = []
    for path in sorted(directory.glob("*.md")):
        finding = parse(path)
        if finding:
            out.append(finding)
    return out


def reindex(conn) -> int:
    """Rebuild the findings tables from disk. Files are the source of truth."""
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS findings (
            name          TEXT PRIMARY KEY,
            scope         TEXT,
            confidence    TEXT,
            summary       TEXT,
            body          TEXT,
            tools         TEXT,
            projects      TEXT,
            tags          TEXT,
            superseded_by TEXT,
            path          TEXT
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS findings_fts USING fts5(
            name, summary, body, tools, tags, projects
        );
    """)
    conn.execute("DELETE FROM findings")
    conn.execute("DELETE FROM findings_fts")
    found = load()
    for f in found:
        conn.execute(
            "INSERT INTO findings (name,scope,confidence,summary,body,tools,projects,tags,superseded_by,path)"
            " VALUES (?,?,?,?,?,?,?,?,?,?)",
            (f.name, f.scope, f.confidence, f.summary, f.body, ",".join(f.tools),
             ",".join(f.projects), ",".join(f.tags), f.superseded_by, f.path),
        )
        conn.execute(
            "INSERT INTO findings_fts (name,summary,body,tools,tags,projects) VALUES (?,?,?,?,?,?)",
            (f.name.replace("-", " "), f.summary, f.body, " ".join(f.tools),
             " ".join(f.tags), " ".join(f.projects)),
        )
    conn.commit()
    return len(found)


@dataclass
class Recall:
    name: str
    scope: str
    confidence: str
    summary: str
    superseded_by: str | None
    path: str
    score: float
    in_scope_here: bool


def recall(conn, query: str, *, project: str | None = None, limit: int = 5) -> list[Recall]:
    """Findings matching `query`, most relevant first.

    `project` narrows nothing away -- a project-scoped finding from somewhere
    else still appears, flagged as out of scope. Hiding it would mean a search
    that silently withholds something you wrote, and the whole point is to stop
    knowledge being invisible from the next repo along.
    """
    from .lookup import _fts_query  # noqa: PLC0415 - shared sanitiser, one owner

    expression = _fts_query(query)
    if not expression:
        return []
    try:
        rows = conn.execute(
            "SELECT f.name, f.scope, f.confidence, f.summary, f.superseded_by, f.path,"
            "       bm25(findings_fts) AS raw"
            "  FROM findings_fts JOIN findings f"
            "    ON f.name = REPLACE(findings_fts.name, ' ', '-')"
            " WHERE findings_fts MATCH ? ORDER BY raw LIMIT ?",
            (expression, limit * 3),
        ).fetchall()
    except Exception:
        return []

    out = []
    for row in rows:
        # A superseded finding is still worth returning -- "we used to believe
        # this" is the part that stops a re-derivation -- but it must never
        # outrank what replaced it.
        score = -row["raw"] * (SUPERSEDED_WEIGHT if row["superseded_by"] else 1.0)
        in_scope = (
            row["scope"] == "universal"
            or row["scope"] == "tool"
            or project is None
            or project in (row["path"] or "")
            or _belongs(conn, row["name"], project)
        )
        # Out of scope is a WEIGHT, not a sort key.
        #
        # Sorting on it demoted every project-scoped finding below every
        # universal one, and `limit` then removed them entirely -- a search
        # that silently withheld something you had written, which is the exact
        # failure this is supposed to fix. Asking "is bandwidth expensive here"
        # from another directory returned three unrelated findings and neither
        # of the two about bandwidth.
        if not in_scope:
            score *= OUT_OF_SCOPE_WEIGHT
        out.append(Recall(
            name=row["name"], scope=row["scope"], confidence=row["confidence"],
            summary=row["summary"], superseded_by=row["superseded_by"],
            path=row["path"], score=score, in_scope_here=in_scope,
        ))
    out.sort(key=lambda r: -r.score)
    return out[:limit]


def _belongs(conn, name: str, project: str) -> bool:
    row = conn.execute("SELECT projects FROM findings WHERE name = ?", (name,)).fetchone()
    return bool(row) and project in (row["projects"] or "").split(",")


def render(results: list[Recall]) -> str:
    if not results:
        return "nothing known about that yet"
    lines = []
    for r in results:
        marks = []
        if r.confidence != "verified":
            marks.append(r.confidence)
        if r.superseded_by:
            marks.append(f"superseded by {r.superseded_by}")
        if not r.in_scope_here:
            marks.append("another project")
        suffix = f"   [{', '.join(marks)}]" if marks else ""
        lines.append(f"\n{r.name}  ({r.scope}){suffix}")
        lines.append(f"  {r.summary[:200]}")
        lines.append(f"  {r.path.replace(str(paths.claude_home()), '~/.claude')}")
    return "\n".join(lines)
