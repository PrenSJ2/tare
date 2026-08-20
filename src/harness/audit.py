"""What the always-loaded index actually costs, and what got missed.

`lookup.py` is what makes shelving survivable; this is what tells the
operator whether it's worth doing and what it would break. Two things this
module has to get right or it lies to the operator by omission:

- `total_tokens` (the headline figure) counts `state='live'` nodes ONLY.
  Vaulted and plugin-disabled capabilities are real -- they still have a
  node, still show up in `lookup` -- but they cost nothing per turn, so
  folding them into the headline would overstate the current bill. They are
  reported separately instead (`disabled_skills`/`disabled_tokens`), so the
  headline means exactly what it says: this is what's loaded right now.
- `coverage` reports how many `SKILL.md` files exist on disk against how
  many became a node. Some plugins ship skills at layouts `scan.py` never
  reaches (`.agents/skills/`, `.claude/skills/` instead of the expected
  `<plugin>/<version>/skills/`). A silent gap there is a wrong answer, not
  a rounding error -- `render()` prints the coverage line unconditionally,
  even when the gap is zero, so a clean scan says so instead of just being
  quiet about it.

Standard library only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from . import paths


@dataclass
class Audit:
    total_tokens: int
    never_invoked_tokens: int
    by_bucket: dict
    mixed_plugins: list
    duplicates: list
    disabled_skills: int
    disabled_tokens: int
    coverage: tuple  # (classified, total_skill_md_files_found)
    instructions: list = field(default_factory=list)  # (tokens, lines, project, file)


# A project's instruction file is loaded on EVERY turn in that project, the
# same as the skill index -- and on this machine the largest is nearly four
# times the whole index. Auditing capabilities while ignoring it measures the
# smaller half of the problem.
_INSTRUCTION_FILES = ("CLAUDE.md", ".claude/CLAUDE.md", "AGENTS.md")

# Roughly the size of the entire always-loaded capability index. An
# instruction file past this is not a file any more, it is a context budget.
HEAVY_INSTRUCTIONS = 10_000


def project_instructions() -> list[tuple[int, int, str, str]]:
    """(tokens, lines, project, filename) for every project instruction file,
    heaviest first.

    Read from disk rather than the graph: these are not capabilities, they are
    the accumulated project knowledge that grows quietly because nothing ever
    prunes it and nothing has ever measured it.
    """
    from . import memory

    found: list[tuple[int, int, str, str]] = []
    projects = paths.projects_dir()
    if not projects.is_dir():
        return found
    for entry in sorted(projects.iterdir()):
        if not entry.is_dir():
            continue
        root = memory.resolve_project(entry.name)
        if root is None:
            continue
        for name in _INSTRUCTION_FILES:
            path = root / name
            try:
                if not path.is_file():
                    continue
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            found.append((paths.est_tokens(text), len(text.splitlines()), root.name, name))
    found.sort(reverse=True)
    return found


def audit(conn) -> Audit:
    total_tokens = _sum_tokens(conn, "state = 'live'")
    never_invoked_tokens = _never_invoked_tokens(conn)
    by_bucket = _bucket_counts(conn)
    mixed_plugins = _mixed_plugins(conn)
    duplicates = _duplicates(conn)
    disabled_skills, disabled_tokens = _disabled(conn)
    coverage = _coverage(conn)

    return Audit(
        total_tokens=total_tokens,
        never_invoked_tokens=never_invoked_tokens,
        by_bucket=by_bucket,
        mixed_plugins=mixed_plugins,
        duplicates=duplicates,
        disabled_skills=disabled_skills,
        disabled_tokens=disabled_tokens,
        coverage=coverage,
        instructions=project_instructions(),
    )


# ---------------------------------------------------------------------------
# Headline figures
# ---------------------------------------------------------------------------


def _sum_tokens(conn, where: str) -> int:
    row = conn.execute(f"SELECT COALESCE(SUM(est_tokens), 0) AS t FROM nodes WHERE {where}").fetchone()
    return row["t"]


def _never_invoked_tokens(conn) -> int:
    """Tokens behind live nodes with zero recorded invocations.

    LEFT JOIN + COALESCE, per mine.py's own convention: "no usage row" and
    "a usage row with invocations=0" both mean never-invoked, and mine.py
    never writes a zero row, so a plain JOIN would silently drop every
    node that has never once been mined as used -- exactly the ones this
    figure exists to count.
    """
    row = conn.execute(
        """
        SELECT COALESCE(SUM(n.est_tokens), 0) AS t
        FROM nodes n LEFT JOIN usage u ON u.node_id = n.id
        WHERE n.state = 'live' AND COALESCE(u.invocations, 0) = 0
        """
    ).fetchone()
    return row["t"]


def _bucket_counts(conn) -> dict:
    counts = {"always": 0, "sometimes": 0, "rarely": 0}
    for row in conn.execute("SELECT bucket, COUNT(*) AS c FROM nodes WHERE bucket IS NOT NULL GROUP BY bucket"):
        if row["bucket"] in counts:
            counts[row["bucket"]] = row["c"]
    return counts


def _disabled(conn) -> tuple[int, int]:
    row = conn.execute(
        "SELECT COUNT(*) AS c, COALESCE(SUM(est_tokens), 0) AS t FROM nodes WHERE state = 'plugin-disabled'"
    ).fetchone()
    return row["c"], row["t"]


# ---------------------------------------------------------------------------
# Mixed plugins
# ---------------------------------------------------------------------------


def _mixed_plugins(conn) -> list[dict]:
    """Plugins where some skills have been invoked and some never have --
    the plugin can't simply be switched off (mechanism 2), but most of what
    it ships is dead weight anyway (mechanism 3's target).

    Grouped across EVERY state a plugin's skills can be in, not just
    'live': a skill already flipped to 'plugin-disabled' by a previous
    partial shelve still counts toward `total` and, if never invoked,
    toward the stuck-token figure -- the operator needs the whole picture
    of what this plugin costs, not just the slice still currently loaded.
    """
    rows = conn.execute(
        """
        SELECT n.provider_plugin AS plugin, n.marketplace AS marketplace,
               n.est_tokens AS est_tokens, COALESCE(u.invocations, 0) AS invocations
        FROM nodes n LEFT JOIN usage u ON u.node_id = n.id
        WHERE n.kind = 'skill' AND n.provider_plugin IS NOT NULL AND n.provider_plugin != ''
        """
    ).fetchall()

    groups: dict[tuple[str, str | None], dict] = {}
    for row in rows:
        key = (row["plugin"], row["marketplace"])
        g = groups.setdefault(key, {"total": 0, "used": 0, "tokens_stuck": 0})
        g["total"] += 1
        if row["invocations"] > 0:
            g["used"] += 1
        else:
            g["tokens_stuck"] += row["est_tokens"] or 0

    mixed = []
    for (plugin, marketplace), g in groups.items():
        if 0 < g["used"] < g["total"]:
            label = f"{plugin}@{marketplace}" if marketplace else plugin
            mixed.append(
                {
                    "plugin": plugin,
                    "marketplace": marketplace,
                    "label": label,
                    "used": g["used"],
                    "total": g["total"],
                    "tokens_stuck": g["tokens_stuck"],
                }
            )
    mixed.sort(key=lambda m: (-m["tokens_stuck"], m["label"]))
    return mixed


# ---------------------------------------------------------------------------
# Duplicates
# ---------------------------------------------------------------------------


def _duplicates(conn) -> list[dict]:
    """Two-or-more capabilities sharing one `name` -- necessarily from
    different sources, since `id` encodes the source (plugin/marketplace or
    filesystem path) and is the primary key, so two rows can never share
    both `id` and `name`.
    """
    rows = conn.execute(
        "SELECT id, kind, name, origin FROM nodes WHERE name IS NOT NULL AND name != '' ORDER BY name, id"
    ).fetchall()

    by_name: dict[str, list] = {}
    for row in rows:
        by_name.setdefault(row["name"], []).append(row)

    duplicates = []
    for name, members in sorted(by_name.items()):
        if len(members) > 1:
            duplicates.append(
                {
                    "name": name,
                    "ids": [m["id"] for m in members],
                    "kinds": [m["kind"] for m in members],
                    "origins": [m["origin"] for m in members],
                }
            )
    return duplicates


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------


def _coverage(conn) -> tuple[int, int]:
    """(classified, found) -- how many `SKILL.md` files this machine's
    `~/.claude` tree actually holds, versus how many became a `nodes` row.

    `found` walks the WHOLE `claude_home()` tree with `rglob`, on purpose:
    that reaches layouts `scan.py`'s targeted walk does not (a plugin
    shipping skills under `.agents/skills/` or `.claude/skills/` instead of
    `<plugin>/<version>/skills/`), which is exactly the gap this figure
    exists to surface. `classified` is the intersection of that file set
    with the paths nodes actually recorded -- not a raw node count -- so a
    node whose backing file has since vanished doesn't inflate the figure.
    """
    root = paths.claude_home()
    if not root.is_dir():
        return 0, 0

    found = {p.resolve(strict=False) for p in root.rglob("SKILL.md")}

    node_paths = set()
    for row in conn.execute("SELECT path FROM nodes WHERE kind = 'skill' AND path IS NOT NULL"):
        node_paths.add(Path(row["path"]).resolve(strict=False))

    classified = len(found & node_paths)
    return classified, len(found)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(a: Audit) -> str:
    """Human-readable report, matching the shape an operator reads at the
    terminal: headline first, then the buckets, then everything that needs
    a decision (mixed plugins, duplicates), then coverage -- always, clean
    or not."""
    lines = []

    pct = round(100 * a.never_invoked_tokens / a.total_tokens) if a.total_tokens else 0
    lines.append(f"Always-loaded index: ~{a.total_tokens:,} tokens per turn")
    lines.append(f"  never invoked:     ~{a.never_invoked_tokens:,} tokens ({pct}%)")
    lines.append(
        f"  plugin-disabled:   {a.disabled_skills} skills (~{a.disabled_tokens:,} tok) "
        "excluded above -- not enabled in settings.json"
    )
    lines.append(
        f"Buckets: always={a.by_bucket.get('always', 0)}, "
        f"rarely={a.by_bucket.get('rarely', 0)}, "
        f"sometimes={a.by_bucket.get('sometimes', 0)}"
    )

    if a.mixed_plugins:
        lines.append("")
        lines.append("Mixed plugins (some skills used, some not):")
        for mp in a.mixed_plugins:
            lines.append(f"  {mp['label']}: {mp['used']}/{mp['total']} used, ~{mp['tokens_stuck']:,} tok stuck")

    if a.duplicates:
        lines.append("")
        lines.append("Duplicates (same name, different source):")
        for dup in a.duplicates:
            lines.append(f"  {dup['name']}: {', '.join(dup['ids'])}")

    classified, found = a.coverage
    lines.append("")
    gap_note = "clean" if classified == found else f"{found - classified} unclassified"
    lines.append(
        f"Coverage: {classified}/{found} SKILL.md files classified ({gap_note}) -- "
        "scanner does not reach every layout (e.g. .agents/skills/, .claude/skills/)"
    )

    if a.instructions:
        heaviest = a.instructions[0]
        lines.append("")
        lines.append("Project instructions (loaded every turn IN THAT PROJECT):")
        for tokens, count, project, name in a.instructions[:5]:
            flag = "  <-- heavier than the whole index above" if tokens >= HEAVY_INSTRUCTIONS else ""
            lines.append(f"  {tokens:7,} tok  {count:5,} lines  {project}/{name}{flag}")
        if len(a.instructions) > 5:
            rest = sum(t for t, _, _, _ in a.instructions[5:])
            lines.append(f"  … {len(a.instructions) - 5} more, ~{rest:,} tok combined")
        if heaviest[0] >= HEAVY_INSTRUCTIONS:
            lines.append("")
            lines.append(
                f"  {heaviest[2]}'s instructions cost ~{heaviest[0]:,} tok on every turn "
                f"there -- shelving capabilities saves less than pruning that one file."
            )

    return "\n".join(lines)
