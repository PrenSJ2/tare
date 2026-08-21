"""The way back: `activate` brings a shelved capability live again,
`deactivate` reverses that.

Two mechanisms a name can resolve to (the design notes mechanisms 1 and 2/3):
a vaulted user-authored skill/agent (undo via `vault.restore`), or a
wholly-disabled plugin (flip it back on in `enabledPlugins`). `deactivate`
only ever reverses the first -- it must say so plainly rather than quietly
doing nothing when asked to deactivate a plugin (see `deactivate`'s
docstring).

Every name given here can be a graph id, a graph `name`, or -- because the
vault manifest is keyed by filesystem name while the graph and the operator
both use the frontmatter name -- a filesystem name that doesn't appear
anywhere in the graph at all. See `vault.py`'s module docstring for why
those three namespaces diverge in real capabilities.
"""

from __future__ import annotations

from pathlib import Path

from . import install, scan, vault


def _find_nodes(conn, name: str):
    """Every graph node matching `name`: an exact id match (unambiguous by
    construction -- ids are the primary key) takes priority; only when
    there is no id match do we fall back to matching by the declared
    `name` column, which is where two different nodes -- a skill and an
    agent, or a live node and a vaulted one -- can collide (rule 2).
    """
    row = conn.execute("SELECT * FROM nodes WHERE id = ?", (name,)).fetchone()
    if row is not None:
        return [row]
    return conn.execute("SELECT * FROM nodes WHERE name = ? ORDER BY id", (name,)).fetchall()


def _describe(rows) -> str:
    return ", ".join(f"{r['id']} (state={r['state']})" for r in rows)


def _resolve_via_vault(name: str):
    """Try `name` against the vault manifest directly, both kinds -- the
    fallback for a name that only exists as a *filesystem* key and so
    matches nothing in the graph at all (rule 1: resolve through
    `vault.resolve_name` explicitly; never guess from the graph's `name`
    column alone, and never hand the operator's raw string straight to
    `vault.restore`/`vault.unrestore` without going through this).

    Returns (kind, manifest_key) or None. Raises LookupError -- never
    resolves it silently -- when `name` is ambiguous, either within one
    kind (`vault.resolve_name`'s own ambiguity) or across both (a skill and
    an agent stashed under the same declared name) (rule 2).
    """
    if not vault.is_initialized():
        return None

    found = []
    for kind in vault.KINDS:
        try:
            key = vault.resolve_name(name, kind)
        except LookupError as exc:
            raise LookupError(f"{name!r} is ambiguous in the vault: {exc}") from exc
        if key is not None:
            found.append((kind, key))

    if len(found) > 1:
        raise LookupError(f"{name!r} matches vaulted entries of more than one kind: {found}; retry with a specific id")
    return found[0] if found else None


def _unpromote_plugin(conn, plugin_key: str) -> list[str]:
    """Undo every promotion belonging to `plugin_key` ("plugin@marketplace")
    before it is re-enabled (rule 4).

    A promoted skill is symlinked onto `skills_dir()` with
    origin='user-authored' but keeps its plugin-scoped id and
    provider_plugin/marketplace columns (see scan.py's
    `_promoted_skill_node`). If the plugin is re-enabled while that symlink
    survives, the skill is served twice -- once through the symlink, once
    through the plugin itself -- doubling its token cost silently. Scoped
    on provider_plugin AND marketplace both: plugin names repeat across
    marketplaces, so provider_plugin alone could unpromote a same-named
    plugin belonging to someone else.
    """
    plugin, _, marketplace = plugin_key.partition("@")
    rows = conn.execute(
        "SELECT id, path FROM nodes WHERE kind='skill' AND origin='user-authored' "
        "AND provider_plugin = ? AND marketplace = ?",
        (plugin, marketplace),
    ).fetchall()

    unpromoted: list[str] = []
    for row in rows:
        if row["path"]:
            # nodes.path for a promoted skill is `<symlink>/SKILL.md` (scan.py
            # upserts it from the live entry under skills_dir(), never the
            # resolved plugin-cache target) -- its parent is the symlink itself.
            symlink_path = Path(row["path"]).parent
            if symlink_path.is_symlink():
                symlink_path.unlink()
        # Return the row to plugin-served state regardless of whether a
        # symlink was actually there to remove -- a symlink already gone by
        # hand must not leave the row permanently claiming user-authored.
        conn.execute(
            "UPDATE nodes SET origin='plugin', state='live' WHERE id = ?",
            (row["id"],),
        )
        unpromoted.append(row["id"])

    if unpromoted:
        conn.commit()
    return unpromoted


