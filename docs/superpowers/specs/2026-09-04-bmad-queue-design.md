# A durable work queue for nightshift, read from BMAD

**Status:** design, approved 2026-09-04
**Scope:** one spec. The planning front-end that *produces* BMAD artifacts is
out of scope and stays BMAD's job.

## The problem

`nightshift` can carry a run forward while nobody is watching, and the hard
parts of that are already built: the four-layer safety argument, the clean
`child_env`, the two-tier production gate that tells *doing* from
*describing*, and a ledger written to be read over coffee.

What it does not have is a backlog. `run_shift` takes its next step from
`recommendation_from(last_assistant_text(session))` — a regex over the final
message of a chat, with one `claude -p` extraction as fallback. Three
consequences follow:

- **The work queue is the last message.** Nothing accumulates across nights.
- **Continuity ends at dawn.** Six steps, four hours, one session.
- **"Done" means `exit_code == 0` plus some commits.** There is no acceptance
  criterion, so the loop cannot tell *finished* from *stopped*.

BMAD-METHOD supplies exactly the missing half — a decomposed plan on disk that
survives sessions — and, more usefully, it left a hole where the dispatcher
should be.

## The seam

`stories-schema.md` documents three fields as caller-only:

> `spec_checkpoint` — "Set by the human at breakdown time; read only by the
> dispatching caller, never by the implementing dev skill."
> `done_checkpoint` — "Caller-only, like `spec_checkpoint`."
> `invoke_dev_with` — "Free text appended verbatim to the prompt that
> dispatches this story… Which dev skill to invoke is the caller's
> configuration, never data in this file."

And `bmad-build-auto` describes itself as "One iteration of an unattended
development loop. Use when invoked by name."

BMAD built the plan and one iteration, and left the loop to a caller.
`nightshift` is that caller. Nothing here is a workaround; it is the
documented extension point.

**BMAD owns the plan and one iteration. tare owns the loop, the boundary, and
the record.** Neither writes into the other's tree.

```
  BMAD (read-only to tare)          tare (owns)
  ─────────────────────────         ───────────────────────────
  _bmad-output/specs/<slug>/
    SPEC.md                    ──►  queue.py     ordered stories
    stories.yaml                    worktree.py  per-story isolation
    stories/<id>-*.md          ──►  nightshift   screen → dispatch → verify
    .memlog.md                      ledger.jsonl what ran, and whether it worked
  _bmad/bmm/config.yaml        ──►
  skill: bmad-build-auto       ◄──  dispatched by name, one story per iteration
```

### Why `stories.yaml` and not the epics markdown

The three caller-only fields exist nowhere else. That file is the one with a
dispatcher contract. The alternative path — `epics.md` plus
`sprint-status.yaml` — belongs to `bmad-dev-story`, which BMAD marks
*"Deprecated: `bmad-build` is now the official implementation method."*
Building against a deprecated generation would be a coupling with a known
expiry date.

### Why tare owns completion status

Validity rule 3 of the stories schema: **"No `status` field, ever."** BMAD
deliberately keeps execution state out of the plan.

So a story is done when tare's ledger records a *verified* completion for its
id — derived on read, never written back into BMAD's tree. This preserves the
position tare already takes elsewhere: one fact, one home, and the copy is what
rots.

## Components

| unit | does | depends on |
|---|---|---|
| `swarm/queue.py` *(new)* | Find spec folders, parse and validate `stories.yaml`, subtract ledger-completed ids, return the next runnable story | ledger, `_bmad/bmm/config.yaml` |
| `swarm/worktree.py` *(new)* | Per-story `git worktree` on `nightshift/<slug>/<id>`, install the pre-push hook, dispose on exit | git only |
| `swarm/nightshift.py` *(changed)* | Take the step from the queue; dispatch `bmad-build-auto`; parse the headless JSON | queue, worktree, verify |
| `swarm/verify.py` *(new)* | Read-only pass: do this story's acceptance criteria hold against the diff? | nothing — read-only tools |

