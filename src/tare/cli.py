"""Command line surface.

Two rules govern the destructive commands here, both learned the hard way:

- **A dry run must be a faithful preview.** It is what an operator reads before
  authorising a change to their working configuration, so it must not claim
  actions the apply path would skip. Both modes go through the same planning
  code for exactly that reason.
- **Errors read as errors.** A failed item, or a check that could not be
  performed, exits non-zero. A previous build printed a warning and returned 0,
  which trains an operator to ignore the exit code entirely.
"""

from __future__ import annotations

import argparse
import sys
from collections import Counter

from . import (
    activate as activate_mod,
    audit as audit_mod,
    buckets,
    console as console_mod,
    db,
    doctor as doctor_mod,
    edges,
    install as install_mod,
    lookup as lookup_mod,
    memory as memory_mod,
    mine as mine_mod,
    paths,
    scan,
    shelve as shelve_mod,
    update as update_mod,
    viewer as viewer_mod,
    tag as tag_mod,
)


def _cmd_scan(conn, args) -> int:
    # scan_vaulted runs FIRST as a second line of defence: the prunes are
    # independently vault-aware, but a manifest failure here aborts before any
    # destructive work rather than after it.
    vaulted = scan.scan_vaulted(conn)
    skills = scan.scan_user_skills(conn)
    agents = scan.scan_agents(conn)
    plugin = scan.scan_plugin_skills(conn)
    print(f"scanned {skills + plugin} skills ({plugin} from plugins), {agents} agents, {vaulted} vaulted")
    return 0


def _cmd_mine(conn, args) -> int:
    r = mine_mod.mine(conn)
    print(f"mined {r.transcripts} transcripts -> {r.invocations} invocations")
    # Degrade and report: an operator told "905 invocations" needs to know what
    # was dropped to get there.
    if r.unmatched:
        shown = ", ".join(r.unmatched_names[:6])
        more = f" (+{r.unmatched - 6} more)" if r.unmatched > 6 else ""
        print(f"  {r.unmatched} distinct name(s) matched no installed capability: {shown}{more}")
        print("    built-in agent types (general-purpose, Explore, fork) are expected here")
    if r.malformed:
        print(f"  {r.malformed} malformed line(s) skipped")
    if r.unreadable:
        print(f"  {r.unreadable} transcript(s) could not be read")
    if r.excluded:
        print(f"  {r.excluded} transcripts excluded as tare's own tagging exhaust")
    return 0


def _cmd_tag(conn, args) -> int:
    r = tag_mod.tag_all(conn)
    print(f"tagged {r.tagged}, cached {r.cached}, failed {r.failed}")
    return 1 if r.failed else 0


def _cmd_build(conn, args) -> int:
    for step in (_cmd_scan, _cmd_mine, _cmd_tag):
        step(conn, args)
    print(f"edges: {edges.build(conn)}")
    print("buckets: " + ", ".join(f"{k}={v}" for k, v in sorted(buckets.classify(conn).items())))
    print(f"indexed: {lookup_mod.reindex(conn)}")
    return 0


def _cmd_lookup(conn, args) -> int:
    results = lookup_mod.lookup(conn, args.query, limit=args.limit)
    # Recorded before rendering, and never allowed to break the search itself:
    # the point of the tool is answering the question, not bookkeeping.
    try:
        memory_mod.record_lookup(conn, args.query, results)
    except Exception:
        pass
    if not results:
        print("nothing matched")
        return 0
    for r in results:
        state = "" if r.state == "live" else f" · {r.state}"
        print(f"\n{r.name}    [{r.invocations} uses{state}]")
        if r.purpose_line:
            print(f"  for: {r.purpose_line}")
        if r.when_to_use:
            print(f"  use: {r.when_to_use}")
        for chain in getattr(r, "chains", [])[:3]:
            print(f"  via: {chain}")
        if r.state == "vaulted":
            print(f"  shelved -- `tare activate {r.name}` brings it back")
    return 0


def _cmd_audit(conn, args) -> int:
    print(audit_mod.render(audit_mod.audit(conn)))
    return 0


def _cmd_install(conn, args) -> int:
    install_mod.install()
    print(f"skill written to {paths.skill_install_path()}")
    print(f"hook registered on SessionStart in {paths.settings_path()}")
    print("\nHooks are live-reloaded; no restart needed.")
    return 0


def _cmd_uninstall(conn, args) -> int:
    install_mod.uninstall()
    print("skill and hook removed; the vault is untouched")
    print("Shelved capabilities stay in the vault. Bring one back with:")
    print("  tare activate <name>")
    return 0


