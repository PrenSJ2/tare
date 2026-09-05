# Follow-ups from the BMAD queue branch

Everything here was found while building `--queue bmad` and deliberately left
out of that branch. Each entry says what it is, how it was established, and why
it was not fixed there.

## Pre-existing, and this branch made them visible rather than causing them

### The swarm half of the test suite does not run under a bare `pytest`

`pyproject.toml` has no `[tool.pytest.ini_options]`, so pytest's default
`python_files = test_*.py *_test.py` never matches `swarm_*.py`. Measured:

```
python -m pytest --collect-only -q         # 403 tests
python -m pytest --collect-only -q | grep -c swarm_    # 0
```

So `cli`, `doctor`, `nightshift`, `keepgoing`, `reader`, `hook`, `monitor`,
`shells` and `project` are all invisible to a default run — roughly 440 tests.

Not fixed here because enabling collection turns a currently-green run red: see
the next item. The fix is two lines plus a decision about those failures.

Every gate in the BMAD plan was corrected to
`pytest tests/test_*.py tests/swarm_*.py` once this was found.

### Six failures in `tests/swarm_project.py`

All `FileNotFoundError` on missing `tests/fixtures/payloads/*.json`. Confirmed
present on `c2e7a2e`, before this branch. Invisible today precisely because of
the item above.

### `run_shift` skips its `end` ledger record on an exception

Story mode's `run_story_shift` was given an outer `try/finally` so the ledger
always closes with a reason. The pre-existing `run_shift` still has the shape
that fix was written for: an exception in its dispatch loop skips
`record({"event": "end"})`, leaving a shift that started and never finished
according to the only record anyone reads.

Session mode only, untouched by this branch. The story-mode fix is the
mirror image and should be ported.

## Open questions this branch could not settle

### Would `tare vault` shelve BMAD's own skills?

The sandboxed validation established the *shape* but not the behaviour: a real
BMAD v6.12.0 install writes **29 skills as individual `SKILL.md` directories**,
project-scoped, which is the layout `tare scan` classifies as
`origin='user-authored'` — the one origin `tare vault --apply` physically moves.

What is unknown is whether the `routes-to` guard protects the workflow skills
`bmad-build` dispatches. It cannot affect the nightshift loop, which never
consults the vault. It can affect someone who installs BMAD and then runs
`tare vault`.

Answering it needs a real `~/.claude` and a real index, which the validation run
was deliberately scoped away from.

### Does a `Task`-tool subagent inherit `--allowedTools` / `--disallowedTools`?

Unresolved. If it does not, `WIDE_DENIED_TOOLS` is bypassable by spawning one
subagent.

Closed conservatively rather than answered: `Task` was removed from
`WIDE_TOOLS`. An unverified assumption that a control holds is not a control,
and a story implementation does not need to spawn subagents. Restoring the
grant requires settling this against the real CLI first — the comment beside
`WIDE_TOOLS` says what to check.

## Known limits of the shipped design, stated rather than fixed

### The story spec file reaches the verifier unscreened

`screen()` covers `title`, `description` and `invoke_dev_with`. It does not
cover `stories/<id>-*.md`, which `verify.acceptance_criteria` reads whole into
the verifier's prompt — so the artifact being judged supplies text to its
judge, and that verdict is what removes a story from the queue permanently. The
same file reaches `bmad-build-auto` under the wide tool policy.

Documented in `verify.py`'s residual-risk section and in the spec. Screening it
is a larger design change than that branch should have carried.

### The production gate's residual miss rate

Measured on a corpus the patterns were not fitted to: roughly **1 in 7** on the
production matcher alone, with four named classes it misses — budget overflow
past four modifiers, credential vocabulary gaps (`certificate`), `.env` verb
gaps (`point the .env at…`), and bare `drop … table` without tier-1 adjacency.

Recorded beside `_PRODUCTION_VERBS` with the measurement. Not chased further on
purpose: three rounds of pattern-chasing each improved their own examples and
regressed on unseen prose, and the fourth round succeeded only by being
subtractive.

Since story mode runs the production matcher without the actionability check,
this rate is the entire boundary on the injection surface.

### `git worktree add` on a Git-LFS repository is not local-only

`worktree.py`'s timeouts assume local git. `worktree add` runs the smudge
filter, which downloads. The timeout was raised to 90s and the docstring
corrected, but the number is a reasoned midpoint, not a measurement against a
real LFS repo or a large monorepo — the two measurements available (2.46s for
20k tracked files, 1.72s to remove 30k ignored files) were both well under the
original 15s.

### `kind` on a park record is an unenforced convention

`queue.park_counts()` counts only judgement parks, so an infrastructure failure
cannot retire a story. The field is set explicitly at all six park sites, but
nothing enforces that a future site sets it — an omission falls back to
`judgement`, which is the conservative direction (it preserves today's
behaviour) but is silent.
