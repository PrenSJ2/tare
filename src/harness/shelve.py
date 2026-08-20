"""Decide what to shelve, and shelve it.

This is the destructive end of harness: a wrong candidate here takes a real
capability out of the operator's working configuration. Three mechanisms,
matching the design notes:

    1. `shelve_user`    -- user-authored skills/agents -> `vault.stash`.
    2. `shelve_plugins` -- wholly-unused plugins -> `enabledPlugins: false`.
    3. `shelve_plugins` -- mixed plugins -> symlink the needed skills onto
       `skills_dir()`, then disable the plugin.

Everything downstream of `candidates()` and `plugin_plan()` trusts what those
two functions decide is safe. Neither of them writes anything; `shelve_user`
and `shelve_plugins` are the only functions in this module that touch disk,
the vault, or settings.json.

## The guard

"Never invoked" is inferred from transcripts, which record capabilities
dispatched *by name* (mine.py). An orchestrator skill dispatches its own
sub-skills itself, so those sub-skills never appear by name in a transcript
and read as never-invoked -- precisely because something else invokes them.
On the machine this was built against, `hyperframes` routes to 20 such
skills and `impeccable` to 17; shelving them out from under a daily-used
orchestrator would be exactly the kind of "confidently wrong" this tool
must not be.

Rule: a capability is NOT a candidate if it is reachable, following
`routes-to` edges only (never `overlaps` -- similarity is not dependency,
and `code-reviewer` overlapping `architect-reviewer` must never protect it),
from any capability with at least one recorded invocation, of ANY origin
(a used plugin skill routing to a user-authored one must still protect it).

`_protected_capabilities` computes this with a LEVELLED multi-source BFS,
not a seed-at-a-time walk. The difference matters: given A(used) -> B(used)
-> C, a seed-at-a-time walk lets A's own traversal step straight through B
(itself a seed) and claim C as A's -- that is the exact bug the previous
build shipped, which labelled all 32 real protected capabilities with one
arbitrary wrong source. Processing every seed's frontier one level at a
time, together, fixes this: B is already `visited` (seeds are pre-added
before any traversal starts, which also makes a self-edge or a cycle back
to a seed a no-op rather than a hang), so when A's level-0 step reaches B
it stops there, and C is only ever discovered from B's own level-1 step --
attributed to B.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from . import buckets, paths, vault

# Cold-remainder-after-protection floor (tokens) below which disabling a
# plugin isn't worth it. Measured on the real configuration this tool was
# built against: 500 tokens captures 94% of the available plugin-disabling
# savings while touching half as many plugins as a floor of 50 would -- a
# floor of 50 would disable a whole plugin (settings.json edit, symlinks,
# the works) to save as little as 54 tokens. Judged on the remainder LEFT
# COLD after used/pinned/protected skills are pulled out to be promoted,
# never on the plugin's whole cold set -- see plugin_plan().
DEFAULT_FLOOR_TOKENS = 500


# ---------------------------------------------------------------------------
# Small shared helpers
# ---------------------------------------------------------------------------


def _used_ids(conn) -> set[str]:
    """Node ids with at least one recorded invocation. `usage` never carries
    a zero-invocation row (mine.py's contract), but the extra WHERE costs
    nothing and means this stays correct even if that ever changes."""
    return {r["node_id"] for r in conn.execute("SELECT node_id FROM usage WHERE invocations > 0")}


def _node_name(conn, node_id: str) -> str:
    row = conn.execute("SELECT name FROM nodes WHERE id = ?", (node_id,)).fetchone()
    return row["name"] if row else node_id


def _require_usage_evidence(conn) -> None:
    """Refuse to apply against a database with zero invocation events. With
    no usage evidence at all, every capability looks equally cold and the
    whole configuration -- pinned exemptions aside -- becomes a candidate.
    Checked unconditionally at the top of every apply path, before any
    candidate is even computed, so it can never be dodged by a plan that
    happens to come out empty. `dry_run` never calls this: a preview must
    still be listable on a machine that has never run `harness mine`.
    """
    (count,) = conn.execute("SELECT COUNT(*) FROM usage").fetchone()
    if count == 0:
        raise RuntimeError(
            "no invocation events recorded in the database -- refusing to apply with zero "
            "usage evidence, since every capability would look equally cold; run `harness mine` first"
        )


# ---------------------------------------------------------------------------
# The guard: routes-to reachability from any used node, levelled BFS
# ---------------------------------------------------------------------------


def _routes_to_adjacency(conn) -> dict[str, list[str]]:
    adj: dict[str, list[str]] = defaultdict(list)
    for row in conn.execute("SELECT src, dst FROM edges WHERE type = 'routes-to'"):
        adj[row["src"]].append(row["dst"])
    return adj


def _protected_capabilities(conn) -> dict[str, str]:
    """node_id -> the used seed's node_id whose routes-to traversal reached
    it first. Only ids NOT themselves in the seed set are returned -- a used
    capability doesn't need to be protected from itself.

    Levelled multi-source BFS: every seed's current frontier is expanded
    together, one hop at a time, rather than exhausting one seed's whole
    reachable set before starting the next. That is what makes the A -> B
    -> C attribution correct (see module docstring) -- a per-seed walk
    would let A's traversal pass straight through the already-used B.

    Seeds are added to `visited` BEFORE any traversal starts, which does
    double duty: it stops A's edge to B from producing a (redundant, wrong)
    proposal in the first place, and it means a self-edge or a cycle back to
    a seed is simply a no-op rather than a fresh discovery -- cycles among
    non-seed nodes also terminate normally, since every node is marked
    visited the moment it is first discovered, before its own neighbours
    are ever considered.
    """
    used = _used_ids(conn)
    adj = _routes_to_adjacency(conn)

    owner: dict[str, str] = {seed: seed for seed in used}
    visited: set[str] = set(used)  # pre-added: rule -- a self-edge cannot loop
    frontier = sorted(used)

    while frontier:
        # Collect every unvisited node reachable from this level's frontier
        # BEFORE marking any of them visited or advancing the frontier --
        # that synchronisation across all seeds at once is the "levelled"
        # part. A node proposed by more than one seed at the same level is
        # attributed deterministically (lexicographically smallest owner);
        # ties like that don't arise in the golden data, but determinism
        # matters more than which arbitrary one wins.
        proposals: dict[str, list[str]] = defaultdict(list)
        for node in frontier:
            for nxt in adj.get(node, ()):
                if nxt in visited:
                    continue
                proposals[nxt].append(owner[node])

        if not proposals:
            break

        next_frontier = []
        for node, owners in proposals.items():
            visited.add(node)
            owner[node] = min(owners)
            next_frontier.append(node)
        frontier = sorted(next_frontier)

    return {node_id: seed for node_id, seed in owner.items() if node_id not in used}


# ---------------------------------------------------------------------------
# Mechanism 1: user-authored skills and agents
# ---------------------------------------------------------------------------


def candidates(conn) -> dict:
    """Every never-invoked, live, user-authored capability, annotated with
    whether it is actually eligible to shelve and, when it is not, why.

    A silent exclusion is its own defect (an operator who knows a skill is
    never invoked must be able to find out why it was left off the list),
    so ineligible capabilities are reported here, not dropped -- each with
    `reason` in {"pinned", "protected", "already-vaulted"} and, for
    "protected", `protected_by` naming the used capability that reaches it.

    Reads `path` straight off `nodes.path` (never derived from `name`):
    deriving it from the name means a capability whose frontmatter `name:`
    differs from its filename -- a real case on the source machine,
    `agents/architect-review.md` declaring `name: architect-reviewer` -- is
    reported "not on disk" forever and can never be shelved.

    A promoted plugin skill (origin='user-authored' but `provider_plugin`
    set) is mechanism 3's territory, not mechanism 1's, and is excluded
    entirely rather than reported ineligible here -- see `plugin_plan()`.
    """
    used = _used_ids(conn)
    protected = _protected_capabilities(conn)

    out: dict[str, list[dict]] = {"skills": [], "agents": []}
    rows = conn.execute(
        "SELECT id, kind, name, path, est_tokens, provider_plugin FROM nodes "
        "WHERE origin = 'user-authored' AND state = 'live'"
    ).fetchall()

    for row in rows:
        node_id, kind, name, path = row["id"], row["kind"], row["name"], row["path"]
        if kind not in ("skill", "agent"):
            continue
        if row["provider_plugin"]:
            continue  # promoted plugin skill -- mechanism 3's territory
        if node_id in used:
            continue  # actually used -- not a "never invoked" candidate at all

        bucket = "skills" if kind == "skill" else "agents"
        vault_kind = bucket
        entry = {
            "id": node_id,
            "kind": kind,
            "name": name,
            "path": path,
            "est_tokens": row["est_tokens"],
            "eligible": True,
            "reason": None,
            "protected_by": None,
        }

        if buckets.is_pinned(name, node_id):
            entry["eligible"], entry["reason"] = False, "pinned"
        elif node_id in protected:
            entry["eligible"], entry["reason"] = False, "protected"
            entry["protected_by"] = _node_name(conn, protected[node_id])
        elif vault.is_stashed(name, vault_kind):
            # Covers both a still-vaulted entry (shouldn't appear here at
            # all, since state would read 'vaulted', but checked anyway for
            # safety) and a RESTORED one -- live again via a vault symlink,
            # state='live', but still an entry `activate`/`deactivate` own,
            # not something mechanism 1 may re-stash out from under them.
            entry["eligible"], entry["reason"] = False, "already-vaulted"

        out[bucket].append(entry)

    return out


def shelve_user(conn, dry_run: bool = True) -> list[dict]:
    """Vault every eligible candidate from `candidates()`. Returns one report
    dict per candidate (eligible or not), so a caller can truthfully say
    "shelved N, failed M, skipped K (and why)".

    `dry_run` defaults to True. On apply, refuses outright with zero usage
    evidence (`_require_usage_evidence`) -- checked before anything else, so
    it fires regardless of what the candidate set happens to contain.
    """
    if not dry_run:
        _require_usage_evidence(conn)

    cand = candidates(conn)
    entries = cand["skills"] + cand["agents"]

    results: list[dict] = []
    for entry in entries:
        report = {
            "id": entry["id"],
            "kind": entry["kind"],
            "name": entry["name"],
            "eligible": entry["eligible"],
            "reason": entry["reason"],
            "protected_by": entry["protected_by"],
            # Always carried, eligible or not: the CLI reports the token cost of
            # protected capabilities too, so the operator can see what the guard
            # is holding back and judge whether it is worth it.
            "est_tokens": entry["est_tokens"],
        }

        if not entry["eligible"]:
            report["status"] = "skipped"
            results.append(report)
            continue

        if dry_run:
            report["status"] = "would-shelve"
            report["est_tokens"] = entry["est_tokens"]
            results.append(report)
            continue

        # Per-capability error handling (requirement 7): stash, update,
        # commit ONE item at a time, so a single bad entry can't stop the
        # sweep, and the caller gets an honest per-item status either way.
        vault_kind = "skills" if entry["kind"] == "skill" else "agents"
        try:
            if not entry["path"]:
                raise FileNotFoundError("node has no recorded path")
            source_file = Path(entry["path"])
            # nodes.path is always the frontmatter-bearing file: the
            # SKILL.md inside the skill directory, or the agent file
            # itself. vault.stash wants the whole skill directory, not
            # just SKILL.md.
            source = source_file.parent if entry["kind"] == "skill" else source_file
            if not source.exists():
                raise FileNotFoundError(f"{source} is not on disk")
            vault.stash(source, vault_kind)
            conn.execute("UPDATE nodes SET state = 'vaulted' WHERE id = ?", (entry["id"],))
            conn.commit()
            report["status"] = "shelved"
        except Exception as exc:
            conn.rollback()
            report["status"] = "failed"
            report["error"] = str(exc)

        results.append(report)

    return results


# ---------------------------------------------------------------------------
# Mechanisms 2 and 3: plugins
# ---------------------------------------------------------------------------


def _plugin_key(provider_plugin: str, marketplace: str | None) -> str:
    """The exact `enabledPlugins` key for a plugin -- matches scan.py's
    `_enabled_plugins()` read side literally (`f"{plugin}@{marketplace}"`),
    since this is the "ownership-scope filter" every read AND write of
    settings.json in this module must agree on. Plugin names repeat across
    marketplaces -- the real cache this was built against has two distinct
    "toolkit" plugins, from two differently-cased marketplace ids -- so
    scoping by plugin name alone would silently touch, or silently skip,
    the wrong marketplace's copy.
    """
    return f"{provider_plugin}@{marketplace}"


def _read_settings() -> dict:
    """Read settings.json. A MISSING file is a legitimate state -- nothing
    has ever explicitly disabled a plugin -- and returns {}, same as
    scan.py's `_enabled_plugins()`. Anything else that prevents reading it
    (corrupt JSON, a permissions error, content that isn't even a JSON
    object) is a FAILURE this module must not paper over: without knowing
    the real enabled/disabled state, any plan computed would be a guess, and
    reporting one anyway is exactly the false authorisation the "faithful
    preview" rule exists to prevent -- so this raises even when only
    previewing, not just on apply.
    """
    path = paths.settings_path()
    if not path.exists():
        return {}
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise RuntimeError(f"settings.json at {path} could not be read: {exc}") from exc
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"settings.json at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"settings.json at {path} is not a JSON object")
    return data


def _write_settings(data: dict) -> None:
    path = paths.settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _enabled_map(settings_data: dict) -> dict:
    raw = settings_data.get("enabledPlugins")
    return raw if isinstance(raw, dict) else {}


def plugin_plan(conn, floor_tokens: int = DEFAULT_FLOOR_TOKENS) -> dict:
    """Compute what mechanisms 2/3 would do, without doing any of it.

    {"disable": [...], "promote": [...]}. A plugin is a candidate to disable
    only if, after pulling every used/pinned/protected skill of its out into
    `promote` (mechanism 3's "widen the promote set rather than blocking the
    disable"), what remains actually cold is worth at least `floor_tokens`
    -- judged on that COLD REMAINDER, never the plugin's whole cold set, so
    a plugin that is 90% used isn't disabled just because its total size is
    large.

    Already-disabled plugins (an explicit `false` in `enabledPlugins`,
    checked via the ownership-scope key from `_plugin_key`) are skipped
    entirely -- nothing to do, not a failure.
    """
    settings_data = _read_settings()
    enabled = _enabled_map(settings_data)
    used = _used_ids(conn)
    protected = _protected_capabilities(conn)

    groups: dict[tuple[str, str], list] = defaultdict(list)
    rows = conn.execute(
        "SELECT id, name, path, est_tokens, provider_plugin, marketplace FROM nodes "
        "WHERE kind = 'skill' AND origin = 'plugin' AND state = 'live' "
        "AND provider_plugin IS NOT NULL AND provider_plugin != ''"
    ).fetchall()
    for row in rows:
        groups[(row["provider_plugin"], row["marketplace"])].append(row)

    disable: list[dict] = []
    promote: list[dict] = []

    for (plugin, marketplace), skill_rows in sorted(groups.items()):
        key = _plugin_key(plugin, marketplace)
        if enabled.get(key, True) is False:
            continue  # already disabled -- nothing to do here

        cold = []
        keep: list[tuple] = []
        for row in skill_rows:
            reason = None
            if row["id"] in used:
                reason = "used"
            elif buckets.is_pinned(row["name"], row["id"]):
                reason = "pinned"
            elif row["id"] in protected:
                reason = "protected"

            if reason:
                keep.append((row, reason))
            else:
                cold.append(row)

        cold_tokens = sum(row["est_tokens"] or 0 for row in cold)
        if cold_tokens < floor_tokens:
            continue  # not worth the churn of disabling this plugin

        disable.append(
            {
                "key": key,
                "provider_plugin": plugin,
                "marketplace": marketplace,
                "cold_tokens": cold_tokens,
                "cold_skills": [row["name"] for row in cold],
            }
        )
        for row, reason in keep:
            protected_by = _node_name(conn, protected[row["id"]]) if reason == "protected" else None
            promote.append(
                {
                    "id": row["id"],
                    "name": row["name"],
                    "path": row["path"],
                    "est_tokens": row["est_tokens"],
                    "plugin_key": key,
                    "provider_plugin": plugin,
                    "marketplace": marketplace,
                    "reason": reason,
                    "protected_by": protected_by,
                }
            )

    return {"disable": disable, "promote": promote}


def _stage_promotion(target: Path, source_dir: Path) -> tuple[Path, bool]:
    """Symlink `target` (under skills_dir()) to `source_dir` (a directory
    inside the plugin cache). Returns (path, created) -- `created` is False
    for an already-correct symlink (idempotent no-op), which matters for
    rollback: only links THIS call actually created should ever be removed.

    Raises FileExistsError -- never silently skips -- for any other
    occupant of `target`: a real directory, an unrelated symlink, or a
    symlink into somewhere else entirely. The previous build skipped the
    symlink here but still flipped the node to origin='user-authored' with
    a nonexistent path, reported it as promoted, and disabled the plugin;
    the next scan pruned the row and cascaded DELETE FROM usage, destroying
    a used capability's invocation history. Raising here, and the caller
    dropping the whole plugin from the disable set on this exception, is
    the fix.
    """
    if target.is_symlink():
        try:
            resolved = target.resolve()
        except OSError:
            resolved = None
        if resolved == source_dir.resolve():
            return target, False
        raise FileExistsError(f"{target} already exists (symlink to {resolved})")
    if target.exists():
        raise FileExistsError(f"{target} already exists and is not a symlink")

    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(source_dir, target_is_directory=True)
    return target, True


def shelve_plugins(conn, dry_run: bool = True, floor_tokens: int = DEFAULT_FLOOR_TOKENS) -> dict:
    """Apply (or preview) `plugin_plan()`. Returns
    {"dry_run", "disabled", "promoted", "failed"}.

    `dry_run` defaults to True, and both modes compute the SAME plan via
    `plugin_plan` -- including the same ownership-scope filter on
    `enabledPlugins` -- so a preview can never claim an action the apply
    path would actually skip.

    Nothing to disable is reported cleanly, not as an error (an empty
    `disable` plan is a perfectly normal outcome once every candidate
    plugin is either already disabled or under `floor_tokens`). An
    unreadable/corrupt settings.json IS an error, in both modes, because
    `plugin_plan` cannot compute a truthful plan without it.
    """
    if not dry_run:
        _require_usage_evidence(conn)

    plan = plugin_plan(conn, floor_tokens=floor_tokens)
    result: dict = {"dry_run": dry_run, "disabled": [], "promoted": [], "failed": []}

    if not plan["disable"]:
        return result  # nothing in scope to do -- not a failure

    if dry_run:
        result["disabled"] = [d["key"] for d in plan["disable"]]
        result["promoted"] = [p["id"] for p in plan["promote"]]
        return result

    # --- apply path ---
    settings_data = _read_settings()
    enabled = settings_data.setdefault("enabledPlugins", {})
    if not isinstance(enabled, dict):
        raise RuntimeError("settings.json 'enabledPlugins' is not a JSON object")

    promote_by_plugin: dict[str, list[dict]] = defaultdict(list)
    for p in plan["promote"]:
        promote_by_plugin[p["plugin_key"]].append(p)

    all_created: list[tuple[dict, Path, bool]] = []  # for full rollback if the settings write fails
    survivors: list[tuple[dict, list[tuple[dict, Path, bool]]]] = []

    for plugin in plan["disable"]:
        key = plugin["key"]
        staged: list[tuple[dict, Path, bool]] = []
        collided = False

        for skill in promote_by_plugin.get(key, []):
            target = paths.skills_dir() / skill["name"]
            source_dir = Path(skill["path"]).parent
            try:
                link, created = _stage_promotion(target, source_dir)
                staged.append((skill, link, created))
            except OSError as exc:
                result["failed"].append({"id": skill["id"], "plugin": key, "action": "promote", "error": str(exc)})
                collided = True
                break

        if collided:
            # Requirement 2: a promotion collision drops the WHOLE plugin
            # from the disable set, not just that one skill -- disabling it
            # anyway would take every other used/protected skill of its
            # dark along with the one that failed to promote. Undo only the
            # links this loop itself just created for this plugin.
            for _skill, link, created in staged:
                if created:
                    link.unlink(missing_ok=True)
            continue

        all_created.extend(staged)
        survivors.append((plugin, staged))

    if not survivors:
        return result

    for plugin, _staged in survivors:
        enabled[plugin["key"]] = False

    try:
        _write_settings(settings_data)
    except OSError as exc:
        # Requirement 9: roll back every symlink staged this run, across
        # every surviving plugin, before raising -- the settings write is
        # the point of no return, and nothing after it is allowed to have
        # already happened.
        for _skill, link, created in all_created:
            if created:
                link.unlink(missing_ok=True)
        raise RuntimeError(f"failed to write settings.json: {exc}") from exc

    # Settings write succeeded -- only now commit DB changes, per item
    # (requirement 7/9), so one bad row can't undo the settings write or
    # stop the rest of the sweep.
    for plugin, staged in survivors:
        result["disabled"].append(plugin["key"])
        for skill, link, _created in staged:
            try:
                conn.execute(
                    "UPDATE nodes SET origin = 'user-authored', path = ? WHERE id = ?",
                    (str(link / "SKILL.md"), skill["id"]),
                )
                conn.commit()
                result["promoted"].append(skill["id"])
            except Exception as exc:
                conn.rollback()
                result["failed"].append({"id": skill["id"], "plugin": plugin["key"], "action": "db-update", "error": str(exc)})

    return result