def _cmd_hookline(conn, args) -> int:
    """Injected into every session at SessionStart.

    A bare count ("64 capabilities are shelved") is too passive to act on: it
    says something exists without saying what, so the reader has no reason to
    look. This emits the SHAPE of what is hidden -- the domains, drawn from the
    shelved capabilities' own tags -- plus the two commands. Knowing that
    "rust, terraform, security auditing" are behind the curtain is what makes
    someone check before concluding nothing covers a task.

    It is kept to a few dozen tokens on purpose. This runs on every turn of
    every session, and the whole point of the vault was to get ~9,600 tokens
    per turn back; spending a hundred of them to make the rest reachable is a
    good trade, spending a thousand is not.

    Wrapped in a bare except: this runs uninvited inside the operator's
    session, so a traceback here would land in their work. Silence is the
    correct failure mode for this command and this command only.
    """
    try:
        # Best effort, silent, non-blocking: if the viewer is already up this
        # is a single refused-or-accepted socket check. Set TARE_NO_VIEWER
        # to opt out entirely.
        viewer_mod.ensure()
    except Exception:
        pass

    try:
        rows = conn.execute(
            "SELECT kind, tags FROM nodes WHERE state = 'vaulted'"
        ).fetchall()
        if not rows:
            return 0

        kinds = Counter(r["kind"] for r in rows)
        shape = ", ".join(f"{n} {k}s" for k, n in sorted(kinds.items()))

        tags = Counter(
            tag.strip()
            for row in rows
            for tag in (row["tags"] or "").split(",")
            if tag.strip()
        )
        domains = ", ".join(tag for tag, _ in tags.most_common(12))

        print(
            f"tare: {len(rows)} capabilities ({shape}) are shelved and are NOT "
            f"listed anywhere in this context."
        )
        if domains:
            print(f"They cover: {domains}.")
        print(
            "Before concluding that no skill or subagent exists for a task, run "
            '`tare lookup "<what you need>"`. Bring one back with '
            "`tare activate <name>` -- it stays available for the rest of the session."
        )
    except Exception:
        pass
    return 0


