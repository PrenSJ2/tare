"""How the configuration got to be the way it is.

Two independent records, because neither is sufficient alone:

- **The filesystem.** Every capability file has a birth time and an mtime, so
  "what appeared recently" and "what changed since it appeared" are free,
  complete and retroactive. What they cannot say is *who* or *why*.
- **The transcripts.** A `Write` or `Edit` against a capability's own files is
  the only retroactive evidence that a session changed it, and it carries the
  project and the session that did it.

The second is evidence of presence, never of absence. Transcripts age out, get
deleted, are excluded as tagging exhaust, and never existed for edits made in
an editor. A capability with no recorded session edit was not necessarily
written by hand — so nothing here claims it was. `authored_outside_sessions`
is a statement about the *record*, and the console says so in as many words.

One thing the filesystem genuinely cannot distinguish: a plugin skill's birth
time is when the plugin was cached on this machine, not when anyone wrote it.
Plugin-provided capabilities are therefore reported separately from the
user's own, rather than interleaved into one misleading timeline.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone

# Below this, an mtime later than the birth time is the copy itself rather
# than an edit: writing a file sets both, microseconds apart.
SAME_WRITE_SECONDS = 2.0


@dataclass
class Entry:
    node_id: str
    name: str
    kind: str
    origin: str
    plugin: str | None
    state: str
    born: str | None = None
    changed: str | None = None
    edits: list[dict] = field(default_factory=list)


def _iso(stamp: float) -> str:
    return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat(timespec="seconds")


def _times(path: str | None) -> tuple[str | None, str | None]:
    """(born, changed) for a capability file, or (None, None) if it is gone.

    `st_birthtime` is macOS and modern Linux; where it is missing, ctime is the
    closest available and is used rather than dropping the entry, since a
    missing date would silently remove a capability from the timeline.
    """
    if not path:
        return None, None
    try:
        st = os.stat(path)
    except OSError:
        return None, None
    born = getattr(st, "st_birthtime", None) or st.st_ctime
    changed = st.st_mtime if st.st_mtime - born > SAME_WRITE_SECONDS else None
    return _iso(born), _iso(changed) if changed else None


def entries(conn) -> list[Entry]:
    """Every capability, with whatever the two records know about it."""
    edits: dict[str, list[dict]] = {}
    for row in conn.execute(
        "SELECT ts, node_id, payload FROM events WHERE kind = 'edit' ORDER BY ts"
    ):
        try:
            payload = json.loads(row["payload"])
        except (TypeError, ValueError):
            continue
        payload["ts"] = row["ts"]
        edits.setdefault(row["node_id"], []).append(payload)

    out = []
    for row in conn.execute(
        "SELECT id, name, kind, origin, provider_plugin, state, path FROM nodes"
    ):
        born, changed = _times(row["path"])
        out.append(Entry(
            node_id=row["id"], name=row["name"], kind=row["kind"],
            origin=row["origin"] or "", plugin=row["provider_plugin"],
            state=row["state"], born=born, changed=changed,
            edits=edits.get(row["id"], []),
        ))
    return out


def _is_own(entry: Entry) -> bool:
    """Written by the operator, as opposed to installed with a plugin."""
    return not entry.plugin


def summary(conn) -> dict:
    """The sidebar payload: four lists and the counts that frame them.

    Ordering is newest-first everywhere, and every list is capped. This is a
    sidebar, not an audit log — `tare history` prints the long form.
    """
    all_entries = entries(conn)
    own = [e for e in all_entries if _is_own(e)]

    def newest(items, key, limit=12):
        dated = [e for e in items if getattr(e, key)]
        dated.sort(key=lambda e: getattr(e, key), reverse=True)
        return dated[:limit]

    touched = [e for e in all_entries if e.edits]
    touched.sort(key=lambda e: e.edits[-1]["ts"], reverse=True)

    def shape(entry: Entry) -> dict:
        return {
            "id": entry.node_id, "n": entry.name, "k": entry.kind,
            "pl": entry.plugin, "s": entry.state,
            "born": entry.born, "changed": entry.changed,
            "edits": entry.edits[-4:],
            "edit_count": len(entry.edits),
            # Carried separately from `changed`: a capability edited from a
            # session may have been touched again since by something else, and
            # an mtime cannot tell the two apart.
            "last_edit": entry.edits[-1]["ts"] if entry.edits else None,
        }

    return {
        # Yours, newest first: the answer to "what did I add lately".
        "added": [shape(e) for e in newest(own, "born")],
        # Changed after it was created, so an evolving capability surfaces
        # even if it was written long ago.
        "evolved": [shape(e) for e in newest(own, "changed")],
        # The only list that can name a culprit.
        "session_edited": [shape(e) for e in touched[:12]],
        # Plugin capabilities date from when the plugin was cached, so they
        # are kept apart from the timeline above rather than dominating it.
        "installed": [shape(e) for e in newest(
            [e for e in all_entries if not _is_own(e)], "born", limit=8)],
        "counts": {
            "own": len(own),
            "from_plugins": len(all_entries) - len(own),
            "session_edited": len(touched),
            "authored_outside_sessions": len([e for e in own if not e.edits]),
        },
    }