def _activate_node(conn, node) -> dict:
    node_id, node_kind, node_name, state = node["id"], node["kind"], node["name"], node["state"]

    if state != "vaulted":
        return {
            "ok": True,
            "action": "none",
            "id": node_id,
            "message": f"{node_id} is already {state}; nothing to activate",
        }

    vault_kind = "skills" if node_kind == "skill" else "agents"
    try:
        vault.restore(node_name, vault_kind)
    except LookupError:
        raise  # ambiguity, or genuinely not vaulted -- rule 2, must surface
    except (FileExistsError, RuntimeError, OSError) as exc:
        # A failure after a durable filesystem change (or one that could be)
        # must be reported, not raised as a bare traceback (rule 5) -- the
        # caller can act on this, a traceback just crashes it.
        return {
            "ok": False,
            "action": "restore",
            "id": node_id,
            "message": f"failed to restore {node_id}: {exc}; reconcile with `tare scan`",
        }

    # The vaulted id and the post-restore live id are the same string for
    # this exact row (both built from the declared name -- see
    # scan._vaulted_node_id / scan._name_and_desc), so flipping this row's
    # state in place keeps the graph immediately consistent instead of
    # leaving it stale until the next `tare scan`.
    conn.execute("UPDATE nodes SET state='live' WHERE id = ?", (node_id,))
    conn.commit()
    # `was` is what the memory needs to tell a genuine shelving mistake from a
    # no-op: an activation from 'vaulted' says the vault got this one wrong,
    # which is the most useful thing this tool can learn about its own
    # judgement. Without it every activation records `was: null` and the signal
    # is silently dropped.
    return {"ok": True, "action": "restore", "id": node_id, "was": "vaulted",
            "message": f"restored {node_id}"}


def _activate_plugin(conn, name: str) -> dict:
    try:
        data = install._load()
    except ValueError as exc:
        return {"ok": False, "action": "none", "id": name, "message": f"settings.json is unreadable: {exc}"}

    enabled = data.get("enabledPlugins")
    if not isinstance(enabled, dict):
        return {
            "ok": False,
            "action": "none",
            "id": name,
            "message": f"{name!r} does not match a vaulted capability or a disabled plugin",
        }

    matches = [
        key for key, value in enabled.items()
        if value is False and (key == name or key.split("@", 1)[0] == name)
    ]
    if not matches:
        return {
            "ok": False,
            "action": "none",
            "id": name,
            "message": f"{name!r} does not match a vaulted capability or a disabled plugin",
        }
    if len(matches) > 1:
        raise LookupError(
            f"{name!r} matches more than one disabled plugin: {matches}; retry with an exact 'plugin@marketplace' key"
        )

    plugin_key = matches[0]
    try:
        enabled[plugin_key] = True
        install._save(data)
    except (ValueError, OSError) as exc:
        return {
            "ok": False,
            "action": "enable-plugin",
            "id": plugin_key,
            "message": f"failed to re-enable {plugin_key}: {exc}",
        }

    unpromoted = _unpromote_plugin(conn, plugin_key)
    message = f"re-enabled plugin {plugin_key}"
    if unpromoted:
        message += (
            f"; un-promoted {len(unpromoted)} skill(s) that were symlinked onto the load path "
            f"so {plugin_key} is not served twice: {', '.join(sorted(unpromoted))}"
        )
    return {"ok": True, "action": "enable-plugin", "id": plugin_key, "was": "plugin-disabled",
            "message": message, "unpromoted": unpromoted}


