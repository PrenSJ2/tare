"""The SQLite graph.

Schema recovered verbatim from a live database, so it is known-good against the
real corpus rather than re-derived.

Two rules that earlier defects were traced to, both load-bearing:

- Do NOT set `isolation_level=None`. Callers rely on explicit `conn.commit()`
  for delete-then-repopulate to be atomic; autocommit turns every commit into a
  no-op and makes those sequences non-atomic.
- `events` rows of kind 'invocation' are a REBUILT CACHE -- `mine` deletes and
  re-derives them on every run from transcripts that Claude Code expires on its
  own schedule. Anything that needs durable history must write its own rows of
  a different kind, which `mine` never touches.
"""

from __future__ import annotations

import sqlite3

from . import paths

SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id              TEXT PRIMARY KEY,
    kind            TEXT NOT NULL,
    name            TEXT NOT NULL,
    path            TEXT,
    origin          TEXT,
    provider_plugin TEXT,
    marketplace     TEXT,
    upstream_ref    TEXT,
    update_command  TEXT,
    desc_raw        TEXT DEFAULT '',
    purpose_line    TEXT DEFAULT '',
    when_to_use     TEXT DEFAULT '',
    tags            TEXT DEFAULT '',
    est_tokens      INTEGER DEFAULT 0,
    bucket          TEXT,
    state           TEXT DEFAULT 'live',
    tag_source      TEXT,
    content_hash    TEXT,
    parse_error     TEXT
);

CREATE TABLE IF NOT EXISTS edges (
    src      TEXT NOT NULL,
    dst      TEXT NOT NULL,
    type     TEXT NOT NULL,
    weight   REAL DEFAULT 1.0,
    evidence TEXT,
    PRIMARY KEY (src, dst, type)
);

CREATE TABLE IF NOT EXISTS usage (
    node_id     TEXT PRIMARY KEY,
    invocations INTEGER DEFAULT 0,
    sessions    INTEGER DEFAULT 0,
    last_used   TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    kind    TEXT NOT NULL,
    node_id TEXT,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS tag_cache (
    content_hash TEXT PRIMARY KEY,
    purpose_line TEXT,
    when_to_use  TEXT,
    tags         TEXT
);

CREATE INDEX IF NOT EXISTS idx_edges_src   ON edges(src);
CREATE INDEX IF NOT EXISTS idx_edges_dst   ON edges(dst);
CREATE INDEX IF NOT EXISTS idx_events_node ON events(node_id);

CREATE VIRTUAL TABLE IF NOT EXISTS nodes_fts USING fts5(
    name, purpose_line, when_to_use, tags, desc_raw
);
"""


def connect() -> sqlite3.Connection:
    """Open (creating if needed) the graph database."""
    path = paths.db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.commit()
    return conn
