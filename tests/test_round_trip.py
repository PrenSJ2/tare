"""The property the whole design rests on.

Shelving must cost context but NOT capability. A token saving that loses a
capability is a regression, not a win -- so these tests assert the round trip
is an identity, and that a shelved capability is still findable.
"""

from __future__ import annotations

import json

from harness import activate, audit, cli, db, install, lookup, scan, shelve, vault


def build_capabilities(home, count=6):
    for i in range(count):
        d = home / "skills" / f"cap{i}"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_text(
            f"---\nname: cap{i}\ndescription: Handles case {i} of the workflow.\n---\n\nBody.\n"
        )


def mark_mined(conn, home):
    """shelve_user refuses to apply with zero usage evidence -- with none, every
    capability looks cold and the whole configuration becomes a candidate.

    The seed gets a real file on disk: a node with usage but no file is exactly
    what the vault-aware prune is supposed to delete, so faking it would make
    these tests fight the behaviour they exist to protect.
    """
    d = home / "skills" / "seed"
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text("---\nname: seed\ndescription: A capability that is actually used.\n---\n\nBody.\n")
    scan.scan_user_skills(conn)
    conn.execute("INSERT OR REPLACE INTO usage (node_id, invocations, sessions, last_used) VALUES ('skill:seed', 3, 1, '2026-08-01T00:00:00Z')")
    conn.execute("INSERT INTO events (ts, kind, node_id) VALUES ('2026-08-01T00:00:00Z','invocation','skill:seed')")
    conn.commit()


def install_harness(home):
    (home / "settings.json").write_text("{}")
    install.install()


def test_vault_then_activate_restores_every_capability(fake_home):
    conn = db.connect()
    install_harness(fake_home)
    build_capabilities(fake_home)
    scan.scan_user_skills(conn)
    mark_mined(conn, fake_home)

    before = {r["name"] for r in conn.execute("SELECT name FROM nodes WHERE kind='skill'")}

    shelve.shelve_user(conn, dry_run=False)
    scan.scan_vaulted(conn)
    scan.scan_user_skills(conn)

    for name in sorted(n for n in before if n.startswith("cap")):
        activate.activate(conn, name)
    scan.scan_vaulted(conn)
    scan.scan_user_skills(conn)

    after = {r["name"] for r in conn.execute("SELECT name FROM nodes WHERE kind='skill'")}
    assert after == before
    for name in before:
        assert (fake_home / "skills" / name).exists(), f"{name} did not come back"


def test_a_shelved_capability_is_still_findable(fake_home):
    """The A/B check: lookup must have no state filter."""
    conn = db.connect()
    install_harness(fake_home)
    build_capabilities(fake_home)
    scan.scan_user_skills(conn)
    mark_mined(conn, fake_home)
    for i in range(6):
        conn.execute("UPDATE nodes SET when_to_use=? WHERE name=?", (f"handling case {i}", f"cap{i}"))
    conn.commit()
    lookup.reindex(conn)

    found_before = {i: lookup.lookup(conn, f"handling case {i}")[0].name for i in range(6)}

    shelve.shelve_user(conn, dry_run=False)
    scan.scan_vaulted(conn)
    scan.scan_user_skills(conn)
    lookup.reindex(conn)

    for i in range(6):
        results = lookup.lookup(conn, f"handling case {i}")
        assert results, f"case {i} became unfindable after shelving"
        assert results[0].name == found_before[i]
        assert results[0].state == "vaulted"


def test_shelving_drops_the_token_total(fake_home):
    conn = db.connect()
    install_harness(fake_home)
    build_capabilities(fake_home)
    scan.scan_user_skills(conn)
    mark_mined(conn, fake_home)

    before = audit.audit(conn).total_tokens
    shelve.shelve_user(conn, dry_run=False)
    scan.scan_vaulted(conn)
    scan.scan_user_skills(conn)
    assert audit.audit(conn).total_tokens < before


def test_scan_is_stable_across_repeated_runs(fake_home):
    """A node that flips state on alternate scans is worse than either state."""
    conn = db.connect()
    install_harness(fake_home)
    build_capabilities(fake_home)
    mark_mined(conn, fake_home)

    def snapshot():
        scan.scan_vaulted(conn)
        scan.scan_user_skills(conn)
        scan.scan_agents(conn)
        scan.scan_plugin_skills(conn)
        return [tuple(r) for r in conn.execute(
            "SELECT id, origin, state, est_tokens FROM nodes ORDER BY id")]

    first = snapshot()
    assert snapshot() == first

    shelve.shelve_user(conn, dry_run=False)
    after_shelve = snapshot()
    assert snapshot() == after_shelve


def test_vault_dry_run_is_the_default_and_moves_nothing(fake_home):
    conn = db.connect()
    install_harness(fake_home)
    build_capabilities(fake_home)
    scan.scan_user_skills(conn)
    mark_mined(conn, fake_home)

    rc = cli.main(["vault"])
    assert rc == 0
    assert (fake_home / "skills" / "cap0").exists()
    assert not vault.is_initialized()


def test_vault_apply_refuses_without_the_skill_and_hook(fake_home):
    """The gate the whole phase is ordered around."""
    conn = db.connect()
    (fake_home / "settings.json").write_text("{}")
    build_capabilities(fake_home)
    scan.scan_user_skills(conn)
    mark_mined(conn, fake_home)

    rc = cli.main(["vault", "--apply"])
    assert rc == 2
    assert (fake_home / "skills" / "cap0").exists()
    row = conn.execute("SELECT state FROM nodes WHERE name='cap0'").fetchone()
    assert row["state"] == "live"


def test_vault_apply_with_corrupt_settings_says_so(fake_home):
    """Not 'not installed' -- that advice sends the operator to a command that
    reads the same broken file."""
    conn = db.connect()
    build_capabilities(fake_home)
    scan.scan_user_skills(conn)
    mark_mined(conn, fake_home)
    (fake_home / "settings.json").write_text("{not json")

    rc = cli.main(["vault", "--apply"])
    assert rc == 2
