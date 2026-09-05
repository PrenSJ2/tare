# Validating `src/swarm/bmad.py` against a real BMAD install

This repository's standing rule: *"Validate against real data, not fixtures alone… Every
significant claim in this repository was checked against a real `~/.claude` before it was
believed."* Fixtures test our idea of BMAD's format. This task tests BMAD's.

## Method

Ran `npx bmad-method install` (BMAD Core v6.12.0) inside a fully sandboxed environment —
`HOME`, `TARE_HOME`, and `SWARM_HOME` all pointed at a scratch directory, never at the real
`/Users/seb`. Two installs were performed: a default one, and a second with
`--output-folder custom-output` to test the override path. Neither `tare scan`, `tare build`,
nor `tare vault` was run — that step is out of scope for this task (see Open items).

`ls /Users/seb/.claude/skills | wc -l` was recorded before (**69**) and after (**69**) both
installs — unchanged. The real `~/.claude` is untouched.

The installer defaulted to a **project-scoped** install, not the user-scope the task brief
warned about: with `--tools claude-code` it wrote skills to `<project>/.claude/skills/`
(29 directories), never to `$HOME/.claude/skills`. `$HOME/.claude` in the sandbox stayed
empty throughout. This is itself worth recording: BMAD's installer does not, in this
version, silently reach for the user's home directory the way the risk assumed — it
installs relative to `--directory` (default: cwd). The sandboxing was still the right call:
nothing here guarantees that behavior can't change, and running unsandboxed to find out
would have been the wrong way to learn it.

## The three claims

### 1. `config_path` — `_bmad/bmm/config.yaml` relative to repo root

**Assumed:** `bmad.config_path()` returns `repo / "_bmad" / "bmm" / "config.yaml"`.

**Observed:** Exactly that. Both installs produced `_bmad/bmm/config.yaml` at that literal
path, with a `# BMM Module Configuration` header and the module version.

**Verdict: confirmed, no discrepancy.**

### 2. `output_root` — config key `output_folder`, default `_bmad-output`

**Assumed:** `bmad.output_root()` reads `output_folder` from `_bmad/bmm/config.yaml`,
falling back to `DEFAULT_OUTPUT_FOLDER = "_bmad-output"`.

**Observed:**
- `bmad-method install --help` documents the flag as `--output-folder <path>
  (default: _bmad-output)` — the default is a CLI-declared fact, not a guess.
- The default install's `_bmad/bmm/config.yaml` carries `output_folder: _bmad-output`
  verbatim, under "Core Configuration Values".
- The second install, run with `--output-folder custom-output`, produced
  `output_folder: custom-output` in the generated config, and
  `bmad.output_root(Path('.'))` against that tree correctly resolved to `custom-output`.

**Verdict: confirmed, both the key name and the default, including the override path.**

### 3. `spec_folders` — `stories.yaml` at `<output_root>/specs/<slug>/`, beside `SPEC.md`

**Assumed:** `bmad.spec_folders()` looks for any directory under `<output_root>/specs/`
that contains a `stories.yaml`, and treats the directory name as the story's `slug`
namespace.

**Observed:** No real spec/story-breakdown workflow could be driven end-to-end
programmatically — `bmad-spec`'s "Story Breakdown" section is explicitly
interactive-only and expects a live conversation walking capabilities with a user, which
a scripted validation run cannot supply. Two things stood in for it:

- The installed skill's own contract, `.claude/skills/bmad-spec/SKILL.md` and its
  `customize.toml`, states the convention directly and authoritatively (this is BMAD's
  own installed documentation, not ours):
  - `spec_output_path = "{output_folder}/specs"`
  - `run_folder_pattern = "spec-{slug}"` — **note:** the folder name is `spec-<slug>`,
    not bare `<slug>`. `stories.yaml` is described as "a sibling of `SPEC.md` inside the
    spec folder, discovered by that fixed name."
  - The installed `assets/stories-schema.md` documents `stories.yaml`'s four validity
    rules — verbatim identical in number and substance to the four rules
    `bmad.parse_stories` already enforces (unique/required fields, prefix-free ids, no
    `status` field, quoted string ids only).
- To confirm the code, not just read about it, a spec folder was hand-authored at the
  documented path — `_bmad-output/specs/spec-probe-widget/{SPEC.md,stories.yaml,.memlog.md}`
  — using the exact schema from `stories-schema.md`. `bmad.spec_folders()` found it,
  `bmad.stories_for()` parsed both stories correctly, and `doctor.check_bmad()` reported
  `ok: spec-probe-widget: 2 stories`.

