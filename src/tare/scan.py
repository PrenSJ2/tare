"""Filesystem -> `nodes` rows.

This was the most defect-prone file in the previous build -- five separate
rounds of fixes, each found only after the previous one shipped (see
the design notes). Every rule below is written in from the start
because of that history, not as a nice-to-have; each has a matching test
named after the failure it prevents.

Four scanners, one graph:
    scan_user_skills   ~/.claude/skills   -> origin='user-authored'
    scan_agents        ~/.claude/agents   -> origin='user-authored'
    scan_plugin_skills  plugin cache      -> origin='plugin'
    scan_vaulted        vault manifest    -> origin='user-authored', state='vaulted'

The four namespaces overlap by design (a promoted plugin skill lives in
`skills_dir()` as a symlink but keeps a plugin-scoped id; a vaulted skill's
id must match exactly what its live row would have been) -- see rule 3 and
rule 4 for how that overlap is kept from drifting apart.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import frontmatter, paths, vault

# Columns only tag.py ever populates. Left out of a normal upsert entirely
# (ON CONFLICT then leaves them exactly as tag.py set them -- scan.py has no
# opinion on tags). Included -- and blanked -- only when the file backing a
# node is gone, unreadable, or fails to parse: otherwise a node whose file
# vanished keeps serving a description and tags for content nobody can see
# the source of any more (rule 8).
_CLEAR_DERIVED = {
    "purpose_line": "",
    "when_to_use": "",
    "tags": "",
    "tag_source": None,
    "content_hash": None,
}


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _read_text(path: Path) -> str:
    # errors="replace" is load-bearing (rule 10): strict UTF-8 decoding
    # raises UnicodeDecodeError, which is NOT an OSError, so it would escape
    # uncaught through every recovery path below that only guards OSError.
    return path.read_text(encoding="utf-8", errors="replace")


def _load(path: Path) -> tuple[frontmatter.Frontmatter | None, int, str | None]:
    """Read and parse `path`'s frontmatter. Never raises: an unreadable file
    is just another parse_error, the same shape as bad YAML, so every
    caller has exactly one failure to handle instead of two.

    Returns (frontmatter-or-None, est_tokens, parse_error).

    est_tokens measures the ALWAYS-LOADED cost, which is the name and
    description Claude Code lists in every prompt -- NOT the whole file. The
    body is only read once a capability is actually invoked, so charging it
    to every turn overstates the index by roughly twenty times: measured on a
    real configuration, whole-file accounting reported 411,782 tokens per turn
    against a true 19,437, and that inflation then dragged two extra plugins
    over the savings floor. When the frontmatter cannot be parsed there is no
    description to charge, but the file is still listed, so fall back to the
    filename's cost rather than the body's.
    """
    try:
        text = _read_text(path)
    except OSError as exc:
        return None, 0, f"could not read: {exc}"
    fm, err = frontmatter.parse(text)
    loaded = f"{fm.name}: {fm.description}" if fm else path.name
    return fm, paths.est_tokens(loaded), err


def _name_and_desc(fm: frontmatter.Frontmatter | None, fallback: str) -> tuple[str, str]:
    """The one place `name:` falls back to a filesystem name -- used for
    live skills/agents, plugin skills, and (via vault._declared_name) for
    vaulted entries too."""
    name = (fm.name if fm and fm.name else "") or fallback
    desc = fm.description if fm else ""
    return name, desc


def _cache_root() -> Path:
    return paths.plugins_cache_dir().resolve(strict=False)


def _vault_root() -> Path:
    return paths.vault_dir().resolve(strict=False)


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_link(entry: Path) -> tuple[Path | None, bool]:
    """Fully resolve a symlink -- following relative links and chains -- for
    classification (rule 4). Returns (resolved_path, exists). `resolved_path`
    is None only when resolution itself blows up (a symlink loop); a target
    that plain does not exist still resolves to a path, just one that
    `.exists()` reports False for, which is exactly the dangling case.
    """
    try:
        resolved = entry.resolve(strict=False)
    except (OSError, RuntimeError):
        return None, False
    return resolved, resolved.exists()


def _vaulted_node_id(key: str, kind: str) -> str:
    """The id a vaulted manifest entry gets -- and the ONLY place that
    computation happens (rule 3). `_protected_ids` (below) and
    `scan_vaulted` both call this exact function; a near-miss between two
    independent implementations would mean the protected set matches no
    row, silently protecting nothing, and rule 1's bug recurs.

    Falls back to the filesystem key when the frontmatter will not parse --
    the same rule `scan_vaulted` uses for its own parse_error branch,
    reused via `vault._declared_name` rather than re-derived here.
    """
    name = vault._declared_name(key, kind) or key
    prefix = "skill" if kind == "skills" else "agent"
    return f"{prefix}:{name}"


def _protected_ids(kind: str) -> set[str]:
    """Ids that must survive a prune even though their files are
    intentionally absent from the live tree.

    Read straight from the vault manifest, never from `nodes.state`:
    guarding a prune on `state != 'vaulted'` looks sufficient but is not --
    on the very first scan after a stash the row still reads 'live',
    because nothing has told it otherwise yet (`scan_vaulted` is what flips
    it, and it may not have run yet this pass). Consulting the manifest
    directly sidesteps the ordering problem entirely (rule 1).

    Gated on `is_initialized()` (rule 2): a plain scan on a machine that has
    never used the vault must not create one just by running. Called before
    any writes in the scanner that uses it, so a corrupt manifest aborts
    cleanly (raises) rather than after deletions have already committed.

    Entries flagged `restored: True` are excluded: they are live again via
    a symlink back in skills_dir/agents_dir, so the corresponding live
    scanner produces their `live` row directly -- protecting the vaulted id
    on top of that would just be protecting an id nothing is trying to
    prune.
    """
    if not vault.is_initialized():
        return set()
    data = vault.manifest()  # raises ValueError on corruption; nothing written yet
    return {
        _vaulted_node_id(key, kind)
        for key, entry in data.get(kind, {}).items()
        if not entry.get("restored")
    }


def _assign_id(candidate_id: str, fallback_id: str, used_ids: set[str]) -> tuple[str, bool]:
    """Guarantee every node a scan produces this pass gets a unique id.

    `candidate_id` is the "natural" id, built from a declared frontmatter
    `name:`. `fallback_id` is derived from the filesystem name alone, which
    is unique among siblings by construction (rule 9): two directories
    cannot share one filename in the same folder, even though they can
    declare the same frontmatter name. Falls through to a numeric suffix
    only in the pathological case where a frontmatter name collides with a
    *different* entry's filesystem name -- rare, but silently dropping a
    node is worse than an ugly id.
    """
    if candidate_id not in used_ids:
        used_ids.add(candidate_id)
        return candidate_id, False
    if fallback_id not in used_ids:
        used_ids.add(fallback_id)
        return fallback_id, True
    n = 2
    while f"{fallback_id}~{n}" in used_ids:
        n += 1
    final = f"{fallback_id}~{n}"
    used_ids.add(final)
    return final, True


def _upsert(
    conn,
    *,
    id,
    kind,
    name,
    path,
    origin,
    state,
    desc_raw,
    est_tokens,
    parse_error,
    provider_plugin=None,
    marketplace=None,
    clear_derived=False,
) -> None:
    """Insert-or-update one node row.

    `ON CONFLICT` only updates the columns explicitly passed -- that is
    exactly why `clear_derived` exists as a separate opt-in rather than
    something inferred here: leaving tag.py's columns out of the normal
    path is what lets scan.py and tag.py coexist without one clobbering the
    other, and including them (blanked) is what stops a node whose file
    disappeared from serving stale content forever (rule 8).
    """
    fields = {
        "id": id,
        "kind": kind,
        "name": name,
        "path": str(path) if path else None,
        "origin": origin,
        "state": state,
        "provider_plugin": provider_plugin,
        "marketplace": marketplace,
        "desc_raw": desc_raw,
        "est_tokens": est_tokens,
        "parse_error": parse_error,
    }
    if clear_derived:
        fields.update(_CLEAR_DERIVED)

    cols = list(fields)
    placeholders = ", ".join(f":{c}" for c in cols)
    updates = ", ".join(f"{c}=excluded.{c}" for c in cols if c != "id")
    conn.execute(
        f"INSERT INTO nodes ({', '.join(cols)}) VALUES ({placeholders}) "
        f"ON CONFLICT(id) DO UPDATE SET {updates}",
        fields,
    )


def _prune(conn, *, kind: str, origin: str, keep: set[str], state: str | None = None, exclude_states: tuple = ()) -> int:
    """Delete nodes a scanner owns but no longer finds on disk, except
    anything in `keep`.

    Scoped by `kind`/`origin`/`state` ONLY -- never by id shape. A previous
    build scoped this with `id LIKE '%@%'`, which would delete a legitimate
    user skill literally named `vendor@thing` (rule 1). `state`/
    `exclude_states` exist so two different scanners can each own a
    disjoint slice of the same (kind, origin) pair -- e.g. scan_user_skills
    owns the 'live' rows of ('skill', 'user-authored') and must never touch
    the 'vaulted' rows of that same pair, which scan_vaulted owns.
    """
    query = "SELECT id FROM nodes WHERE kind = ? AND origin = ?"
    params: list = [kind, origin]
    if state is not None:
        query += " AND state = ?"
        params.append(state)
    for st in exclude_states:
        query += " AND state != ?"
        params.append(st)

    rows = conn.execute(query, params).fetchall()
    stale = [r["id"] for r in rows if r["id"] not in keep]
    if stale:
        conn.executemany("DELETE FROM nodes WHERE id = ?", [(i,) for i in stale])
    return len(stale)


# ---------------------------------------------------------------------------
# Symlink classification (rule 4)
# ---------------------------------------------------------------------------


def _classify_skill_symlink(entry: Path) -> dict:
    """Classify a symlink living directly in `skills_dir()`. Four cases,
    unambiguous (rule 4) -- and classified by resolved TARGET LOCATION
    FIRST, existence checked only afterward. `resolve(strict=False)`
    returns the theoretical target even when nothing is there, so a
    promoted skill whose plugin version directory was removed by
    `/plugin update` still resolves into the cache and must keep its
    plugin-scoped id (and usage rows) -- losing it here is the promote ->
    shelve loop again, just triggered by a missing target instead of a
    wrong id derivation. Same reasoning for a dangling restore into the
    vault: it stays 'user-authored'. Only a link that resolves outside
    both roots -- dangling or not -- is 'external-tool'.

      1. resolves into plugins_cache_dir -> promoted; plugin-scoped id, so
         its usage rows survive.
      2. resolves into vault_dir         -> restored; origin='user-authored',
         not external-tool -- it is a live capability again.
      3. anything else                   -> external-tool (agent-browser,
         find-skills are real examples on this machine and must keep
         classifying this way).
      4. target does not exist (any of the above) -> still emit the node
         from case 1-3's classification, just with parse_error='dangling
         symlink' and no readable content. Never let it vanish.
    """
    resolved, exists = _resolve_link(entry)
    fs_name = entry.name

    if resolved is None:
        # Symlink loop -- resolution itself failed, so there is no target
        # path to classify by at all. Distinct from "dangling", which still
        # has a (non-existent) target location to attribute.
        return {
            "id": f"skill:{fs_name}",
            "name": fs_name,
            "origin": "external-tool",
            "desc_raw": "",
            "est_tokens": 0,
            "clear_derived": True,
            "parse_error": "broken symlink (resolution failed)",
        }

    if _is_within(resolved, _cache_root()):
        info = _promoted_skill_node(entry, resolved)
    elif _is_within(resolved, _vault_root()):
        fm, tokens, err = _load(resolved / "SKILL.md")
        name, desc = _name_and_desc(fm, fs_name)
        info = {
            "id": f"skill:{name}",
            "name": name,
            "origin": "user-authored",
            "desc_raw": desc,
            "est_tokens": tokens,
            "clear_derived": fm is None,
            "parse_error": err,
        }
    else:
        fm, tokens, err = _load(resolved / "SKILL.md")
        name, desc = _name_and_desc(fm, fs_name)
        info = {
            "id": f"skill:{name}",
            "name": name,
            "origin": "external-tool",
            "desc_raw": desc,
            "est_tokens": tokens,
            "clear_derived": fm is None,
            "parse_error": err,
        }

    if not exists:
        info = {**info, "clear_derived": True, "desc_raw": "", "est_tokens": 0, "parse_error": "dangling symlink"}
    return info


def _promoted_skill_node(entry: Path, resolved: Path) -> dict:
    """A symlink in skills_dir() that resolves inside the plugin cache: this
    skill has been promoted (the design notes mechanism 3), and its usage
    history lives under the plugin-scoped id, not `skill:<name>`. Rebuild
    that id from the marketplace/plugin path components rather than
    inventing a fresh one -- a fresh id here is exactly the promote ->
    shelve-because-unused -> promote loop the notes describe.

    The real cache layout, confirmed against the pre-loss golden database,
    is `<marketplace>/<plugin>/<version>/skills/<relpath>` -- e.g.
    `ExampleMarket/toolkit/0.1.0/skills/asset/SKILL.md`. `version` (often
    semver, sometimes the literal string "unknown") and the `skills`
    segment both have to be stripped: the id is `skill:<plugin>@
    <marketplace>:<relpath>`, not `skill:<plugin>@<marketplace>:
    <version>/skills/<relpath>` -- the version is not part of a plugin's
    identity here (there is no version column in `nodes` at all), it is
    just an artifact of how the cache is laid out on disk.
    """
    try:
        rel_parts = resolved.relative_to(_cache_root()).parts
    except ValueError:
        rel_parts = ()

    if len(rel_parts) < 5 or rel_parts[3] != "skills":
        # Too shallow, or doesn't match <marketplace>/<plugin>/<version>/
        # skills/<relpath> -- can't attribute it, so fall back to
        # external-tool rather than emit a malformed plugin-scoped id.
        fm, tokens, err = _load(resolved / "SKILL.md")
        name, desc = _name_and_desc(fm, entry.name)
        note = "symlink resolves into plugins cache but path does not match <marketplace>/<plugin>/<version>/skills/<relpath>"
        return {
            "id": f"skill:{name}",
            "name": name,
            "origin": "external-tool",
            "desc_raw": desc,
            "est_tokens": tokens,
            "clear_derived": fm is None,
            "parse_error": f"{err}; {note}" if err else note,
        }

    marketplace, plugin, _version, _skills, *rest = rel_parts
    relpath = "/".join(rest)
    fm, tokens, err = _load(resolved / "SKILL.md")
    name, desc = _name_and_desc(fm, entry.name)
    return {
        "id": f"skill:{plugin}@{marketplace}:{relpath}",
        "name": name,
        "origin": "user-authored",
        "provider_plugin": plugin,
        "marketplace": marketplace,
        "desc_raw": desc,
        "est_tokens": tokens,
        "clear_derived": fm is None,
        "parse_error": err,
    }


def _classify_agent_symlink(entry: Path) -> dict:
    """Same classification as `_classify_skill_symlink`, minus the promoted
    case: there is no plugin-scoped id shape for agents in this system, so
    a symlink into the plugin cache falls through to external-tool. Same
    target-location-first, existence-checked-after ordering (rule 4): a
    dangling restore into the vault must still read 'user-authored'.
    """
    resolved, exists = _resolve_link(entry)
    fs_name = entry.stem

    if resolved is None:
        return {
            "id": f"agent:{fs_name}",
            "name": fs_name,
            "origin": "external-tool",
            "desc_raw": "",
            "est_tokens": 0,
            "clear_derived": True,
            "parse_error": "broken symlink (resolution failed)",
        }

    origin = "user-authored" if _is_within(resolved, _vault_root()) else "external-tool"
    fm, tokens, err = _load(resolved)
    name, desc = _name_and_desc(fm, fs_name)
    info = {
        "id": f"agent:{name}",
        "name": name,
        "origin": origin,
        "desc_raw": desc,
        "est_tokens": tokens,
        "clear_derived": fm is None,
        "parse_error": err,
    }

    if not exists:
        info = {**info, "clear_derived": True, "desc_raw": "", "est_tokens": 0, "parse_error": "dangling symlink"}
    return info


# ---------------------------------------------------------------------------
# scan_user_skills / scan_agents
# ---------------------------------------------------------------------------


def scan_user_skills(conn) -> int:
    """`~/.claude/skills` -> nodes with origin='user-authored' (or
    'external-tool'/'plugin'-attributed-but-user-authored for symlinks --
    see the classifier above).
    """
    protected = _protected_ids("skills")  # built first, before any writes: rule 1 + rule 2

    root = paths.skills_dir()
    entries = sorted(root.iterdir(), key=lambda p: p.name) if root.is_dir() else []

    found_ids: set[str] = set()
    used_ids: set[str] = set()

    for entry in entries:
        if entry.is_symlink():
            info = _classify_skill_symlink(entry)
        elif entry.is_dir():
            fm, tokens, err = _load(entry / "SKILL.md")
            name, desc = _name_and_desc(fm, entry.name)
            info = {
                "id": f"skill:{name}",
                "name": name,
                "origin": "user-authored",
                "desc_raw": desc,
                "est_tokens": tokens,
                "clear_derived": fm is None,
                "parse_error": err,
            }
        else:
            continue  # skills_dir() should only ever hold dirs or symlinks

        final_id, collided = _assign_id(info["id"], f"skill:{entry.name}", used_ids)
        if collided:
            note = f"duplicate name {info['name']!r}; disambiguated from {info['id']!r}"  # rule 9
            info["parse_error"] = f"{info['parse_error']}; {note}" if info["parse_error"] else note

        _upsert(
            conn,
            id=final_id,
            kind="skill",
            name=info["name"],
            path=entry / "SKILL.md",  # path is always the frontmatter-bearing file, never the directory
            origin=info["origin"],
            state="live",
            provider_plugin=info.get("provider_plugin"),
            marketplace=info.get("marketplace"),
            desc_raw=info["desc_raw"],
            est_tokens=info["est_tokens"],
            parse_error=info["parse_error"],
            clear_derived=info["clear_derived"],
        )
        found_ids.add(final_id)

    _prune(conn, kind="skill", origin="user-authored", keep=found_ids | protected, exclude_states=("vaulted",))
    conn.commit()
    return len(found_ids)


def scan_agents(conn) -> int:
    """`~/.claude/agents` -> nodes with origin='user-authored' (or
    'external-tool' for a foreign symlink)."""
    protected = _protected_ids("agents")

    root = paths.agents_dir()
    entries = sorted(root.iterdir(), key=lambda p: p.name) if root.is_dir() else []

    found_ids: set[str] = set()
    used_ids: set[str] = set()

    for entry in entries:
        if entry.is_symlink():
            info = _classify_agent_symlink(entry)
            fallback = entry.stem
        elif entry.is_file() and entry.suffix == ".md":
            fm, tokens, err = _load(entry)
            name, desc = _name_and_desc(fm, entry.stem)
            info = {
                "id": f"agent:{name}",
                "name": name,
                "origin": "user-authored",
                "desc_raw": desc,
                "est_tokens": tokens,
                "clear_derived": fm is None,
                "parse_error": err,
            }
            fallback = entry.stem
        else:
            continue  # not an agent definition

        final_id, collided = _assign_id(info["id"], f"agent:{fallback}", used_ids)
        if collided:
            note = f"duplicate name {info['name']!r}; disambiguated from {info['id']!r}"
            info["parse_error"] = f"{info['parse_error']}; {note}" if info["parse_error"] else note

        _upsert(
            conn,
            id=final_id,
            kind="agent",
            name=info["name"],
            path=entry,
            origin=info["origin"],
            state="live",
            desc_raw=info["desc_raw"],
            est_tokens=info["est_tokens"],
            parse_error=info["parse_error"],
            clear_derived=info["clear_derived"],
        )
        found_ids.add(final_id)

    _prune(conn, kind="agent", origin="user-authored", keep=found_ids | protected, exclude_states=("vaulted",))
    conn.commit()
    return len(found_ids)


# ---------------------------------------------------------------------------
# scan_plugin_skills
# ---------------------------------------------------------------------------


def _plugin_skill_dirs(skills_root: Path) -> list[Path]:
    """Every skill directory under a plugin version's `skills/` root,
    however deep.

    Must use `rglob`, not a shallow glob: a plain `*/SKILL.md` missed 22 of
    113 real plugin skills nested arbitrarily deep (e.g.
    `swmansion/skills/0.1.0/skills/detour/migrate-to-detour/`, i.e.
    `detour/migrate-to-detour` under that plugin's `skills/`). But a bare
    rglob then over-corrects and also picks up SKILL.md files that are
    really reference *documents* belonging to another skill (13 of those on
    the same corpus, under paths like `references/*/SKILL.md`). A directory
    only counts as a genuine skill if no *other* skill directory found here
    is one of its ancestors (rule 7).
    """
    all_dirs = sorted({p.parent for p in skills_root.rglob("SKILL.md")})
    return [d for d in all_dirs if not any(other != d and other in d.parents for other in all_dirs)]


def _version_key(version: str) -> tuple:
    """Sort key for a plugin's version directory names. Dotted numeric
    components compare numerically -- plain string sort would rank "0.10.0"
    below "0.9.0" -- and a non-numeric component (the literal "unknown",
    used when a plugin ships without version metadata) sorts after any
    numeric one at the same position, so a real version always wins over
    "unknown" if both are somehow present.
    """
    return tuple((0, int(part)) if part.isdigit() else (1, part) for part in version.split("."))


def _plugin_skills_root(plugin_dir: Path) -> Path | None:
    """Which version directory's `skills/` to scan for this plugin.

    Real layout (confirmed against the pre-loss golden database):
    `<plugin_dir>/<version>/skills/...`. `version` is usually semver but
    can be the literal string "unknown". A plugin can have more than one
    version directory present at once (e.g. left behind by `/plugin
    update`); only the highest-versioned one with a `skills/` subdirectory
    is scanned -- the id shape has no version component, so scanning more
    than one would either double-count identical relpaths under a single
    id (last-write-wins, silently dropping one) or require inventing an id
    shape nothing else in this system expects. Older version directories
    are simply ignored, not merged.
    """
    candidates = [d for d in plugin_dir.iterdir() if d.is_dir() and (d / "skills").is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda d: _version_key(d.name)) / "skills"


def _enabled_plugins() -> dict:
    """`{"<plugin>@<marketplace>": bool}` read from settings.json's
    `enabledPlugins`. A missing file, a missing key, or a plugin simply not
    mentioned all mean "enabled" -- shelving a wholly-unused plugin
    (the design notes mechanism 2) works by writing an explicit `false`, never
    by omission, so absence must default to enabled or every installed-but-
    never-configured plugin would read as disabled.
    """
    try:
        text = _read_text(paths.settings_path())
    except OSError:
        return {}
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return {}
    raw = data.get("enabledPlugins") if isinstance(data, dict) else None
    return raw if isinstance(raw, dict) else {}


def scan_plugin_skills(conn) -> int:
    """Plugin cache -> nodes with origin='plugin', state='live' or
    'plugin-disabled' depending on settings.json.

    No vault interaction anywhere in here -- the vault only ever holds
    user-authored capabilities (the design notes), so there is nothing here to
    gate on `is_initialized()` for.
    """
    # Ids already claimed by a promotion. scan_user_skills upserts a
    # promoted skill's node with origin='user-authored' at exactly this
    # plugin-scoped id; re-claiming it here as 'plugin'/'plugin-disabled'
    # would undo every promotion on the very next scan (rule 6).
    claimed = {r["id"] for r in conn.execute("SELECT id FROM nodes WHERE origin = 'user-authored'")}

    enabled = _enabled_plugins()
    found_ids: set[str] = set()

    cache_root = paths.plugins_cache_dir()
    if cache_root.is_dir():
        for marketplace_dir in sorted(p for p in cache_root.iterdir() if p.is_dir()):
            marketplace = marketplace_dir.name
            for plugin_dir in sorted(p for p in marketplace_dir.iterdir() if p.is_dir()):
                plugin = plugin_dir.name
                skills_root = _plugin_skills_root(plugin_dir)
                if skills_root is None:
                    continue

                for skill_dir in _plugin_skill_dirs(skills_root):
                    relpath = skill_dir.relative_to(skills_root).as_posix()
                    node_id = f"skill:{plugin}@{marketplace}:{relpath}"
                    if node_id in claimed:
                        continue  # rule 6

                    fm, tokens, err = _load(skill_dir / "SKILL.md")
                    name, desc = _name_and_desc(fm, skill_dir.name)
                    state = "live" if enabled.get(f"{plugin}@{marketplace}", True) else "plugin-disabled"

                    _upsert(
                        conn,
                        id=node_id,
                        kind="skill",
                        name=name,
                        path=skill_dir / "SKILL.md",
                        origin="plugin",
                        state=state,
                        provider_plugin=plugin,
                        marketplace=marketplace,
                        desc_raw=desc,
                        est_tokens=tokens,
                        parse_error=err,
                        clear_derived=fm is None,
                    )
                    found_ids.add(node_id)

    _prune(conn, kind="skill", origin="plugin", keep=found_ids)
    conn.commit()
    return len(found_ids)


# ---------------------------------------------------------------------------
# scan_vaulted
# ---------------------------------------------------------------------------


def scan_vaulted(conn) -> int:
    """Vault manifest -> nodes with origin='user-authored', state='vaulted'.

    Gated on `is_initialized()` (rule 2): a plain scan on a machine that has
    never used the vault must not create one just by running.
    """
    if not vault.is_initialized():
        return 0

    data = vault.manifest()  # raises on corruption; nothing written yet
    count = 0

    for kind in vault.KINDS:
        node_kind = "skill" if kind == "skills" else "agent"
        found_ids: set[str] = set()

        for key in sorted(data.get(kind, {})):
            entry = data[kind][key]
            if entry.get("restored"):
                # Live again via a restore symlink -- scan_user_skills /
                # scan_agents produces the 'live' row for this id directly.
                # Re-adding it here as 'vaulted' would have `lookup` tell
                # the operator to activate something already active
                # (rule 5).
                continue

            source = vault._manifest_source_path(key, kind)
            fm, tokens, err = _load(source)
            name, desc = _name_and_desc(fm, key)
            node_id = _vaulted_node_id(key, kind)  # same helper _protected_ids uses (rule 3)

            _upsert(
                conn,
                id=node_id,
                kind=node_kind,
                name=name,
                path=source,  # the frontmatter-bearing file itself, matching every other scanner's convention
                origin="user-authored",
                state="vaulted",
                desc_raw=desc,
                est_tokens=tokens,
                parse_error=err,
                clear_derived=fm is None,
            )
            found_ids.add(node_id)
            count += 1

        _prune(conn, kind=node_kind, origin="user-authored", keep=found_ids, state="vaulted")

    conn.commit()
    return count
