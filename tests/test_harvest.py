"""Turning fix commits into findings.

The model call is stubbed throughout. What is tested is everything around it:
which commits are even offered, what a malformed answer does, and the guards
that stop a harvest quietly damaging findings someone refined by hand.
"""

import subprocess

from tare import brain, db, harvest
from tare.harvest import Candidate


def _repo(tmp_path, commits):
    repo = tmp_path / "proj"
    repo.mkdir()
    run = lambda *a: subprocess.run(["git", "-C", str(repo), *a],
                                    capture_output=True, check=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    for i, (subject, body) in enumerate(commits):
        (repo / f"f{i}.txt").write_text(str(i))
        run("add", "-A")
        run("commit", "-q", "-m", subject, "-m", body)
    return repo


CAUSAL_BODY = (
    "The startup sequence set the default tab before the overview returned, "
    "because the fetch resolves after mount, which meant that every reload "
    "flickered through the wrong tab before settling on the right one. " * 2
)


# --- which commits are offered at all --------------------------------------

def test_only_fixes_that_explain_themselves_are_offered(tmp_path):
    repo = _repo(tmp_path, [
        ("fix(ui): kill the reload flicker", CAUSAL_BODY),      # keep
        ("feat(ui): add a new tab", CAUSAL_BODY),               # not a fix
        ("fix(ui): typo", "small"),                             # no body
        ("fix(api): rename a field", "Renamed for consistency across the codebase. " * 12),
    ])
    offered = [c.subject for c in harvest.candidates(repo)]
    assert offered == ["fix(ui): kill the reload flicker"]


def test_a_feature_commit_is_never_offered_however_long_its_body(tmp_path):
    """762 commits contain claim-shaped language; 138 are self-explaining fixes.

    The gap is design prose — "installation_id, never derived from hostname"
    reads like a lesson and is a specification.
    """
    repo = _repo(tmp_path, [("feat: telemetry", "installation_id is never derived "
                                                "from hostname or username. " * 20)])
    assert harvest.candidates(repo) == []


def test_a_directory_that_is_not_a_repo_yields_nothing(tmp_path):
    assert harvest.candidates(tmp_path) == []


# --- parsing the answer ----------------------------------------------------

def _candidate():
    return Candidate(repo="proj", sha="abc1234", subject="fix: x", body="y")


def test_a_well_formed_answer_becomes_a_finding():
    parsed = harvest.parse_response(
        "NAME: a-validator-nothing-invokes-is-not-validation\n"
        "SCOPE: tool\nTOOLS: flutter\nTAGS: forms,validation\n"
        "CLAIM: TextFormField.validator only runs if a Form invokes it.\n"
        "WHY: Without a formKey nothing calls validate, so every field passes.\n"
        "APPLY: Assert the validator is invoked, not that valid input succeeds.\n",
        _candidate())
    assert parsed.skipped == ""
    assert parsed.name == "a-validator-nothing-invokes-is-not-validation"
    assert parsed.scope == "tool"
    assert parsed.tools == ["flutter"]
    assert "only runs if a Form invokes it" in parsed.claim


def test_none_means_nothing_is_written():
    parsed = harvest.parse_response("NONE", _candidate())
    assert parsed.skipped == "nothing durable to learn"
    assert parsed.name == ""


def test_a_malformed_answer_is_dropped_not_guessed_at():
    """A finding with an invented name is worse than an unharvested commit."""
    for reply in ("SCOPE: universal\nCLAIM: something", "NAME: x", "", "   "):
        assert harvest.parse_response(reply, _candidate()).skipped


def test_an_empty_field_does_not_swallow_the_next_one():
    """Caught by reading a real generated finding: `tools: [TAGS: guards, ...]`.

    An empty TOOLS line consumed its own newline, so the lookahead that should
    have stopped at `TAGS:` never fired and the tags landed in the tools field.
    """
    parsed = harvest.parse_response(
        "NAME: a-thing\nSCOPE: universal\nTOOLS:\nTAGS: guards,scanning\n"
        "CLAIM: A claim about a thing.\n", _candidate())
    assert parsed.tools == []
    assert parsed.tags == ["guards", "scanning"]


def test_an_unknown_scope_falls_back_to_the_narrowest():
    parsed = harvest.parse_response(
        "NAME: a-thing\nSCOPE: galactic\nCLAIM: A claim about a thing.\n", _candidate())
    assert parsed.scope == "project"


def test_a_name_with_punctuation_is_made_safe():
    parsed = harvest.parse_response(
        "NAME: A Thing! That/Broke\nSCOPE: universal\nCLAIM: A claim.\n", _candidate())
    # It becomes a filename, so it cannot carry a slash.
    assert parsed.name == "a-thing-that-broke"


def test_the_rendered_finding_records_the_commit_it_came_from():
    parsed = harvest.parse_response(
        "NAME: a-thing\nSCOPE: universal\nCLAIM: A claim.\nWHY: Because.\nAPPLY: Do this.\n",
        _candidate())
    text = harvest.render_finding(parsed)
    # Provenance is what makes a finding auditable, and what the same-commit
    # guard matches on.
    assert "source: proj@abc1234" in text
    assert "projects: [proj]" in text
    assert "**Why it matters:** Because." in text


# --- the guards ------------------------------------------------------------

def test_a_commit_that_already_produced_a_finding_is_skipped(fake_home):
    """Shown the overlapping finding at rank 1, the model still proposed a
    second one from the same commit. That is a call for a person to make, not
    a harvester running unattended.
    """
    directory = fake_home / "brain" / "findings"
    directory.mkdir(parents=True)
    (directory / "existing.md").write_text(
        "---\nname: existing\nscope: universal\n"
        "source: proj@abc1234 fix: x\nsuperseded_by: null\n---\n\nA claim.\n")
    assert harvest._already_harvested(_candidate()) is True
    assert harvest._already_harvested(
        Candidate(repo="proj", sha="different", subject="s", body="b")) is False


def test_dedup_candidates_come_from_the_commits_own_words(fake_home):
    directory = fake_home / "brain" / "findings"
    directory.mkdir(parents=True)
    (directory / "flicker.md").write_text(
        "---\nname: flicker\nscope: universal\nsuperseded_by: null\n---\n\n"
        "A default set before an async fetch resolves flickers on every reload.\n")
    conn = db.connect()
    brain.reindex(conn)
    shown = harvest._existing_for(
        conn, Candidate(repo="p", sha="s", subject="fix: reload flicker", body=CAUSAL_BODY))
    assert "flicker" in shown


def test_no_findings_yet_is_reported_rather_than_left_blank(fake_home):
    conn = db.connect()
    assert harvest._existing_for(conn, _candidate()) == "(none)"


def test_a_dry_run_writes_nothing(fake_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, [("fix(ui): kill the reload flicker", CAUSAL_BODY)])
    monkeypatch.setattr(harvest, "_ask", lambda prompt, **kw:
                        "NAME: a-thing\nSCOPE: universal\nCLAIM: A claim.\n")
    conn = db.connect()
    results = harvest.harvest(conn, repo, apply=False)
    assert results and not results[0].skipped
    assert not (brain.brain_dir() / "a-thing.md").exists()


def test_apply_writes_the_finding_and_reindexes(fake_home, tmp_path, monkeypatch):
    repo = _repo(tmp_path, [("fix(ui): kill the reload flicker", CAUSAL_BODY)])
    monkeypatch.setattr(harvest, "_ask", lambda prompt, **kw:
                        "NAME: a-default-set-before-a-fetch-resolves-flickers\n"
                        "SCOPE: universal\nCLAIM: A default set before an async fetch "
                        "resolves flickers on every reload.\n")
    conn = db.connect()
    harvest.harvest(conn, repo, apply=True)
    assert (brain.brain_dir() / "a-default-set-before-a-fetch-resolves-flickers.md").exists()
    # Written AND searchable, or the harvest has only made files.
    assert brain.recall(conn, "default flickers on reload")


def test_an_existing_finding_is_never_overwritten(fake_home, tmp_path, monkeypatch):
    """A harvest that silently rewrites a finding you refined by hand is a
    data-loss bug wearing a feature's clothes.
    """
    directory = fake_home / "brain" / "findings"
    directory.mkdir(parents=True)
    (directory / "a-thing.md").write_text(
        "---\nname: a-thing\nscope: universal\nsuperseded_by: null\n---\n\nHand written.\n")
    repo = _repo(tmp_path, [("fix(ui): kill the reload flicker", CAUSAL_BODY)])
    monkeypatch.setattr(harvest, "_ask", lambda prompt, **kw:
                        "NAME: a-thing\nSCOPE: universal\nCLAIM: Generated.\n")
    conn = db.connect()
    results = harvest.harvest(conn, repo, apply=True)
    assert "already exists" in results[0].skipped
    assert "Hand written." in (directory / "a-thing.md").read_text()


def test_the_prompt_is_excluded_from_the_usage_corpus():
    """This shells out to `claude -p`, which writes a transcript into the very
    corpus `mine` reads. Unregistered, tare would mine its own exhaust.
    """
    from tare import mine

    assert harvest.PROMPT.startswith(mine.HARVEST_PROMPT_SIGNATURE)
    assert mine.HARVEST_PROMPT_SIGNATURE in mine.OWN_PROMPT_SIGNATURES