Paths are resolved from `_bmad/bmm/config.yaml` (`implementation_artifacts`),
never hardcoded. A non-default BMAD install is the common case, not an edge
one.

## One iteration

```
 1  queue.next()
      enumerate  _bmad-output/specs/*/stories.yaml
      parse in list order, validate ids against the schema's four rules
      subtract ids the ledger records as VERIFIED complete
 2  checkpoints
      spec_checkpoint: true  -> park + advance   (a human is meant to review; there isn't one)
      done_checkpoint: true  -> run it, then end the shift
 3  screen(title + description + invoke_dev_with)      <- existing two-tier gate, unchanged
      hit -> park + advance, record verdict + matched pattern
 4  worktree.create()   nightshift/<slug>/<id> off HEAD, pre-push hook installed
 5  dispatch            claude -p in the worktree, clean child_env, acceptEdits,
                        widened allowlist, prompt = preamble + "bmad-build-auto, story <id>"
                        + invoke_dev_with verbatim
 6  parse headless JSON
      {"status":"blocked", error_code, reason} -> record, park, advance
      {"status":"complete", files:[...]}       -> verify
 7  verify.check()      read-only claude -p: do the acceptance criteria hold against this diff?
      verified     -> completion entry in ledger, push branch, gh pr create
      not verified -> record the gap, push the branch, open NO pr, story stays open
                      (the work is preserved and reviewable; it is not offered as done)
 8  loop, subject to the backstops below
```

### Step 3 is the most important line in the file

`invoke_dev_with` is free text appended verbatim to a dispatch prompt, and that
prompt now runs under a widened tool policy. It is an injection surface
reaching straight at the loosest capability set in the system. It goes
**through** `screen()`, never around it.

### Step 7 is what makes this development rather than a cron job

Today `done` is an exit code and some commits. Here a story leaves the queue
only when a separate read-only pass says its acceptance criteria hold. An
unverified story is not a failure; it is simply still open, and it comes back
tomorrow. That distinction is the entire reason a durable queue earns its
keep.

## The safety model

The user chose to widen the tool policy, having been shown that
`nightshift.py:33` calls it *"the only real control."* That choice is taken as
given. What the design owes in return is a replacement boundary, because
widening tools with nothing behind them leaves no control at all.

| | before | after |
|---|---|---|
| 1 prompt | a request | unchanged — still not counted on |
| 2 refusal list | screens a recommendation | screens story text **and `invoke_dev_with`**; now load-bearing |
| 3 tool policy | narrow allowlist, "the only real control" | widened — **no longer the control** |
| 4 clean `child_env` | parent cannot answer prompts | unchanged, still essential |
| 5 worktree + pre-push hook | — | **the new boundary** |
| 6 branch check | refuse on a default branch | structural: work only ever lands on `nightshift/*` |

### The boundary is enforced by git, not by a prompt pattern

`--allowedTools` cannot express "push only to `nightshift/*`" — matching is
prefix-based and `git push origin HEAD:main` walks straight through it. So the
restriction is a **`pre-push` hook installed into the worktree** that rejects
any ref outside `refs/heads/nightshift/`. Deterministic, inspectable, fails
closed: the same properties layer 2 already has, enforced by git rather than by
asking politely. `gh pr merge` stays in `DENIED_TOOLS` as defence in depth.

### What this does not claim

The module docstring currently ends: *"None of that is a sandbox, and this
module does not claim otherwise."* That honesty is the best thing in the file
and must be updated in the same voice rather than quietly dropped, because the
claim it makes is now different and narrower:

> A worktree buys **reviewability, not confinement**. Nothing merges;
> everything is a diff read in the morning. Under the widened policy,
> filesystem writes outside the repository and network egress are
> unconstrained.

`CONTINUATION_PREAMBLE` also needs rewriting: it currently says "Do NOT push"
and "Stay on the current branch", both of which are false in this mode.

`acceptEdits` stays rather than `bypassPermissions` — the latter would void
`DENIED_TOOLS` too.

### Backstops

Refusal is no longer terminal, so the exits it used to provide are replaced by:

