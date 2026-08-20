"""Drift detection. `doctor` is the only module in this project whose entire
job is to look without touching -- it must never create, move, or delete
anything, including the vault itself (rule 1: the previous build's
`doctor.inspect()` called `vault.manifest()`, which calls `ensure_vault()`,
which `git init`s and writes -- so a *diagnostic* created the vault on a
machine that had never used one).

Findings are grouped by severity, not truncated (rule 3): a `doctor` that
hides the 38th problem to keep the list short is worse than useless the one
time there really are 40. And several checks fire on states that are
completely legitimate elsewhere in the system -- a `restored: True` entry, a
promoted symlink into a versioned plugin cache path, a dangling symlink --
so each of those is only ever reported as a *problem* under the specific
extra condition that makes it one (rule 5: do not cry wolf).
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from . import install, paths, vault


@dataclass
class Finding:
    severity: str  # "error" | "warning"
    check: str  # short machine-stable name, e.g. "dangling-symlink"
    message: str
    fix: str  # concrete remediation -- must name a command that actually helps (rule 4)


@dataclass
class Report:
    vault_state: str  # "absent" | "invalid" | "healthy"
    skill_installed: bool
    hook_installed: bool
    settings_readable: bool
    findings: list[Finding] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # checks that could not run at all


def _add(report: Report, severity: str, check: str, message: str, fix: str) -> None:
    report.findings.append(Finding(severity, check, message, fix))


# ---------------------------------------------------------------------------
# Vault git health -- read-only probes only, never `vault.manifest()` or
# `vault.ensure_vault()` on a vault that isn't already known-initialized.
# ---------------------------------------------------------------------------


def _git_status_dirty(root: Path) -> bool | None:
    """True if the vault's git working tree has uncommitted changes, False
    if clean, None if the check itself could not be performed (git missing,
    not a repo, etc.) -- distinct from "no problems found" (rule 3).
    """
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return None
    if result.returncode != 0:
        return None
    return bool(result.stdout.strip())


def _inspect_vault_git(report: Report, root: Path) -> None:
    dirty = _git_status_dirty(root)
    if dirty is None:
        report.skipped.append("vault git status could not be checked")
    elif dirty:
        _add(
            report,
            "warning",
            "vault-git-dirty",
            f"the vault at {root} has uncommitted changes -- every stash/restore is supposed to commit itself",
            f"inspect with `git -C {root} status`, then commit or discard by hand",
        )


# ---------------------------------------------------------------------------
# Manifest cross-checks
# ---------------------------------------------------------------------------


def _inspect_manifest(report: Report, conn, root: Path) -> dict | None:
    """Read the manifest for cross-checking against the vault directory and
    the graph. Returns None (after recording a finding, never raising --
    rule 2: a corrupt manifest is a finding, not a crash) if it can't be
    read.
    """
    try:
        data = vault.manifest()
    except ValueError as exc:
        _add(
            report,
            "error",
            "vault-manifest-corrupt",
            f"vault manifest is unreadable: {exc}",
            f"recover the last committed copy: git -C {root} checkout HEAD -- manifest.json",
        )
        return None
    return data


def _entry_source_path(key: str, kind: str) -> Path:
    root = paths.vault_dir()
    if kind == "skills":
        return root / "skills" / key
    return root / "agents" / f"{key}.md"


def _entry_live_path(key: str, kind: str) -> Path:
    if kind == "skills":
        return paths.skills_dir() / key
    return paths.agents_dir() / f"{key}.md"


def _inspect_manifest_vs_disk(report: Report, data: dict) -> None:
    """Both directions of the manifest/vault-directory cross-check: a
    manifest entry with nothing on disk loses a capability silently, and a
    directory entry with no manifest record leaves a stale, unindexed copy
    behind -- neither is safe to ignore.
    """
    root = paths.vault_dir()
    for kind in vault.KINDS:
        kind_dir = root / kind
        entries = data.get(kind, {})

        for key in entries:
            source = _entry_source_path(key, kind)
            if not source.exists():
                _add(
                    report,
                    "error",
                    "manifest-entry-missing-from-vault",
                    f"manifest lists {kind}/{key} but nothing exists at {source}",
                    f"the vault copy is gone -- check `git -C {root} log -- {kind}/{key}` for the last known content, "
                    "there is no other copy",
                )

        if kind_dir.is_dir():
            on_disk = {p.name if kind == "skills" else p.stem for p in kind_dir.iterdir()}
            for name in sorted(on_disk - set(entries)):
                found = _entry_source_path(name, kind)
                _add(
                    report,
                    "error",
                    "vault-entry-missing-from-manifest",
                    f"{found} exists in the vault but has no manifest entry",
                    "the manifest is the only index to what's stashed -- add an entry by hand from "
                    f"`git -C {root} log` history, or the capability is unreachable via `harness activate`",
                )


def _inspect_restored_entries(report: Report, data: dict) -> None:
    """A `restored: True` manifest entry is normal (rule 5) -- it only
    becomes a finding when the symlink it claims exists is actually
    absent, which means the graph is about to say 'live' about a
    capability nothing serves any more.
    """
    for kind in vault.KINDS:
        for key, entry in data.get(kind, {}).items():
            if not entry.get("restored"):
                continue
            live = _entry_live_path(key, kind)
            if not live.is_symlink() and not live.exists():
                _add(
                    report,
                    "error",
                    "restored-symlink-missing",
                    f"{kind}/{key} is marked restored in the manifest but {live} does not exist",
                    f"run `harness activate {key}` to relink it, or `harness scan` to reconcile the graph "
                    "if it was deliberately removed",
                )


def _inspect_shelved_but_loaded(report: Report, data: dict) -> None:
    """A manifest entry that is ALSO a real file on the load path.

    Stashing moves the file, so normal operation cannot produce this -- but
    restoring a vault by hand (copying the files back rather than using
    `harness activate`) leaves the manifest untouched and the capability
    loaded. Found on the real machine after exactly that: 63 entries the
    graph called shelved while every one of them sat in ~/.claude/skills.

    Worth an error rather than a warning because it makes two other outputs
    lie: `audit` omits the capability from the always-loaded total though it
    is loaded, and `lookup` tells the operator to activate something already
    active. Deliberately does NOT fire for a symlink -- that is a restore,
    which `_inspect_restored_entries` owns.
    """
    for kind in vault.KINDS:
        for key, entry in data.get(kind, {}).items():
            live = _entry_live_path(key, kind)
            if live.is_symlink() or not live.exists():
                continue
            _add(
                report,
                "error",
                "shelved-but-loaded",
                f"{kind}/{key} is in the vault manifest but {live} is a real file on the load path "
                "-- it is being loaded while the graph reports it shelved",
                f"if you restored it by hand, drop it from the vault so the graph agrees: "
                f"`harness activate {key}` then `harness deactivate {key}`, or remove the vault "
                "entry and re-run `harness scan`",
            )


# ---------------------------------------------------------------------------
# Graph cross-checks
# ---------------------------------------------------------------------------


def _inspect_vaulted_without_node(report: Report, conn, data: dict) -> None:
    """A manifest entry (not restored) with no corresponding graph node --
    `harness lookup` cannot surface a capability that isn't in the graph at
    all, silently defeating the entire point of vaulting rather than
    deleting.
    """
    from . import scan  # local import: doctor must not force scan's import-time cost onto every caller

    for kind in vault.KINDS:
        node_kind = "skill" if kind == "skills" else "agent"
        for key, entry in data.get(kind, {}).items():
            if entry.get("restored"):
                continue
            node_id = scan._vaulted_node_id(key, kind)
            row = conn.execute("SELECT 1 FROM nodes WHERE id = ?", (node_id,)).fetchone()
            if row is None:
                _add(
                    report,
                    "warning",
                    "vaulted-without-node",
                    f"{kind}/{key} is vaulted but has no node {node_id!r} in the graph",
                    "run `harness scan` to rebuild it",
                )


def _inspect_stale_promoted_symlinks(report: Report, conn) -> None:
    """A promoted skill (origin='user-authored', provider_plugin set) whose
    plugin is disabled in settings.json but whose symlink still exists on
    disk (rule 4/5): promotion followed by disabling is the whole point of
    mechanism 3, so this is only a problem if the settings say the plugin
    should be OFF while the node still reads 'live' -- i.e. the promote step
    ran but the disable step never landed, or the plugin was disabled by
    hand afterward without going through `harness activate`/un-promote.
    Such a node is `state='live'`, so telling the operator to run `harness
    activate` (which no-ops on a live node) is exactly the previous build's
    dead-end advice; the real fix is re-running the plugin toggle.
    """
    try:
        settings = install._load()
    except ValueError:
        report.skipped.append("stale-promoted-symlink check skipped: settings.json is unreadable")
        return
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return

    rows = conn.execute(
        "SELECT id, provider_plugin, marketplace, state FROM nodes "
        "WHERE kind='skill' AND origin='user-authored' AND provider_plugin IS NOT NULL AND provider_plugin != ''"
    ).fetchall()
    for row in rows:
        plugin_key = f"{row['provider_plugin']}@{row['marketplace']}" if row["marketplace"] else row["provider_plugin"]
        if enabled.get(plugin_key, True) is False and row["state"] == "live":
            _add(
                report,
                "warning",
                "stale-promoted-symlink",
                f"{row['id']} is promoted and marked live, but {plugin_key} is disabled in settings.json",
                f"re-run the plugin disable for {plugin_key} (it should have un-promoted this skill); "
                "`harness activate` on this node is a no-op, it is already live",
            )


# ---------------------------------------------------------------------------
# Symlink sweep -- dangling links under skills_dir()/agents_dir()
# ---------------------------------------------------------------------------


def _inspect_dangling_symlinks(report: Report) -> None:
    """A dangling symlink under skills_dir()/agents_dir() is completely
    normal mid-transition (e.g. a plugin update mid-flight leaves a
    promoted symlink briefly unresolved) -- scan.py emits a node for it
    with `parse_error='dangling symlink'` rather than dropping it (rule 5).
    doctor still surfaces it, because a symlink that stays dangling across
    a scan is real drift the operator should know about -- but always as a
    fact, not phrased as though every dangling link is automatically wrong.
    """
    for root, label in ((paths.skills_dir(), "skill"), (paths.agents_dir(), "agent")):
        if not root.is_dir():
            continue
        for entry in sorted(root.iterdir()):
            if entry.is_symlink() and not entry.exists():
                _add(
                    report,
                    "warning",
                    "dangling-symlink",
                    f"{entry} is a dangling {label} symlink (target does not exist)",
                    "if the target plugin/vault entry was deliberately removed, delete this symlink by hand; "
                    "otherwise run `harness scan` and check the node's parse_error for what it resolves to",
                )


# ---------------------------------------------------------------------------
# Install / settings health
# ---------------------------------------------------------------------------


def _inspect_install(report: Report) -> None:
    report.skill_installed = paths.skill_install_path().exists()
    report.hook_installed = install.is_installed()

    if not report.skill_installed:
        _add(
            report,
            "error",
            "skill-not-installed",
            f"the harness skill is not installed at {paths.skill_install_path()}",
            "run `harness install`",
        )
    if not report.hook_installed:
        registered = install.registered_command()
        if registered is None:
            _add(
                report,
                "error",
                "hook-not-installed",
                "no SessionStart hook is registered for harness",
                "run `harness install`",
            )
        else:
            _add(
                report,
                "error",
                "hook-command-missing",
                f"the registered hook command {registered!r} points at an executable that no longer exists",
                "run `harness install` to re-register it against the current executable",
            )

    try:
        install._load()
        report.settings_readable = True
    except ValueError as exc:
        report.settings_readable = False
        _add(
            report,
            "error",
            "settings-unreadable",
            f"settings.json could not be read: {exc}",
            "fix the JSON by hand, or restore from the most recent "
            f"{paths.settings_path().name}.*.bak written by `harness install`",
        )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def inspect(conn) -> Report:
    """Run every check. Strictly read-only: never calls `vault.ensure_vault()`
    or `vault.manifest()` on a vault that isn't already known-initialized
    (rule 1), and never writes to the graph, the vault, or settings.json.
    """
    report = Report(vault_state="absent", skill_installed=False, hook_installed=False, settings_readable=False)

    if not paths.vault_dir().exists():
        report.vault_state = "absent"
    elif not vault.is_initialized():
        report.vault_state = "invalid"
        _add(
            report,
            "error",
            "vault-invalid",
            f"{paths.vault_dir()} exists but is not a valid vault (missing .git or manifest.json)",
            "if this was a killed `harness install`/first stash, remove the partial directory and re-run "
            "the operation that creates it; otherwise investigate by hand -- do not delete without checking "
            "for a partial .git history first",
        )
    else:
        report.vault_state = "healthy"
        root = paths.vault_dir()
        _inspect_vault_git(report, root)
        data = _inspect_manifest(report, conn, root)
        if data is not None:
            _inspect_manifest_vs_disk(report, data)
            _inspect_restored_entries(report, data)
            _inspect_shelved_but_loaded(report, data)
            _inspect_vaulted_without_node(report, conn, data)

    _inspect_stale_promoted_symlinks(report, conn)
    _inspect_dangling_symlinks(report)
    _inspect_install(report)

    return report


def render(report: Report) -> str:
    """Human-readable summary. Never truncates the findings list (rule 3)."""
    lines = [f"vault: {report.vault_state}"]
    lines.append(f"skill installed: {report.skill_installed}")
    lines.append(f"hook installed: {report.hook_installed}")
    lines.append(f"settings readable: {report.settings_readable}")

    if report.skipped:
        lines.append("")
        lines.append(f"checks that could not run ({len(report.skipped)}):")
        for note in report.skipped:
            lines.append(f"  - {note}")

    if not report.findings:
        lines.append("")
        lines.append("no problems found")
        return "\n".join(lines)

    errors = [f for f in report.findings if f.severity == "error"]
    warnings = [f for f in report.findings if f.severity == "warning"]

    lines.append("")
    lines.append(f"{len(report.findings)} finding(s): {len(errors)} error(s), {len(warnings)} warning(s)")
    for group_name, group in (("errors", errors), ("warnings", warnings)):
        if not group:
            continue
        lines.append("")
        lines.append(f"{group_name}:")
        for f in group:
            lines.append(f"  [{f.check}] {f.message}")
            lines.append(f"    fix: {f.fix}")

    return "\n".join(lines)
