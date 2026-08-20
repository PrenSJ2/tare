""""What do I already have for this job?" -- ranked search over the graph.

This is what makes shelving survivable. A capability that gets shelved keeps
its node with `state='vaulted'`; `lookup` is the only thing standing between
that row and it being effectively deleted from the operator's mental model.
So `lookup` carries no state filter anywhere -- see `lookup()` below -- and
every result is labelled with its state so the operator can tell "have it,
loaded" from "have it, vaulted -- run `harness activate`" apart.

Ranking is BM25 (via nodes_fts, FTS5's built-in ranking function) with a
usage prior layered on top. The prior is MULTIPLICATIVE, not additive, and
that is load-bearing, not a style choice:

A previous build computed `score = bm25 + USAGE_PRIOR_WEIGHT * log1p(invocations)`.
Measured on a real query against a small fixture, `bm25(nodes_fts)` came back
around 5e-6 (FTS5's bm25() magnitude is tiny -- see the docstring on
`lookup()`) while `log1p(35)` (a heavily-used node) is about 3.58 -- six
orders of magnitude apart. Text relevance was entirely swamped; every query,
regardless of what it asked for, returned whatever capability had the most
invocations. `score = base * (1.0 + USAGE_PRIOR_WEIGHT * log1p(invocations))`
instead lets usage nudge the ranking -- a heavily-used node with comparable
text relevance to a rival edges it out -- without a wildly different-scale
term overwhelming the thing actually being searched for.

`USAGE_PRIOR_WEIGHT` was tuned by hand against one fixture, not against real
corpus statistics. It is a reversible ranking constant, not a measured
quantity -- change it freely if it misranks on a real corpus, but don't
mistake the current value for anything more than a starting guess.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

# See module docstring: this scales a usage-recency signal into the ranking,
# multiplicatively against the BM25 base so it nudges rather than swamps.
# Tuned against one fixture, not real corpus statistics -- a reversible
# ranking constant, safe to retune once more query/result pairs exist.
USAGE_PRIOR_WEIGHT = 0.5

# How many relationship chains to attach per result. Kept small -- this is
# "why did this match and what does it work with", not a full edge dump.
CHAIN_LIMIT = 5

_TOKEN_RE = re.compile(r"[A-Za-z0-9]+")


@dataclass
class Result:
    """One ranked lookup hit. `state` is always present and never filtered
    on -- a vaulted capability is exactly as findable as a live one."""

    id: str
    name: str
    kind: str
    state: str
    purpose_line: str
    when_to_use: str
    invocations: int
    score: float
    chains: list[str] = field(default_factory=list)


def reindex(conn) -> int:
    """Rebuild `nodes_fts` from `nodes`. Idempotent: clears the index first,
    then repopulates from the current table, so running this twice back to
    back leaves the same rows rather than piling up duplicates.

    Rows are inserted with an explicit rowid equal to `nodes.rowid` --
    `nodes_fts` has no id column of its own (the schema in db.py is fixed
    and shared with every other module, so it can't grow one here), and
    `nodes.id` is TEXT so it does not alias the table's implicit rowid the
    way an INTEGER PRIMARY KEY would. Matching on the underlying rowid is
    what lets `lookup()` join a search hit back to the node it came from.
    """
    conn.execute("DELETE FROM nodes_fts")
    rows = conn.execute(
        "SELECT rowid AS rid, name, purpose_line, when_to_use, tags, desc_raw FROM nodes"
    ).fetchall()
    conn.executemany(
        "INSERT INTO nodes_fts (rowid, name, purpose_line, when_to_use, tags, desc_raw) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        [
            (
                row["rid"],
                row["name"] or "",
                row["purpose_line"] or "",
                row["when_to_use"] or "",
                row["tags"] or "",
                row["desc_raw"] or "",
            )
            for row in rows
        ],
    )
    conn.commit()
    return len(rows)


def _fts_query(text: str) -> str:
    """Turn free-typed operator text into a safe FTS5 MATCH expression.

    Every token is quoted as an FTS5 string literal -- not just to escape
    punctuation, but so a query that happens to contain a bare "AND", "OR",
    "NOT" or "NEAR" is searched for as literal text instead of silently
    being reinterpreted as a boolean operator. Tokens are OR'd together
    rather than the FTS5 default (AND): a query like "optimize rust
    performance" must still surface a capability whose text matches only
    "rust" and "performance", not require every single word to appear.
    BM25 already ranks a document matching more of the query terms higher,
    so OR does not flatten relevance -- it just stops a single non-matching
    word from excluding an otherwise-strong hit.
    """
    tokens = _TOKEN_RE.findall(text.lower())
    if not tokens:
        return ""
    return " OR ".join(f'"{t}"' for t in tokens)


def _name_of(conn, node_id: str) -> str | None:
    row = conn.execute("SELECT name FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row["name"] if row else None


def _chains_for(conn, node_id: str, node_name: str) -> list[str]:
    """Relationship chains touching this node, rendered as
    `X --(type)--> Y` -- how an operator sees why something matched and
    what it works with. Both directions are included (this node as src and
    as dst), each formatted as pretty an edge in the direction it was
    actually recorded, not forced to read left-to-right from the matched
    node.
    """
    rows = conn.execute(
        "SELECT src, dst, type FROM edges WHERE src = ? OR dst = ? ORDER BY type, dst, src",
        (node_id, node_id),
    ).fetchall()

    chains = []
    for row in rows[:CHAIN_LIMIT]:
        if row["src"] == node_id:
            other_name = _name_of(conn, row["dst"]) or row["dst"]
            chains.append(f"{node_name} --({row['type']})--> {other_name}")
        else:
            other_name = _name_of(conn, row["src"]) or row["src"]
            chains.append(f"{other_name} --({row['type']})--> {node_name}")
    return chains


def lookup(conn, query: str, limit: int = 5) -> list[Result]:
    """Ranked search over every capability -- live and vaulted alike.

    Deliberately NO `state` filter (see module docstring): the whole point
    of the vault is that shelving drops the always-loaded cost without
    dropping findability, so a vaulted node must be exactly as reachable
    here as a live one, just labelled `state='vaulted'` for the operator to
    act on.

    Ranking: BM25 from `nodes_fts` (rebuilt by `reindex()` -- this function
    does not reindex for you; call it after any change to `nodes`), then
    multiplied by a usage prior. `bm25(nodes_fts)` returns a value where
    *more negative* means a *better* match (an FTS5 convention, not a bug),
    so it's negated first to get an ordinary higher-is-better base score.
    """
    match = _fts_query(query)
    if not match:
        return []

    rows = conn.execute(
        """
        SELECT n.id AS id, n.kind AS kind, n.name AS name, n.state AS state,
               n.purpose_line AS purpose_line, n.when_to_use AS when_to_use,
               COALESCE(u.invocations, 0) AS invocations,
               bm25(nodes_fts) AS raw_bm25
        FROM nodes_fts
        JOIN nodes n ON n.rowid = nodes_fts.rowid
        LEFT JOIN usage u ON u.node_id = n.id
        WHERE nodes_fts MATCH ?
        """,
        (match,),
    ).fetchall()

    scored = []
    for row in rows:
        base = -row["raw_bm25"]  # flip FTS5's more-negative-is-better convention
        usage_prior = 1.0 + USAGE_PRIOR_WEIGHT * math.log1p(row["invocations"])
        scored.append((base * usage_prior, row))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    results = []
    for score, row in scored[:limit]:
        results.append(
            Result(
                id=row["id"],
                name=row["name"],
                kind=row["kind"],
                state=row["state"],
                purpose_line=row["purpose_line"] or "",
                when_to_use=row["when_to_use"] or "",
                invocations=row["invocations"],
                score=score,
                chains=_chains_for(conn, row["id"], row["name"]),
            )
        )
    return results
