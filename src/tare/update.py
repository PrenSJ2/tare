"""What is behind its upstream.

A plugin is installed into `plugins/cache/<marketplace>/<plugin>/<version>/`,
and the marketplace it came from declares a version in its own manifest at
`plugins/marketplaces/<marketplace>/.claude-plugin/marketplace.json`. When the
manifest has moved on and the cache has not, the capability the graph describes
is not the capability that would be installed today.

That matters here more than it would in a plain package manager, because this
tool SHELVES things. A plugin disabled while two versions behind is a decision
made about code the operator no longer has, and a promoted skill symlinks into
a versioned cache path that an update will move out from under it.

## Read-only, and offline

This compares what is already on disk. It does not contact GitHub, so it cannot
see a release the marketplace manifest has not itself picked up -- `/plugin
marketplace update` is what refreshes that, and this reports what the refreshed
manifest then implies. Saying "up to date" would overclaim; the report says
what it actually compared.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from . import paths


@dataclass
class Drift:
    plugin: str
    marketplace: str
    installed: str
    available: str
    skills: int = 0
    state: str = "live"          # live | plugin-disabled
    promoted: int = 0            # promoted skills symlinked into the stale path


@dataclass
class UpdateReport:
    behind: list = field(default_factory=list)
    current: int = 0
    unknown: list = field(default_factory=list)   # installed but not in any manifest
    marketplaces: int = 0


def _version_key(version: str) -> tuple:
    """Sort dotted numeric versions numerically.

    Plain string comparison ranks "0.10.0" below "0.9.0", which would report a
    freshly updated plugin as behind. Non-numeric components sort last so a
    real version always beats a placeholder like "unknown".
    """
    parts = []
    for chunk in str(version).replace("-", ".").split("."):
        parts.append((0, int(chunk)) if chunk.isdigit() else (1, chunk))
    return tuple(parts)


def _installed_versions() -> dict[tuple[str, str], list[str]]:
    """(marketplace, plugin) -> the version directories present in the cache."""
    out: dict[tuple[str, str], list[str]] = {}
    cache = paths.plugins_cache_dir()
    if not cache.is_dir():
        return out
    for marketplace_dir in sorted(p for p in cache.iterdir() if p.is_dir()):
        for plugin_dir in sorted(p for p in marketplace_dir.iterdir() if p.is_dir()):
            versions = sorted(
                (p.name for p in plugin_dir.iterdir() if p.is_dir()),
                key=_version_key,
            )
            if versions:
                out[(marketplace_dir.name, plugin_dir.name)] = versions
    return out


def _declared_versions() -> dict[tuple[str, str], str]:
    """(marketplace, plugin) -> the version its marketplace manifest declares."""
    out: dict[tuple[str, str], str] = {}
    root = paths.marketplaces_dir()
    if not root.is_dir():
        return out
    for manifest in root.glob("*/.claude-plugin/marketplace.json"):
        marketplace = manifest.parent.parent.name
        try:
            data = json.loads(manifest.read_text(encoding="utf-8", errors="replace"))
        except (OSError, json.JSONDecodeError):
            # A manifest we cannot read is not the same as a plugin that is up
            # to date; the caller reports the marketplace count so a silent
            # zero is visible rather than mistaken for "nothing to do".
            continue
        if not isinstance(data, dict):
            continue
        for plugin in data.get("plugins") or []:
            if isinstance(plugin, dict) and plugin.get("name") and plugin.get("version"):
                out[(marketplace, str(plugin["name"]))] = str(plugin["version"])
    return out


def check(conn) -> UpdateReport:
    """Compare the installed cache against what the marketplaces declare."""
    installed = _installed_versions()
    declared = _declared_versions()
    report = UpdateReport(marketplaces=len({m for m, _ in declared} | {m for m, _ in installed}))

    for (marketplace, plugin), versions in sorted(installed.items()):
        newest = versions[-1]
        available = declared.get((marketplace, plugin))
        if available is None:
            report.unknown.append(f"{plugin}@{marketplace}")
            continue
        if _version_key(available) <= _version_key(newest):
            report.current += 1
            continue

        key = f"{plugin}@{marketplace}"
        row = conn.execute(
            "SELECT COUNT(*) c, SUM(state = 'plugin-disabled') off "
            "FROM nodes WHERE provider_plugin = ? AND marketplace = ?",
            (plugin, marketplace),
        ).fetchone()
        promoted = conn.execute(
            "SELECT COUNT(*) c FROM nodes WHERE provider_plugin = ? AND marketplace = ? "
            "AND origin = 'user-authored'",
            (plugin, marketplace),
        ).fetchone()["c"]

        report.behind.append(Drift(
            plugin=plugin,
            marketplace=marketplace,
            installed=newest,
            available=available,
            skills=row["c"] or 0,
            state="plugin-disabled" if (row["off"] or 0) else "live",
            promoted=promoted,
        ))
    return report


def render(report: UpdateReport) -> str:
    lines: list[str] = []
    if not report.behind:
        lines.append(
            f"Nothing behind: {report.current} plugin(s) match what their "
            f"marketplace declares, across {report.marketplaces} marketplace(s)."
        )
    else:
        lines.append(f"{len(report.behind)} plugin(s) behind their marketplace:")
        for drift in report.behind:
            note = ""
            if drift.promoted:
                # The promoted symlink points at the OLD version directory, so
                # an update moves the target out from under it -- doctor's
                # stale-promoted-symlink check is what catches the aftermath.
                note = f"  <-- {drift.promoted} promoted skill(s) symlink into the old path"
            elif drift.state == "plugin-disabled":
                note = "  (currently disabled)"
            lines.append(
                f"  {drift.plugin}@{drift.marketplace}: {drift.installed} -> "
                f"{drift.available}, {drift.skills} skill(s){note}"
            )
        lines.append("")
        lines.append("Update with `/plugin` inside Claude Code, then re-run `tare scan`.")

    if report.unknown:
        lines.append("")
        lines.append(
            f"{len(report.unknown)} installed plugin(s) are in no marketplace manifest "
            f"-- nothing to compare against: {', '.join(sorted(report.unknown)[:6])}"
        )

    lines.append("")
    lines.append(
        "Compared on-disk manifests only; this never contacts a remote, so run "
        "`/plugin marketplace update` first if a manifest itself may be stale."
    )
    return "\n".join(lines)
