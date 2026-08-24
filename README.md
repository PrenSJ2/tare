<img src="assets/tare-icon.svg" alt="" width="72" align="right">

# tare

> **tare** *(n.)* the weight of a container, deducted to find the weight
> of the contents.

*The mark (`assets/tare-icon.svg`, also the console's favicon): the outline is
everything installed, the filled band is what is loaded right now, the gap
above it is the vault, and the dashed rule is the tare line a scale is zeroed
to.*

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

## Setup

Everything below runs locally against your own `~/.claude`. Nothing is uploaded
anywhere, and no account or key is needed.

**Requirements:** Python 3.11+, git, and the
[`claude` CLI](https://docs.claude.com/en/docs/claude-code) already set up —
tare reads the transcripts and configuration it leaves behind, so it has
nothing to work with otherwise.

```bash
git clone https://github.com/PrenSJ2/tare.git && cd tare
uv tool install .          # or: pipx install .
```

Then, in this order:

```bash
tare install               # the lookup skill + a SessionStart hook
tare build                 # scan + mine + tag + edges + buckets + index
tare audit                 # what your always-loaded context costs today
```

**`tare install` comes first and is not optional.** The skill and the hook are
what make a shelved capability reachable again; `tare vault --apply` refuses to
run without them, because shelving something you can no longer find is just
deleting it slowly.

`tare build` is the slow one — it shells out to `claude -p` once per untagged
capability to normalise descriptions, and caches by content hash so a re-run is
free. A few hundred capabilities takes a few minutes the first time. Everything
except `tag` works without the CLI if you would rather skip it.

Read `tare audit` before shelving anything. It tells you what your setup costs
per turn and how much of it has never been invoked, which is the number the
rest of the tool exists to move.

## Related repositories

tare works on its own. Two optional pieces extend it, and it degrades to its
own two views without either:

| | |
|---|---|
| **[swarm](https://github.com/PrenSJ2/swarm)** | Reads Claude Code transcripts into a picture of what your subagents actually did. tare's console uses it for the agent views and for showing which shells are running. Also carries `nightshift` (unattended overnight continuation, behind a production gate) and `keepgoing` (a `Stop` hook so a session carries on instead of waiting to be told to). |
| **[tare-console](https://github.com/PrenSJ2/agent-flow)** | The UI. A fork of [patoles/agent-flow](https://github.com/patoles/agent-flow) (Apache-2.0), kept as a git checkout that can still pull from upstream rather than a vendored copy that silently drifts. See `NOTICE.md` there for attribution. |

```bash
# optional — agent views, and `swarm nightshift` / `swarm keepgoing`
git clone https://github.com/PrenSJ2/swarm.git && cd swarm && uv tool install .

# optional — the console UI
git clone https://github.com/PrenSJ2/agent-flow.git && cd agent-flow
pnpm install && pnpm run build:app
```

tare looks for the console at `~/Documents/Code/agent-flow`. If you cloned it
somewhere else, point at it:

```bash
export TARE_CONSOLE_DIR=/path/to/agent-flow
```

**A note if you run the console:** the fork inherits upstream's anonymous usage
telemetry, which is enabled by default and reports to the upstream project's
Supabase. Set `DO_NOT_TRACK=1` to turn it off.

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

## The console

```bash
tare console --start    # the API, on 127.0.0.1:4242
tare viewer --start     # the UI, on 127.0.0.1:3939
```

Three views over the same machine:

- **Agents** — every agent that has ever run, rebuilt from transcripts. A
  hook-driven viewer starts empty and shows only what arrives after it
  launches; this reconstructs the record already on disk, so opening it later
  still tells you everything. Session tabs are named for the repository they
  run in and coloured to match the terminal tab, and an **ALL** tab puts every
  session's agents on one canvas.
- **Skills** — the capability graph on canvas, coloured by domain, with a
  sidebar for how it got this way.
- **Memory** — what usage has taught it, searchable.

Skills and Memory are separate views rather than panels beside the agent
canvas, because neither is scoped to a session: they are properties of the
machine, true whether or not anything is running.

The ALL tab is not just a filter being switched off. The simulation keys
agents by name alone, and every session's root is spawned with the same one,
so merging them raw collapses six orchestrators into one node belonging to
none of them. Names are namespaced per session on the way in
(`homelab#cc75/code-reviewer`) and the session roots are laid out on a ring sized
from the spacing between neighbours, so two sessions sit close together and a
dozen spread out only as far as they must.

The UI is a fork of [agent-flow](https://github.com/patoles/agent-flow), kept
as a git checkout that can still pull from upstream rather than a vendored
copy that silently drifts. See `NOTICE.md` in that repository for attribution.

Both bind loopback with no flag to change it. They read transcripts, file
paths and shell commands — a developer's private working record — so being
unreachable off the machine is a property of the design, not a default.

## Grouping and history

```bash
tare domains            # what each domain holds, and why
tare domains code       # one domain in full
tare history            # what was added, what changed, what a session edited
```

The graph carries 641 distinct tags across 180 capabilities: accurate, and
useless as a grouping. `domains` rolls them into nine — code, marketing,
video, design, process, infra, writing, data, and other — by keyword rules
rather than clustering, so a capability in the wrong domain is a line someone
can read and correct. Every answer reports the term that decided it, in the
CLI and in the console panel both.

`history` reads two records because neither is sufficient alone. The
filesystem knows when each capability appeared and when it last changed:
complete, retroactive, anonymous. Transcripts know which session edited what,
and in which project: attributable, but only where a transcript survives.

That second record is evidence of presence, never of absence. Transcripts age
out, are excluded as tagging exhaust, and never existed for an edit made in an
editor — so a capability with **no recorded session edit was not necessarily
written by hand**, and nothing here claims it was.

## Findings — what you have learned, as opposed to what you can run

```bash
tare recall --reindex "docker containers not starting after reboot"
tare recall "dns name wont resolve but dig works"
```

`lookup` answers *what capability does X*. `recall` answers *what do I already
know about X*: a claim about how something behaves, learned the hard way,
usually at some cost.

Findings live as one small file each in `~/.claude/brain/findings/`, with
frontmatter and `[[wikilinks]]`. Files are the source of truth; the index is
rebuilt from them.

### Scope is the field a CLAUDE.md cannot express

A project instruction file is a per-directory thing, so a hard-won lesson
written in one repository is invisible from every other. That is the single
reason findings left it:

| scope | surfaces |
|---|---|
| `universal` | in every project |
| `tool` | wherever that tool is in play |
| `project` | in its own project, and ranked down elsewhere — never withheld |

Harvested from one real file, `homelab/CLAUDE.md`, **nine of sixteen findings were
universal** — things like "macOS caches negative DNS answers", "`restart:
unless-stopped` does not mean start-on-boot", "a stale process answering looks
like success". All of them were reachable only from a camera project.

### Superseded, not deleted

`superseded_by` points at whatever replaced a finding. The old one stays
searchable and is returned flagged, because *we used to believe X and here is
why we stopped* is the part that prevents re-deriving it. Asking about a
superseded belief returns the current answer first and the history second.

### Out of scope is a weight, not a filter

Ranked down, never hidden. An earlier version sorted on scope instead, and
`limit` then removed project-scoped findings entirely — a search that silently
withheld something you had written, which is precisely the failure this exists
to fix.

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
tare update   which plugins are behind their marketplace
```

## Known limitations

- **`tare tag` shells out to `claude -p`**, which writes a transcript into
  the very corpus `mine` reads. It cannot be prevented while shelling out;
  those sessions are excluded by signature and the count is reported.
- **Not every plugin layout is reached.** `tare audit` prints a coverage
  line naming how many `SKILL.md` files it could not classify. A stated gap is
  a limitation; an unstated one is a wrong answer.
- **A plugin skill's date is when it was cached here**, not when it was
  written, so `history` reports plugin capabilities in their own list rather
  than interleaving them into a timeline that would read as authorship.
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