The `spec-` prefix on the folder name does **not** break `bmad.py`: `spec_folders()`
never assumes the directory name equals the slug it was given at spec-creation time — it
just walks every directory under `specs/` that has a `stories.yaml`, and `Story.slug`
is documented as "the spec folder's name", not "the slug BMAD was told to use". The two
happen to differ by a `spec-` prefix in the real tool, and the code never notices,
because it never compares them.

**Verdict: confirmed on the path structure and the stories.yaml schema.** One nuance
worth naming even though it needed no fix: BMAD's real folder names carry a `spec-`
prefix (`spec-<slug>`) that this codebase's docstrings and tests describe informally as
`<slug>`. Anywhere a human reads `Story.slug` or a log line built from it, it will show
the `spec-` prefix — this is cosmetic, not a parsing risk, but doctor/log messages that
say "slug" should be read as "folder name" if this is ever surfaced to a user.

No code changes were needed. No fixture/test gap was found: the existing tests already
encode the four validity rules faithfully to `stories-schema.md`, and
`tests/swarm_bmad.py` / `tests/swarm_doctor.py` need no update.

## The skill-landing question

`.claude/skills/` after a default `--modules bmm --tools claude-code` install contains
**29 directories**, each an individual `bmad-*/SKILL.md` (with sibling `assets/`,
`scripts/`, `references/`, `customize.toml`, etc. — no plugin manifest, no single
packaged bundle). Examples: `bmad-spec`, `bmad-build`, `bmad-build-auto`, `bmad-prd`,
`bmad-architecture`, `bmad-brainstorming`, `bmad-help`, `bmad-agent-dev`, and 21 others.

This is exactly the shape `tare scan` classifies as `origin='user-authored'` — individual
`SKILL.md`-carrying directories directly under `.claude/skills/`, not a plugin. That makes
them vault-eligible under tare's existing rules, as the risk in the spec anticipated. This
observation comes from reading the installed tree, not from running `tare scan` (out of
scope this task — see Open items).

`bmad-build`'s own `SKILL.md` is a thin dispatcher: its entire body is "run
`render_skill.py`, then follow the one `workflow.md` instruction it prints." The actual
menu/step logic (`step-01-clarify-and-route.md` … `step-05-present.md`) lives as plain
files *inside* the `bmad-build/` skill directory, not as separate top-level skills BMAD
dispatches by name. Whether that changes how a `routes-to` guard would need to be shaped
is exactly the question in the first open item below — recorded, not resolved.

## Test suite

`python -m pytest tests/test_*.py tests/swarm_*.py -q` — **748 passed, 9 failed**, matching
the documented pre-existing baseline exactly (6 `FileNotFoundError` cases in
`tests/swarm_project.py`, 3 known-red `screen()` gate cases in `tests/swarm_nightshift.py`).
No new failures, no fix required, nothing to add to the failure count.

## Open items

This sandboxed run cannot settle either of these — both need the user's real `~/.claude`
index or a real multi-agent session, which are both out of scope here:

1. **Whether `tare vault` would shelve BMAD's own skills, and whether the `routes-to`
   guard protects the workflow skills `bmad-build` dispatches.** This run confirms the
   *shape* that would make BMAD skills vault-eligible (29 individual `SKILL.md`
   directories, `origin='user-authored'`) and that `bmad-build`'s dispatch is internal to
   its own directory rather than a fan-out to sibling top-level skills — but confirming
   the guard's actual behavior needs a real index built by `tare scan`/`tare build`
   against an installed BMAD, which this task's scope explicitly forbids running. If this
   matters before the feature ships, it should be its own task against the real
   `~/.claude`, not folded back into this one.

2. **Whether a `Task`-tool subagent inherits the parent's `--allowedTools`/
   `--disallowedTools`.** Recorded as unverified during Task 4. Still unverified — nothing
   in this task's scope (a BMAD install, not an agent-permission experiment) could settle
   it. If it does not inherit, story mode's deny list is bypassable by spawning one. This
   needs a dedicated permission-boundary test, not a BMAD install.

## Sandbox state

Left in place for inspection at
`/private/tmp/claude-501/-Users-seb-Documents-Code-tare/07054922-ede8-4340-9d5f-85eb5ade4367/scratchpad/bmad-probe/`:
- `project/` — default install (`_bmad/`, `_bmad-output/` with the hand-authored
  `spec-probe-widget` folder, `.claude/skills/` with 29 BMAD skills)
- `project2/` — second install with `--output-folder custom-output`
- `home/.claude/` — empty; confirms the installer never wrote to `$HOME/.claude` in
  either run

`/Users/seb/.claude/skills` was 69 before and 69 after — confirmed untouched.
