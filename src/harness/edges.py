"""Infer relationships between capabilities into the `edges` table.

Four edge types, and they must never blur into each other:

- `provided-by` -- structural. A plugin skill to its plugin, read straight
  off `provider_plugin`/`marketplace`. No inference involved.
- `routes-to`   -- A dispatches B: A's source text explicitly names B.
- `overlaps`    -- A and B cover similar ground, from description/tag
  similarity. Similarity is NOT dependency.
- `used-with`   -- A and B were invoked in the same session.

Why `routes-to` is the one that matters most: usage is mined from
transcripts, which record capabilities dispatched *by name*. An orchestrator
skill dispatches its sub-skills itself, so those sub-skills never appear by
name in a transcript and read as never-invoked -- precisely because something
else invokes them. A later module (the shelving guard) protects anything
reachable via `routes-to` from a used capability, and that guard is what
stops the tool shelving the sub-skills of a suite the operator uses daily. On
the machine this was built against, `hyperframes` routes to 14 such skills
and `impeccable` to about 15.

That guard walks `routes-to` only. If `overlaps` meant the same thing as
`routes-to`, or if the two were merged, the guard would also protect every
capability that merely resembles a used one -- `code-reviewer` overlaps
`architect-reviewer` without depending on it -- and would over-protect
enormously, defeating the point of shelving at all. So `overlaps` is computed
from a completely different signal (TF-IDF cosine, no reference to source
text) and is never allowed to produce a `routes-to` row, and vice versa.

Standard library only.
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections import Counter, defaultdict
from pathlib import Path

# The four edge types this module owns. `build` deletes exactly these before
# recomputing, so a second run replaces rather than accumulates -- and so it
# never touches edge rows some other module might one day own.
EDGE_TYPES = ("provided-by", "routes-to", "overlaps", "used-with")

# Cosine similarity floor for an `overlaps` edge. Chosen well above "shares a
# few common words" -- two nodes that both mention "review" and "code" should
# not overlap just because those tokens are common across the corpus; TF-IDF
# already discounts that, this is a second margin on top.
OVERLAP_THRESHOLD = 0.15

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def build(conn) -> int:
    """Recompute all four edge types. Returns the number of edges written.

    Idempotent: this always deletes its own edge types first and reinserts
    a freshly computed set, so running it twice back to back produces the
    same rows, not duplicates or a second copy alongside the first.
    """
    edges: list[tuple[str, str, str, float, str]] = []
    edges.extend(_provided_by_edges(conn))
    edges.extend(_routes_to_edges(conn))
    edges.extend(_overlaps_edges(conn))
    edges.extend(_used_with_edges(conn))

    # Delete-then-repopulate, and per db.py this connection is never
    # autocommit, so the delete and the reinsert land in one transaction --
    # a crash mid-build leaves the previous edge set intact rather than half
    # of it gone and half not yet replaced.
    placeholders = ", ".join("?" * len(EDGE_TYPES))
    conn.execute(f"DELETE FROM edges WHERE type IN ({placeholders})", EDGE_TYPES)
    conn.executemany(
        "INSERT INTO edges (src, dst, type, weight, evidence) VALUES (?, ?, ?, ?, ?)",
        edges,
    )
    conn.commit()
    return len(edges)


def _provided_by_edges(conn) -> list[tuple[str, str, str, float, str]]:
    """A plugin-provided skill -> the plugin that provides it.

    Purely structural: the plugin id is built directly from the
    `provider_plugin`/`marketplace` columns already on the skill's own row,
    not inferred from anything. Plugin names repeat across marketplaces, so
    marketplace is folded into the id to keep two same-named plugins from
    different marketplaces from colliding on one dst.
    """
    rows = conn.execute(
        "SELECT id, provider_plugin, marketplace FROM nodes "
        "WHERE kind = 'skill' AND provider_plugin IS NOT NULL AND provider_plugin != ''"
    ).fetchall()
    out = []
    for row in rows:
        dst = _plugin_id(row["provider_plugin"], row["marketplace"])
        out.append((row["id"], dst, "provided-by", 1.0, f"provider_plugin={row['provider_plugin']}"))
    return out


def _plugin_id(provider_plugin: str, marketplace: str | None) -> str:
    return f"plugin:{provider_plugin}@{marketplace}" if marketplace else f"plugin:{provider_plugin}"


def _routes_to_edges(conn) -> list[tuple[str, str, str, float, str]]:
    """A -> B when A's own source file explicitly names B.

    This is the dispatch signal, read from the file on disk at `nodes.path`
    (frontmatter and body both -- whichever section the reference is in).
    Deliberately NOT derived from description similarity: a skill can read
    nothing like another and still dispatch it by name, and two skills can
    read almost identically and never call each other.
    """
    nodes = conn.execute("SELECT id, name, path FROM nodes").fetchall()
    # Only names worth searching for -- an empty name can't be "named".
    targets = [(row["id"], row["name"]) for row in nodes if row["name"]]

    out = []
    for row in nodes:
        text = _read_source(row["path"])
        if not text:
            continue
        for dst_id, dst_name in targets:
            if dst_id == row["id"]:
                continue
            if re.search(rf"\b{re.escape(dst_name)}\b", text):
                out.append((row["id"], dst_id, "routes-to", 1.0, f"names '{dst_name}' in source"))
    return out


def _read_source(path: str | None) -> str:
    if not path:
        return ""
    try:
        return Path(path).read_text()
    except OSError:
        # A node whose file went missing between scan and this build should
        # not crash edge inference for everything else.
        return ""


def _overlaps_edges(conn) -> list[tuple[str, str, str, float, str]]:
    """A <-> B when their purpose/when-to-use/tags text is similar.

    Hand-rolled TF-IDF cosine, standard library only -- no dependency on
    source text (that's routes-to's job) and no dependency on usage.
    Symmetric, so both directions are written.
    """
    rows = conn.execute("SELECT id, purpose_line, when_to_use, tags FROM nodes").fetchall()
    docs = {row["id"]: _tokenize(_overlap_text(row)) for row in rows}
    docs = {node_id: toks for node_id, toks in docs.items() if toks}
    vectors = _tfidf_vectors(docs)

    ids = sorted(vectors)
    out = []
    for i, a in enumerate(ids):
        for b in ids[i + 1 :]:
            sim = _cosine(vectors[a], vectors[b])
            if sim >= OVERLAP_THRESHOLD:
                evidence = f"cosine={sim:.3f}"
                out.append((a, b, "overlaps", sim, evidence))
                out.append((b, a, "overlaps", sim, evidence))
    return out


def _overlap_text(row) -> str:
    return " ".join([row["purpose_line"] or "", row["when_to_use"] or "", row["tags"] or ""])


def _tokenize(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _tfidf_vectors(docs: dict[str, list[str]]) -> dict[str, dict[str, float]]:
    n_docs = len(docs)
    doc_freq: Counter[str] = Counter()
    for tokens in docs.values():
        doc_freq.update(set(tokens))

    vectors: dict[str, dict[str, float]] = {}
    for node_id, tokens in docs.items():
        term_freq = Counter(tokens)
        length = len(tokens)
        vec = {}
        for term, count in term_freq.items():
            # +1 smoothing on both numerator and denominator keeps idf
            # finite even for a term that appears in every single doc.
            idf = math.log((1 + n_docs) / (1 + doc_freq[term])) + 1
            vec[term] = (count / length) * idf
        vectors[node_id] = vec
    return vectors


def _cosine(a: dict[str, float], b: dict[str, float]) -> float:
    common = set(a) & set(b)
    if not common:
        return 0.0
    dot = sum(a[t] * b[t] for t in common)
    norm_a = math.sqrt(sum(v * v for v in a.values()))
    norm_b = math.sqrt(sum(v * v for v in b.values()))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def _used_with_edges(conn) -> list[tuple[str, str, str, float, str]]:
    """A <-> B when both were invoked in the same session.

    Session id is read out of `events.payload` (JSON), which is `mine`'s to
    populate and this module's to read only -- never written here. Weight is
    the number of distinct sessions the pair co-occurred in. Symmetric, so
    both directions are written.
    """
    rows = conn.execute(
        "SELECT node_id, payload FROM events WHERE kind = 'invocation' AND node_id IS NOT NULL"
    ).fetchall()

    sessions: dict[str, set[str]] = defaultdict(set)
    for row in rows:
        session = _session_of(row["payload"])
        if session is None:
            continue
        sessions[session].add(row["node_id"])

    pair_counts: Counter[tuple[str, str]] = Counter()
    for node_ids in sessions.values():
        for a, b in itertools.combinations(sorted(node_ids), 2):
            pair_counts[(a, b)] += 1

    out = []
    for (a, b), count in pair_counts.items():
        evidence = f"co-occurred in {count} session(s)"
        out.append((a, b, "used-with", float(count), evidence))
        out.append((b, a, "used-with", float(count), evidence))
    return out


def _session_of(payload: str | None) -> str | None:
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    session = data.get("session") or data.get("session_id")
    return session if isinstance(session, str) else None
