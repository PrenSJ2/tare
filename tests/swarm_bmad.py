"""Reading BMAD's plan, and refusing to read it wrongly.

The example in `parse_stories`' docstring is copied verbatim from BMAD's own
`stories-schema.md`. That matters: a hand-written approximation of their
format only ever tests our idea of it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from swarm import bmad


# Copied verbatim from BMAD's src/bmm-skills/plan/bmad-spec/assets/stories-schema.md
SCHEMA_EXAMPLE = """\
- id: "1"
  title: Add rate limiting to the public API
  description: >-
    Introduce a token-bucket limiter in front of the public endpoints;
    return 429 with a Retry-After header on limit breach.
  spec_checkpoint: true
  invoke_dev_with: >-
    Rate limit state must be shared across instances; use the existing
    Redis client, not in-process memory.
- id: "2"
  title: Expose limiter metrics to the ops dashboard
  description: >-
    Emit per-route accept/reject counters the existing dashboard can
    scrape; no new dashboard panels in this story.
"""


def test_it_parses_the_schemas_own_example(tmp_path):
    stories = bmad.parse_stories(SCHEMA_EXAMPLE, spec_dir=tmp_path / "spec-x")

    assert [s.id for s in stories] == ["1", "2"]
    assert stories[0].title == "Add rate limiting to the public API"
    assert stories[0].spec_checkpoint is True
    assert stories[0].done_checkpoint is False          # default
    assert "Redis client" in stories[0].invoke_dev_with
    assert stories[1].invoke_dev_with == ""             # default
    assert stories[0].slug == "spec-x"


def test_execution_order_is_list_order_not_id_sort(tmp_path):
    text = '- id: "10"\n  title: T10\n  description: D\n- id: "2"\n  title: T2\n  description: D\n'
    assert [s.id for s in bmad.parse_stories(text, spec_dir=tmp_path)] == ["10", "2"]


# --- the four validity rules, each refused whole ----------------------------

def test_an_unquoted_id_is_refused_as_rule_4(tmp_path):
    """The one that will actually happen. YAML turns `id: 1` into an int,
    the ledger join silently stops matching, and the story runs every night."""
    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.parse_stories("- id: 1\n  title: T\n  description: D\n", spec_dir=tmp_path)
    assert exc.value.rule == 4
    assert "unquoted" in exc.value.detail


def test_duplicate_ids_are_refused(tmp_path):
    text = '- id: "1"\n  title: A\n  description: D\n- id: "1"\n  title: B\n  description: D\n'
    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.parse_stories(text, spec_dir=tmp_path)
    assert exc.value.rule == 1


def test_ids_that_are_not_prefix_free_are_refused(tmp_path):
    text = '- id: "3"\n  title: A\n  description: D\n- id: "3-2"\n  title: B\n  description: D\n'
    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.parse_stories(text, spec_dir=tmp_path)
    assert exc.value.rule == 2


def test_a_status_field_is_refused(tmp_path):
    text = '- id: "1"\n  title: A\n  description: D\n  status: done\n'
    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.parse_stories(text, spec_dir=tmp_path)
    assert exc.value.rule == 3


def test_an_id_with_a_slash_is_refused(tmp_path):
    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.parse_stories('- id: "a/b"\n  title: A\n  description: D\n', spec_dir=tmp_path)
    assert exc.value.rule == 4


def test_a_broken_file_yields_nothing_rather_than_a_partial_plan(tmp_path):
    """Half a plan is not a smaller plan, it is a different one."""
    text = '- id: "1"\n  title: A\n  description: D\n- id: 2\n  title: B\n  description: D\n'
    with pytest.raises(bmad.BmadFormatError):
        bmad.parse_stories(text, spec_dir=tmp_path)


def test_an_empty_file_is_an_empty_plan_not_an_error(tmp_path):
    assert bmad.parse_stories("", spec_dir=tmp_path) == []


# --- finding the install ----------------------------------------------------

def _install(repo: Path, *, slugs=("spec-alpha",), output_folder=None) -> Path:
    """A BMAD install as it appears on disk: config plus spec folders."""
    cfg = repo / "_bmad" / "bmm"
    cfg.mkdir(parents=True, exist_ok=True)
    body = "project_name: demo\n"
    if output_folder:
        body += f"output_folder: {output_folder}\n"
    (cfg / "config.yaml").write_text(body)
    root = repo / (output_folder or "_bmad-output") / "specs"
    for slug in slugs:
        d = root / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "SPEC.md").write_text("# spec\n")
        (d / "stories.yaml").write_text('- id: "1"\n  title: T\n  description: D\n')
    return repo


def test_spec_folders_are_found_under_the_default_output_root(tmp_path):
    _install(tmp_path, slugs=("spec-alpha", "spec-beta"))
    found = bmad.spec_folders(tmp_path)
    assert [p.name for p in found] == ["spec-alpha", "spec-beta"]


def test_the_output_root_honours_config_rather_than_being_hardcoded(tmp_path):
    """A non-default install is the common case, not an edge one."""
    _install(tmp_path, output_folder="build-artifacts")
    assert bmad.output_root(tmp_path) == tmp_path / "build-artifacts"
    assert [p.name for p in bmad.spec_folders(tmp_path)] == ["spec-alpha"]


def test_a_folder_without_stories_yaml_is_not_a_spec_folder(tmp_path):
    _install(tmp_path)
    lonely = tmp_path / "_bmad-output" / "specs" / "spec-planning-only"
    lonely.mkdir(parents=True)
    (lonely / "SPEC.md").write_text("# no stories yet\n")
    assert [p.name for p in bmad.spec_folders(tmp_path)] == ["spec-alpha"]


def test_no_install_means_no_spec_folders(tmp_path):
    assert bmad.spec_folders(tmp_path) == []
    assert bmad.is_installed(tmp_path) is False


# --- error messages identify the correct file ---------------------------------

def test_a_malformed_config_yaml_names_config_not_stories(tmp_path):
    """A broken config.yaml should be identified in the error message, not stories.yaml."""
    cfg = tmp_path / "_bmad" / "bmm"
    cfg.mkdir(parents=True, exist_ok=True)
    # Write invalid YAML to config.yaml
    (cfg / "config.yaml").write_text("invalid: [unclosed\n")

    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.output_root(tmp_path)
    assert exc.value.rule == 1
    assert exc.value.source == "config.yaml"
    assert "config.yaml" in str(exc.value)
    assert "does not parse as YAML" in exc.value.detail


# --- checkpoint field validation -----------------------------------------------

def test_spec_checkpoint_true_and_false_parse_correctly(tmp_path):
    """Actual YAML booleans must be accepted."""
    text = '- id: "1"\n  title: T\n  description: D\n  spec_checkpoint: true\n- id: "2"\n  title: T\n  description: D\n  spec_checkpoint: false\n'
    stories = bmad.parse_stories(text, spec_dir=tmp_path)
    assert stories[0].spec_checkpoint is True
    assert stories[1].spec_checkpoint is False


def test_checkpoint_fields_absent_give_the_default(tmp_path):
    """Omitting checkpoint fields should use the default False."""
    text = '- id: "1"\n  title: T\n  description: D\n'
    stories = bmad.parse_stories(text, spec_dir=tmp_path)
    assert stories[0].spec_checkpoint is False
    assert stories[0].done_checkpoint is False


def test_a_quoted_spec_checkpoint_false_string_is_refused(tmp_path):
    """The dangerous case: `spec_checkpoint: "false"` parses as a string, not a bool."""
    text = '- id: "1"\n  title: T\n  description: D\n  spec_checkpoint: "false"\n'
    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.parse_stories(text, spec_dir=tmp_path)
    assert exc.value.rule == 1
    assert "spec_checkpoint" in exc.value.detail
    assert "'false'" in exc.value.detail


def test_a_numeric_done_checkpoint_is_refused(tmp_path):
    """Integers must be refused, not silently coerced to bool."""
    text = '- id: "1"\n  title: T\n  description: D\n  done_checkpoint: 1\n'
    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.parse_stories(text, spec_dir=tmp_path)
    assert exc.value.rule == 1
    assert "done_checkpoint" in exc.value.detail
    assert "int" in exc.value.detail


def test_stories_for_names_which_spec_folder_is_malformed(tmp_path):
    """`parse_stories` has no way to know which folder it was handed --
    `stories_for` is the 4am path (via `queue.next_story`), and a queue with
    several spec folders that hits a bad file two folders in must not make an
    operator guess which one. `doctor.check_bmad` already names the folder
    when IT catches this; this is the equivalent for the path that actually
    runs the queue."""
    cfg = tmp_path / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text("project_name: demo\n")
    good = tmp_path / "_bmad-output" / "specs" / "spec-alpha"
    good.mkdir(parents=True)
    (good / "stories.yaml").write_text('- id: "1"\n  title: T\n  description: D\n')
    bad = tmp_path / "_bmad-output" / "specs" / "spec-beta"
    bad.mkdir(parents=True)
    (bad / "stories.yaml").write_text("not-a-list: true\n")

    with pytest.raises(bmad.BmadFormatError) as exc:
        bmad.stories_for(tmp_path)
    assert "spec-beta" in str(exc.value)
    assert "spec-beta" in exc.value.source