def _cmd_activate(conn, args) -> int:
    try:
        result = activate_mod.activate(conn, args.name)
    except (LookupError, FileNotFoundError, FileExistsError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result.get("failed"):
        print(f"partly applied: {result['failed']}", file=sys.stderr)
        print("Run `tare scan` to reconcile the index.", file=sys.stderr)
        return 1
    if result.get("already_live"):
        print(f"{args.name} is already live")
    else:
        print(f"restored {args.name}")
        # An activation from 'vaulted' is the record saying a shelving
        # decision was wrong -- the single most useful thing this tool can
        # learn about its own judgement.
        try:
            memory_mod.record_activation(
                conn, result.get("id") or args.name, args.name, was=result.get("was")
            )
        except Exception:
            pass
    for gone in result.get("unpromoted", []):
        print(f"  un-promoted {gone} -- its plugin serves it again")
    return 0


def _cmd_deactivate(conn, args) -> int:
    try:
        result = activate_mod.deactivate(conn, args.name)
    except (LookupError, FileNotFoundError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if result.get("deactivated"):
        print(f"{args.name} shelved again")
    else:
        print(f"{args.name} was not vaulted; nothing to do")
    return 0


def _cmd_learned(conn, args) -> int:
    from pathlib import Path

    project = memory_mod.current_project() if args.here else None
    if project:
        print(f"scoped to this project ({Path.cwd()})\n")
    print(memory_mod.render(memory_mod.suggestions(conn, project=project)))

    if args.here:
        # The pointer half: harness knows what you use here, this project's own
        # files know how it is configured here. Naming them beats copying them
        # -- a copy is what goes stale.
        notes = memory_mod.project_notes(Path.cwd())
        print()
        if notes:
            print("how tools are configured here is recorded in:")
            for name, lines in notes:
                print(f"  {Path.cwd() / name}  ({lines} lines)")
        else:
            print("no CLAUDE.md in this project -- that is where per-project tool")
            print("configuration belongs (which browser profile, which extension,")
            print("which credentials), not in the harness database.")

    if args.projects:
        print("\nwhat each project leans on:")
        for name, top in sorted(
            memory_mod.by_project(conn).items(),
            key=lambda kv: -sum(c for _, c in kv[1]),
        )[:8]:
            used = ", ".join(f"{n} ({c})" for n, c in top[:4])
            root = memory_mod.resolve_project(name)
            notes = ", ".join(n for n, _ in memory_mod.project_notes(root)) if root else ""
            where = f"  [{notes}]" if notes else ("  [no notes]" if root else "  [moved or deleted]")
            print(f"  {name[:38]:38s} {used}{where}")
        print("\nHow a tool is configured per project belongs in that project's")
        print("CLAUDE.md -- this only knows what you use where.")
    return 0


def _cmd_console(conn, args) -> int:
    if args.start:
        if console_mod.is_up(args.port):
            print(f"already running at {console_mod.url(args.port)}")
            return 0
        console_mod.ensure(args.port)
        print(f"console starting at {console_mod.url(args.port)}")
        return 0
    if console_mod.is_up(args.port):
        print(f"console: running at {console_mod.url(args.port)}")
        return 0
    print("console: not running — start it with `tare console --start`")
    return 0


def _cmd_viewer(conn, args) -> int:
    if args.stop:
        print("stop it in the terminal running it, or: pkill -f agent-flow-app")
        return 0
    if args.start and not viewer_mod.is_up(args.port):
        if not viewer_mod.available():
            print("needs `npx` on PATH — install Node.js first", file=sys.stderr)
            return 1
        viewer_mod.ensure(args.port)
        print(f"starting the viewer; it will come up at {viewer_mod.url(args.port)}")
        print("first run downloads agent-flow, which takes a few seconds")
        return 0
    print(f"viewer: {viewer_mod.status(args.port)}")
    return 0


def _cmd_update(conn, args) -> int:
    report = update_mod.check(conn)
    print(update_mod.render(report))
    return 1 if report.behind else 0


def _cmd_doctor(conn, args) -> int:
    report = doctor_mod.inspect(conn)
    print(doctor_mod.render(report))
    # Exit non-zero on any error-severity finding. An earlier version probed for
    # a `problems()` method the Report does not have, so getattr's default fired
    # every time and doctor reported success while printing two errors.
    return 1 if any(f.severity == "error" for f in report.findings) else 0


def _cmd_vault(conn, args) -> int:
    dry = not args.apply

    # The gate the whole design is ordered around: the skill and hook are what
    # make a shelved capability findable again, so nothing may be vaulted
    # before they exist. Distinguish "settings unreadable" from "not
    # installed" -- reporting the latter for the former sends the operator to
    # `tare install`, which reads the same broken file.
    if not dry:
        try:
            install_mod._load()
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            print("Fix that file before harness can tell whether it is installed.", file=sys.stderr)
            return 2
        if not install_mod.is_installed():
            print("error: the tare skill and hook are not registered.", file=sys.stderr)
            print("Shelving without them would strand every capability. Run:", file=sys.stderr)
            print("  tare install", file=sys.stderr)
            return 2

    before = audit_mod.audit(conn).total_tokens
    # Captured BEFORE applying: afterwards the plugins are disabled and the
    # promoted nodes have changed hands, so plugin_plan() correctly returns
    # nothing -- recomputing it here reported every token figure as ~0.
    planned = shelve_mod.plugin_plan(conn, floor_tokens=args.floor)

    try:
        reports = shelve_mod.shelve_user(conn, dry_run=dry)
        plugins = shelve_mod.shelve_plugins(conn, dry_run=dry, floor_tokens=args.floor)
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    moved = [r for r in reports if r["status"] in ("shelved", "would-shelve")]
    failed = [r for r in reports if r["status"] == "failed"]
    protected = [r for r in reports if r.get("reason") == "protected"]
    verb = "would shelve" if dry else "shelved"

    user_tokens = sum(r.get("est_tokens", 0) or 0 for r in moved)
    print(f"{verb} {len(moved)} user capabilit(ies) (~{user_tokens} tok):")
    for r in sorted(moved, key=lambda r: (r["kind"], r["name"])):
        print(f"  {r['kind']} {r['name']}  (~{r.get('tokens', 0)} tok)")

    for r in [r for r in reports if r["status"] == "skipped" and r.get("reason") not in ("protected", "pinned")]:
        print(f"  skipped {r['name']}: {r.get('reason')}")
    for r in failed:
        print(f"  failed {r['name']}: {r.get('error')}")

    # Never exclude silently: an operator who knows a capability is never
    # invoked must be able to see why it was left off the list.
    if protected:
        tok = sum(r.get("est_tokens", 0) or 0 for r in protected)
        print(
            f"\nprotected {len(protected)} capabilit(ies) (~{tok} tok) -- "
            "reachable via routes-to from a used capability, so left alone:"
        )
        for r in sorted(protected, key=lambda r: r["name"]):
            print(f"  {r['kind']} {r['name']}  -- routed to from {r.get('protected_by')!r}")

    if plugins["promoted"]:
        # "kept", not a saving. This line is shaped like the disable breakdown
        # below, which IS a per-item saving, so without the qualifier someone
        # skimming a hundred lines and summing the parentheticals overcounts by
        # every promoted skill.
        promoted_tokens = {p["id"]: p.get("est_tokens", 0) or 0 for p in planned["promote"]}
        kept = sum(promoted_tokens.get(pid, 0) for pid in plugins["promoted"])
        print(f"\n{'would promote' if dry else 'promoted'} {len(plugins['promoted'])} skill(s) (~{kept} tok kept):")
        for pid in plugins["promoted"]:
            print(f"  {pid} (~{promoted_tokens.get(pid, 0)} tok kept)")
    if plugins["disabled"]:
        # Per-plugin breakdown, not one combined figure for a comma list: at
        # ~100 items an operator cannot otherwise tell which plugin drives the
        # saving without cross-referencing `tare audit`.
        saving = {d["key"]: d.get("cold_tokens", 0) for d in planned["disable"]}
        total = sum(saving.get(k, 0) for k in plugins["disabled"])
        print(f"\n{'would disable' if dry else 'disabled'} {len(plugins['disabled'])} plugin(s), "
              f"~{total} tok total: "
              + ", ".join(f"{k} (~{saving.get(k, 0)} tok)" for k in plugins["disabled"]))
        plugin_saving = total
    else:
        plugin_saving = 0
    for f in plugins["failed"]:
        print(f"  failed {f.get('id')}: {f.get('error')}")

    after = audit_mod.audit(conn).total_tokens
    if dry:
        # Project rather than re-audit: nothing has moved, so audit() would
        # just report `before` again and the operator would learn nothing.
        projected = before - user_tokens - plugin_saving
        print(f"\nalways-loaded index: ~{before:,} tok now -> ~{projected:,} tok if applied")
        print(f"total would reclaim: ~{user_tokens + plugin_saving:,} tokens")
    else:
        print(f"\nalways-loaded index: ~{before:,} tok -> ~{after:,} tok")
        print(f"total reclaimed: ~{before - after:,} tokens")

    # Recovery guidance belongs in BOTH modes. Printing it only after applying
    # tells the operator how to undo something they have already done.
    if moved or plugins["disabled"]:
        print("\nNothing changed. Re-run with --apply to perform it. If you do:" if dry else "")
        print("User skills/agents: `tare activate <name>` restores them from the vault.")
        if plugins["disabled"]:
            print("Disabled plugins: `tare activate <name>` re-enables them in settings.json,")
            print("but that is a settings change, not a vaulted file -- `tare deactivate`")
            print("will NOT re-disable a plugin.")
    if not dry:
        print("\nThe index is now stale for what changed. Run `tare scan` to reconcile it.")

    return 1 if (failed or plugins["failed"]) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tare", description="Inventory and search your Claude Code harness.")
    sub = parser.add_subparsers(dest="command", required=True)

    def add(name, help_text, fn):
        p = sub.add_parser(name, help=help_text)
        p.set_defaults(fn=fn)
        return p

    add("scan", "read ~/.claude into the node table", _cmd_scan)
    add("mine", "mine transcripts for real usage", _cmd_mine)
    add("tag", "normalise descriptions (hash-cached)", _cmd_tag)
    add("build", "scan + mine + tag + edges + buckets + index", _cmd_build)

    p = add("lookup", "find a capability by intent", _cmd_lookup)
    p.add_argument("query")
    p.add_argument("--limit", type=int, default=5)

    add("audit", "token cost, buckets, duplicates", _cmd_audit)
    add("install", "register the tare skill and SessionStart hook", _cmd_install)
    add("uninstall", "remove the skill and hook; keeps the vault", _cmd_uninstall)
    add("hookline", "one line for the SessionStart hook", _cmd_hookline)

    p = add("activate", "bring a shelved capability back", _cmd_activate)
    p.add_argument("name")
    p = add("deactivate", "shelve a restored capability again", _cmd_deactivate)
    p.add_argument("name")

    add("update", "report which plugins are behind their marketplace", _cmd_update)
    p = add("console", "the capability graph, memory and agent record, live", _cmd_console)
    p.add_argument("--start", action="store_true")
    p.add_argument("--port", type=int, default=console_mod.DEFAULT_PORT)

    p = add("viewer", "the live agent-flow fleet viewer", _cmd_viewer)
    p.add_argument("--start", action="store_true", help="start it if it is not up")
    p.add_argument("--stop", action="store_true")
    p.add_argument("--port", type=int, default=viewer_mod.DEFAULT_PORT)
    add("doctor", "check the vault and installation for drift", _cmd_doctor)
    p = add("learned", "what usage has taught the harness about itself", _cmd_learned)
    p.add_argument("--here", action="store_true", help="only this project's signal")
    p.add_argument("--projects", action="store_true", help="what each project leans on")

    p = add("vault", "shelve never-invoked capabilities", _cmd_vault)
    p.add_argument("--apply", action="store_true", help="perform it (default is a dry run)")
    p.add_argument("--floor", type=int, default=shelve_mod.DEFAULT_FLOOR_TOKENS)

    args = parser.parse_args(argv)
    conn = db.connect()
    try:
        return args.fn(conn, args)
    finally:
        conn.close()


def run() -> None:
    sys.exit(main())


if __name__ == "__main__":
    run()