def activate(conn, name: str) -> dict:
    """Bring `name` (a graph id, a graph name, or a vault filesystem name)
    live again -- a vaulted skill/agent via `vault.restore`, or a disabled
    plugin via `enabledPlugins` (rule 4 also un-promotes anything the
    plugin's promotion had staged, so it isn't served twice).
    """
    rows = _find_nodes(conn, name)

    if len(rows) > 1:
        raise LookupError(f"{name!r} matches more than one capability: {_describe(rows)}; retry with a specific id")

    if rows:
        return _activate_node(conn, rows[0])

    resolved = _resolve_via_vault(name)
    if resolved is not None:
        kind, key = resolved
        node_id = scan._vaulted_node_id(key, kind)
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is not None:
            return _activate_node(conn, row)
        # Genuinely vaulted, but the graph hasn't been scanned since -- restore
        # directly rather than reporting "not found" for something real.
        vault.restore(name, kind)
        return {
            "ok": True,
            "action": "restore",
            "id": node_id,
            "message": f"restored {node_id} (run `tare scan` to refresh the graph)",
        }

    return _activate_plugin(conn, name)


def _deactivate_node(conn, node) -> dict:
    node_id, node_kind, node_name, state = node["id"], node["kind"], node["name"], node["state"]

    if state != "live":
        return {"ok": True, "action": "none", "id": node_id, "message": f"{node_id} is already {state}; nothing to deactivate"}

    if node["origin"] != "user-authored" or node["provider_plugin"]:
        # A plugin-served (or promoted plugin-served) node is not a vault
        # restore. deactivate cannot re-disable a plugin -- that must be
        # stated plainly, never silently no-op'd (docstring rule).
        return {
            "ok": False,
            "action": "none",
            "id": node_id,
            "message": f"{node_id} is served by a plugin, not a vault restore; deactivate cannot re-disable a plugin",
        }

    vault_kind = "skills" if node_kind == "skill" else "agents"
    if not vault.is_stashed(node_name, vault_kind):
        return {"ok": False, "action": "none", "id": node_id, "message": f"{node_id} was not restored from the vault; nothing to deactivate"}

    try:
        vault.unrestore(node_name, vault_kind)
    except LookupError:
        raise
    except (FileExistsError, RuntimeError, OSError) as exc:
        return {
            "ok": False,
            "action": "unrestore",
            "id": node_id,
            "message": f"failed to unrestore {node_id}: {exc}; reconcile with `tare scan`",
        }

    # Same-row state flip as activate's restore path, for the same reason:
    # keep the graph consistent immediately rather than until the next scan.
    conn.execute("UPDATE nodes SET state='vaulted' WHERE id = ?", (node_id,))
    conn.commit()
    return {"ok": True, "action": "unrestore", "id": node_id, "message": f"unrestored {node_id}"}


def deactivate(conn, name: str) -> dict:
    """Reverse a vault restore. Cannot re-disable a plugin -- if `name`
    resolves to a plugin-served capability or an enabled plugin, the result
    says so plainly rather than pretending success or quietly doing
    nothing (the previous build's `deactivate` silently answered "was not
    vaulted" for a genuinely-restored node with a divergent filesystem
    name -- see `_resolve_via_vault` for the fix).
    """
    rows = _find_nodes(conn, name)

    if len(rows) > 1:
        raise LookupError(f"{name!r} matches more than one capability: {_describe(rows)}; retry with a specific id")

    if rows:
        return _deactivate_node(conn, rows[0])

    resolved = _resolve_via_vault(name)
    if resolved is not None:
        kind, key = resolved
        node_id = scan._vaulted_node_id(key, kind)
        vault.unrestore(name, kind)
        return {
            "ok": True,
            "action": "unrestore",
            "id": node_id,
            "message": f"unrestored {node_id} (run `tare scan` to refresh the graph)",
        }

    try:
        data = install._load()
    except ValueError as exc:
        return {"ok": False, "action": "none", "id": name, "message": f"settings.json is unreadable: {exc}"}

    enabled = data.get("enabledPlugins")
    plugin_matches = []
    if isinstance(enabled, dict):
        plugin_matches = [
            key for key, value in enabled.items()
            if value is not False and (key == name or key.split("@", 1)[0] == name)
        ]
    if plugin_matches:
        return {
            "ok": False,
            "action": "none",
            "id": name,
            "message": (
                f"{name!r} looks like an enabled plugin, not a vaulted capability; deactivate cannot "
                "re-disable a plugin"
            ),
        }

    return {"ok": False, "action": "none", "id": name, "message": f"{name!r} was not vaulted; nothing to deactivate"}