- `max_steps` and `max_minutes`, both configurable, defaults raised.
- The queue running dry, or every remaining story being parked.
- A **consecutive-failure limit**, default 3: three dispatches in a row
  returning `blocked` or unverified ends the shift. That pattern means
  something is systematically wrong, and burning the night on it produces
  nothing worth reading.
- The 21:00–07:00 window becomes optional — off by default, `--window` restores
  it — consistent with the rest of the widened envelope.

## Error handling

The organising rule: **never let a wrong answer look like an empty one.**

| failure | behaviour |
|---|---|
| BMAD absent or half-installed | `--queue bmad` refuses to start, naming what is missing. No silent fallback to session mode: believing you are running a plan while running a chat message is the worst outcome available. Mode is explicit — `bmad` or `session`. |
| Malformed `stories.yaml` | Validate on load against the schema's four rules; refuse the whole file, naming the rule violated. Unquoted `id: 1` is the one that will actually happen — YAML coerces it to an integer and string comparison silently stops matching. |
| BMAD format drift | Their layout is our API, and issue #1002 shows their own templates drifting from their own validators. `swarm doctor` gains a check: does this install still match what `queue.py` expects? Drift is reported as drift, never as an empty queue. |
| Worktree lifecycle | Deterministic naming makes a stale tree identifiable. `doctor` reports orphaned `nightshift/*` worktrees. Creation reuses-or-refuses rather than silently working in the wrong tree, and never force-removes a tree holding uncommitted work. |
| No parseable headless JSON | The model narrated instead of returning the contract. Treated as `blocked`, `error_code: no_contract`, raw tail retained. Never inferred as success from the exit code — that inference is precisely what this design exists to delete. |
| Verification wrongly says no | Verdict and reasoning recorded; the story is re-queued, not failed. Bias runs toward not-verified, because marking done wrongly is the expensive error. Capped: three failed verifications park the story for a human, or it consumes every night indefinitely. |
| Push or PR failure | Auth failure leaves the branch local and the ledger says *unpushed*. A pre-push hook rejection is a design bug, not a normal outcome — recorded loudly, because it means something attempted to push outside the boundary. |

## Testing

Framed by the rule this repository already states: *a green suite proves the
code does what the tests say; it does not prove the tests say the right thing.*

- **The pre-push hook is tested against refs that are not obviously wrong.** The
  `curl` finding is the precedent — the earlier layer-3 claim was false because
  the test only exercised a denylisted command. Assert rejection of
  `nightshift-evil/x` and `refs/heads/main`, not merely `main`.
- **BMAD fixtures are copied verbatim** from their repository — the
  `stories-schema.md` example, the epics template — never hand-written
  approximations, which test only our idea of their format.
- **Ledger status is read back from a fresh process.** Directly the lesson from
  the four scanners that returned without `conn.commit()`: a test that reads on
  the writing handle proves nothing.
- `screen()` exercised on real `invoke_dev_with` prose, including the case where
  that free text names a production action.
- Round trip: a verified story must not re-dispatch; an unverified one must.
- **Validated against a real `npx bmad-method install` before any of it is
  believed.** Fixtures alone would reproduce the failure this repository
  already documents.

## Out of scope

- The planning front-end. BMAD produces `SPEC.md` and `stories.yaml`; tare does
  not generate them.
- Container isolation. The worktree boundary is designed with a seam for it, and
  it is a later decision, not this spec.
- Anything that merges. `gh pr merge` stays denied; a human reads the PR.

## Open coupling risks, recorded rather than resolved

1. **BMAD's file layout is now an API we do not control.** `swarm doctor`
   detects drift; it cannot prevent it.
2. **`bmad-build-auto` shells through `uv run render_skill.py`.** That is
   another moving part inside the dispatch path, and its failure mode is a
   HALT we must read as `blocked`.
3. **A user-scope BMAD install lands ~72 skills in `~/.claude`** as
   `origin='user-authored'` — which makes them vault-eligible. Whether the
   `routes-to` guard holds against BMAD's menu-style dispatch is untested, and
   it is the first thing to check on a real install.
