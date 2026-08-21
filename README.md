# tare

> **tare** *(n.)* the weight of a container, deducted to find the weight
> of the contents.

Most of a Claude Code configuration is listed in every prompt and almost never
used. `tare` inventories what you have, mines your own transcripts for what
you actually invoke, and moves the rest off the always-loaded path — while
keeping every bit of it findable.

Measured on the machine this was built for:

```
before   ~19,839 tokens per turn, 87% of it never invoked
after    ~10,191 tokens per turn
```

Nothing was deleted. 64 capabilities moved into a git-backed vault, 28 plugin
skills were promoted onto the load path, three wholly-unused plugins were
switched off, and `tare lookup` still returns all of them.

## The bargain

A shelved capability keeps its node in the graph with `state='vaulted'` and is
still returned by search, labelled as shelved, with the command to bring it
back. **The context cost drops; the capability does not.** A token saving that
loses a capability is a regression, not a win, and the test suite asserts that
directly.

## Install

```bash
uv tool install --editable .
tare install      # writes the lookup skill and a SessionStart hook
tare build        # scan + mine + tag + edges + buckets + index
tare audit        # what your always-loaded context costs
```

`tare install` comes first and is not optional: the skill and hook are what
make a shelved capability reachable, so `vault --apply` refuses to run without
them.

## Shelving

```bash
tare vault              # dry run — what would be shelved, and what it saves
tare vault --apply      # do it
tare activate <name>    # bring one back
tare deactivate <name>  # shelve it again
tare doctor             # check nothing has drifted
```

Dry run is the default everywhere, and it is a faithful preview: both modes run
the same planning code, because a dry run that claims actions the real run
would skip is a false authorisation.

Three mechanisms:

1. **User-authored skills and agents** move into `~/.claude/vault/`, a git
   repository — one commit per stash.
2. **Wholly-unused plugins** are switched off in `settings.json`. Plugin files
   are never moved.
3. **Mixed plugins** — some skills used, most not — have the needed skills
   symlinked onto the load path *first*, then the plugin is disabled.

## The guard, which is the part that matters

"Never invoked" is inferred from transcripts, and transcripts record
capabilities dispatched **by name**. An orchestrator dispatches its own
sub-skills, so those sub-skills never appear by name and read as never-invoked
— *precisely because something else invokes them*. The usage signal misses them
by construction.

On the real configuration, `hyperframes` (used the previous day) routes to 20
shelving candidates, and `marketing-plan` routes to 14 skills inside the plugin
that was about to be disabled — it would have promoted a skill because it was
used, then removed everything it depends on.

So nothing reachable via `routes-to` from a capability with recorded
invocations is ever a candidate. It costs about 15% of the available saving and
removes the risk of breaking a suite you use daily. `overlaps` deliberately
does not protect: `code-reviewer` overlaps `architect-reviewer` without
depending on it.

## What it learns from being used

```bash
tare learned             # gaps, wrong shelving decisions, evidence
tare learned --here      # narrowed to this project
tare learned --projects  # what each project leans on
```

Every other memory system for coding agents remembers *facts* — project
decisions, conversation history — and every one reports the same unsolved
problem: the store accretes, nothing prunes, and stale entries sit beside
current ones with no signal which is which.

This remembers **use**, which rots differently. A lookup that happened stays
true; it loses relevance with age rather than becoming wrong, so decay is
enough. Three signals are recorded as durable events:

| | |
|---|---|
| `lookup` | a search, and what it returned |
| `miss` | a search that surfaced nothing — a capability gap |
| `activation` | a shelved capability pulled back — a shelving decision that was wrong |

Note that a relevance *score* cannot detect a useless search here. Measured
against the real index, a bad match (`systematic-debugging` for "quantum error
correction", 8.36) scores between two good ones (`ai-engineer` 8.25,
`content-marketer` 8.53). Only an empty result set is a hard miss; the real
signal is behavioural — a query asked repeatedly that never led to using
anything.

It reports and changes nothing. Letting usage silently re-rank search would
make the results unexplainable the first time they surprised you.

## Where knowledge lives

Three kinds, three homes, no duplication:

| | where | measured by |
|---|---|---|
| what you use, and where | its own event log, per project | `tare learned` |
| how tools are configured here | that project's `CLAUDE.md` | `tare learned --here` points at it |
| general project knowledge | that project's `CLAUDE.md` | `tare audit` costs it |

`tare` deliberately does not store project facts. They already have a home
that sits beside the code it describes and goes stale visibly; copying them
into a database would create a third source of truth, and the copy is what
rots.

It does now **measure** them, because a project's instruction file loads on
every turn exactly like the skill index — and on this machine the largest is
~37,800 tokens against a whole capability index of ~10,200. Auditing
capabilities while ignoring that measures the smaller half of the problem.

## Everything else

```
tare scan     read ~/.claude into the node table
tare mine     mine transcripts for real usage
tare tag      normalise descriptions (hash-cached)
tare build    all of the above, plus edges, buckets and the search index
tare doctor   check the vault and installation for drift
```

## Known limitations

- **`tare tag` shells out to `claude -p`**, which writes a transcript into
  the very corpus `mine` reads. It cannot be prevented while shelling out;
  those sessions are excluded by signature and the count is reported.
- **Not every plugin layout is reached.** `tare audit` prints a coverage
  line naming how many `SKILL.md` files it could not classify. A stated gap is
  a limitation; an unstated one is a wrong answer.
- **`update` and `graph` are not implemented** — upstream drift reporting and
  the HTML export.
- **`deactivate` cannot re-disable a plugin.** Re-enabling one is a settings
  change, not a vaulted file; `tare vault --apply` will disable it again.

## Notes for anyone working on this

`the design notes` is worth reading first. This repository was destroyed
once and rebuilt from those notes, and its addendum records six defects that
**290 passing tests did not catch** — including all four scanners returning
without `conn.commit()`, so every scan wrote nothing to disk while the suite
passed, because every test reused one connection where uncommitted writes are
still visible.

The habit that found them: validate against real data and a preserved copy of
the live database, not fixtures alone.
