from tare import db, frontmatter, paths


def test_paths_follow_tare_home(fake_home):
    assert paths.claude_home() == fake_home
    assert paths.db_path().parent == fake_home
    assert paths.vault_dir() == fake_home / "vault"
    assert paths.skill_install_path() == fake_home / "skills" / "tare" / "SKILL.md"


def test_connect_creates_every_table(fake_home):
    conn = db.connect()
    names = {r["name"] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"nodes", "edges", "usage", "events", "tag_cache"} <= names


def test_connect_is_idempotent_and_keeps_data(fake_home):
    conn = db.connect()
    conn.execute("INSERT INTO nodes (id, kind, name) VALUES ('skill:a', 'skill', 'a')")
    conn.commit()
    conn.close()
    conn = db.connect()
    assert conn.execute("SELECT COUNT(*) c FROM nodes").fetchone()["c"] == 1


def test_connect_is_not_autocommit(fake_home):
    """isolation_level=None would silently break delete-then-repopulate."""
    conn = db.connect()
    assert conn.isolation_level is not None


def test_frontmatter_parses_name_and_description():
    fm, err = frontmatter.parse("---\nname: alpha\ndescription: Does a thing.\n---\n\nBody\n")
    assert err is None and fm.name == "alpha" and fm.description == "Does a thing."


def test_frontmatter_reports_rather_than_raises():
    for text, fragment in [
        ("no frontmatter here", "no frontmatter"),
        ("---\nname: a\n", "not closed"),
        ("---\n\tbad: [\n---\n", "valid YAML"),
        ("---\n- a\n- b\n---\n", "not a mapping"),
    ]:
        fm, err = frontmatter.parse(text)
        assert fm is None and fragment in err


def test_non_scalar_name_is_treated_as_absent_not_coerced():
    fm, err = frontmatter.parse("---\nname:\n  a: b\ndescription: x\n---\n")
    assert err is None and fm.name == ""


def test_est_tokens_is_zero_for_empty():
    assert paths.est_tokens("") == 0 and paths.est_tokens("a" * 400) == 100


def test_fake_home_isolates_both_halves(fake_home):
    """A regression guard with a real incident behind it.

    `fake_home` once set TARE_HOME only. The two halves read their root from
    different variables, so swarm stayed pointed at the operator's real
    ~/.claude, and a test that installed both wrote eight hook entries into
    it -- aimed at pytest temp paths that were gone by the time it finished.

    Assert on the modules' own resolution, not on the environment: what
    matters is where a write would actually land.
    """
    from swarm import paths as swarm_paths
    from tare import paths as tare_paths

    assert tare_paths.claude_home() == fake_home
    assert swarm_paths.claude_home() == fake_home
    # The settings file is the one both halves write, and the one that got hurt.
    assert tare_paths.settings_path() == swarm_paths.settings_path()
    assert str(fake_home) in str(swarm_paths.settings_path())
