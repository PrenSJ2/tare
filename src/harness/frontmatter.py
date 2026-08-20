"""Parse the YAML frontmatter block at the top of a SKILL.md or agent .md.

Returns None rather than raising when a file has no usable frontmatter. Callers
turn that into a node carrying a `parse_error`, because the governing rule of
this project is to degrade and report -- a capability harness cannot read must
still appear in the graph, or the operator is told it does not exist.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import yaml


@dataclass
class Frontmatter:
    name: str = ""
    description: str = ""
    extra: dict = field(default_factory=dict)


def parse(text: str) -> tuple[Frontmatter | None, str | None]:
    """(frontmatter, error). Exactly one of the two is None."""
    if not text.startswith("---"):
        return None, "no frontmatter block"

    lines = text.splitlines()
    closing = None
    for i, line in enumerate(lines[1:], start=1):
        if line.strip() in ("---", "..."):
            closing = i
            break
    if closing is None:
        return None, "frontmatter block is not closed"

    block = "\n".join(lines[1:closing])
    try:
        data = yaml.safe_load(block)
    except yaml.YAMLError as exc:
        return None, f"frontmatter is not valid YAML: {exc.__class__.__name__}"

    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, "frontmatter is not a mapping"

    name = data.get("name")
    description = data.get("description")
    return (
        Frontmatter(
            # A non-scalar name or description is treated as absent rather than
            # coerced -- str(dict) would put a plausible-looking but wrong value
            # into the graph, which is worse than an empty one.
            name=name if isinstance(name, str) else "",
            description=description if isinstance(description, str) else "",
            extra={k: v for k, v in data.items() if k not in ("name", "description")},
        ),
        None,
    )
