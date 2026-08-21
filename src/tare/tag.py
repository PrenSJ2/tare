"""Normalise a capability's free-text description into structured fields.

Third-party skill descriptions vary from one terse line to three hundred words
of trigger phrases. `lookup` ranks better against consistent fields, so each
capability is reduced to a purpose line, a when-to-use line, and a few tags.

The backend is the `claude` CLI, invoked once per uncached capability and
cached by content hash so a re-run costs nothing.

## The honest limitation

`claude -p` writes a session transcript per invocation into `~/.claude/projects`
-- the very corpus `mine` reads. Constraining the subprocess with
`--allowedTools ''` and `--max-turns 1` does NOT stop it; this was verified
directly in the previous build (one call took the transcript count from 1504 to
1505, and 241 such transcripts accumulated).

So harness's own code writes nothing under ~/.claude except its database, but
the tagging subprocess writes a transcript per uncached capability and harness
cannot prevent that while shelling out. The consequence that mattered -- those
sessions polluting the usage signal -- is closed: `mine` excludes them by
content signature and reports the count. That is why TAG_PROMPT_SIGNATURE is
imported from `mine` rather than duplicated here. If the two ever drift, the
exclusion silently stops working and harness starts mining its own exhaust.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass

from .mine import TAG_PROMPT_SIGNATURE

TAG_MODEL_TIMEOUT = 60

PROMPT_TEMPLATE = (
    TAG_PROMPT_SIGNATURE
    + """

Return ONLY a JSON object with exactly these keys:
  "purpose_line": one sentence saying what it does.
  "when_to_use":  one sentence saying when it should fire.
  "tags":         a list of 3-6 short lowercase keywords.

Capability name: {name}
Description:
{description}
"""
)


@dataclass
class TagResult:
    tagged: int = 0
    cached: int = 0
    failed: int = 0
    skipped: int = 0


def content_hash(name: str, description: str) -> str:
    return hashlib.sha256(f"{name}\x00{description}".encode("utf-8")).hexdigest()


def _extract_json(text: str) -> dict | None:
    """Pull the first balanced JSON object out of a model response.

    A greedy `re.search(r"\\{.*\\}")` was used here before and discarded good
    output whenever the model appended a sentence of prose after the object --
    it matched to the LAST closing brace, producing invalid JSON. Scanning for
    the first balanced object handles both trailing prose and nested objects.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start : i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def _normalise(data: dict, fallback_name: str) -> dict | None:
    """Validate the model's object, or reject it.

    `tags` is type-checked deliberately. If the model returns a string where a
    list was asked for, iterating it yields single characters, and the previous
    build stored that garbage *as a success* -- an unusable index entry that
    looked fine in the row count.
    """
    purpose = data.get("purpose_line")
    when = data.get("when_to_use")
    tags = data.get("tags")

    if not isinstance(purpose, str) or not isinstance(when, str):
        return None
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return None

    return {
        # An empty purpose line makes a capability permanently unfindable, so
        # fall back to its name rather than storing "". Two real skills
        # (product-demo-capture, slideshow) were lost that way before.
        "purpose_line": purpose.strip() or fallback_name,
        "when_to_use": when.strip(),
        "tags": ", ".join(t.strip() for t in tags if t.strip()),
    }


def _ask_claude(name: str, description: str) -> dict | None:
    prompt = PROMPT_TEMPLATE.format(name=name, description=description)
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "", "--max-turns", "1"],
            capture_output=True,
            text=True,
            timeout=TAG_MODEL_TIMEOUT,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    data = _extract_json(proc.stdout)
    if data is None:
        return None
    return _normalise(data, name)


def tag_all(conn, ask=_ask_claude) -> TagResult:
    """Tag every node whose description has not already been tagged.

    `ask` is injectable so the suite never shells out to the real CLI.
    """
    result = TagResult()
    rows = conn.execute(
        "SELECT id, name, desc_raw FROM nodes WHERE COALESCE(desc_raw, '') != ''"
    ).fetchall()

    for row in rows:
        digest = content_hash(row["name"], row["desc_raw"])
        cached = conn.execute(
            "SELECT purpose_line, when_to_use, tags FROM tag_cache WHERE content_hash = ?",
            (digest,),
        ).fetchone()

        if cached is not None:
            fields = {
                "purpose_line": cached["purpose_line"],
                "when_to_use": cached["when_to_use"],
                "tags": cached["tags"],
            }
            result.cached += 1
        else:
            fields = ask(row["name"], row["desc_raw"])
            if fields is None:
                # Degrade and report: a capability we could not tag keeps
                # whatever it had and is counted, never silently dropped.
                result.failed += 1
                continue
            conn.execute(
                "INSERT OR REPLACE INTO tag_cache (content_hash, purpose_line, when_to_use, tags)"
                " VALUES (?, ?, ?, ?)",
                (digest, fields["purpose_line"], fields["when_to_use"], fields["tags"]),
            )
            result.tagged += 1

        conn.execute(
            "UPDATE nodes SET purpose_line = ?, when_to_use = ?, tags = ?,"
            " content_hash = ?, tag_source = 'claude' WHERE id = ?",
            (
                fields["purpose_line"],
                fields["when_to_use"],
                fields["tags"],
                digest,
                row["id"],
            ),
        )

    conn.commit()
    return result
