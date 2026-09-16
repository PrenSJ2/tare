# A session the console can tell is over

**Status:** design, approved 2026-09-11
**Scope:** one spec, covering the Agents view only. The Orchestration panel has
the same defect and is deliberately left alone; see *Out of scope*.

## The problem

The Agents view is rebuilt from transcripts rather than driven by hooks, and
that is the point of it — a hook-driven viewer starts empty, this one opens
later and still tells you everything. But it reconstructs history with no sense
of *when*, so a session that ended on Sunday renders exactly like one running
now.

Measured on the machine this was built for, 2026-09-11. Of the 25 sessions in
the fleet window, 4 contributed agents and 21 contributed none; every one of
the 172 agents reported `status: "done"`:

```
teamworks     0.5h ago   73 agents
vive-claude   0.5h ago   31 agents
tare         33.5h ago   38 agents   ← over
vive         45.7h ago   30 agents   ← over
```

Three separate things are wrong.

**Nothing in the payload records liveness.** `AgentRun.status` is per-agent and
resolves to `done` for anything older than `STALE_AFTER_SECONDS`. There is no
session-level state at all, so the UI could not sort by it even if it wanted
to.

**`fleet()` destroys session identity.** It returns
`dict[project, list[AgentRun]]`, so a live session and a dead one in the same
repository arrive merged into one project tab, unrecoverably.

**Twenty-one of twenty-five sessions are parsed for nothing**, and the parse is
where the payload's cost lives:

```
all_sessions()        14.6 ms
read_session x25    1484.4 ms   ← 86% of the payload
+ detail() x220      234.0 ms   (warm cache)
```

## The bargain, applied to the agent half

The capability half's rule is that a shelved capability keeps its node and
stays findable — *the context cost drops; the capability does not.* The same
rule governs here. A session folded out of the default view must still be
fully reachable, and the test suite asserts it directly (see *Tests*).

This is why the answer is a fold and not a shorter window. Cutting `fleet()` to
"sessions touched in the last 24h" would be three lines and would genuinely
lose history.

## What decides that a session is over

Two signals, in order of certainty.

**`session_end`, from swarm's own stream.** `swarm install` already registers a
`SessionEnd` recording hook, and `project()` already writes the record with its
`reason`. When one exists for a session id, that session is over and we know
it -- *while the record is still fresh*. `claude --resume` / `claude -c`
reuses the session id and keeps appending to the same transcript, so the
record only says the session ended ONCE, not that the id is retired. A
transcript written to long after the record is proof the record is stale, and
that case is corrected below (see *What the rule does on the real corpus*).

**Transcript mtime, for everything else.** No `session_end` and no write in
`SESSION_COLD_AFTER_SECONDS` → presumed over.

**The payload reports which signal fired.** This is the same discipline
`shells.py` follows when it says *project* where it cannot say *session*: the
data never claims a certainty it does not have. A session that ended cleanly
renders differently from one we merely infer is finished, and the difference is
legible to the reader, not buried in a heuristic.

The process table was considered as a third signal and rejected. A `claude`
process exposes its working directory and not its session id, so it can only
ever speak at project granularity — in a repository with two sessions open it
would mark both live or neither. This machine has three concurrent sessions in
`regulatoryAi` and two each in `teamworks` and `vive`, so that is not a corner
case here.

### What the rule does on the real corpus

Running the proposed rule over the same 25 sessions:

```
live  12    ended 13    cold  0

ended · cctv          0.2h   other               ← warm mtime, definitively over
ended · vive          3.0h   prompt_input_exit
ended · vive         20.9h   clear
ended · tare         67.2h   prompt_input_exit
… 9 more
```

**Correction, added at final review — the cctv row above was wrong, and it is
worth saying so plainly rather than quietly editing the claim away.** This
spec originally read: *"The cctv row is the design earning its keep: written
twelve minutes ago, so mtime alone would have called it live, and
`session_end` overrides correctly."* That is false. cctv's transcript was not
written twelve minutes before this table — it was written 278 hours (11.6
days) after its `session_end` record. `claude --resume` reuses the session id
and keeps appending to the same transcript, so a `session_end` does not retire
the id; it only records that the session ended ONCE. cctv had been resumed
after that record was written, mtime was the correct signal the whole time,
and this design as first written would have reported it `ended` permanently —
exactly the error this spec says must never happen: *"a live-looking dead
session is a smaller lie than a hidden live one."* A second session
(`bookteamworks`, not part of this original 25-session window) showed the same
pattern, 69 hours stale. Both are why `session_state()` now discards a
`session_end` record once the transcript was written more than
`SESSION_END_STALE_AFTER_SECONDS` (120s) after it, and falls through to mtime
instead — the reader module documents the measured gap between a normal end
(≈0 or slightly negative) and a resumed one (thousands of minutes) that makes
120s a safe cutoff. Re-run at fix time, on a later 25-session window (time had
moved on, so it is not the identical set of sessions above), the counts moved
from `live 12 / ended 13 / cold 0` to `live 14 / ended 11 / cold 0`, with
exactly the two resumed sessions reclassified from `ended` to `live` and
nothing else in the window affected.

**The `cold` tier fires zero times here**, because every session in the window
that is over has a `session_end` record. Across all 191 streams on disk only 81
do (42%), but that is a historical figure from before the recording hooks were
installed. The mtime tier is retained regardless: it covers `kill -9` and a
closed terminal, and it is the only tier that works on a machine where swarm's
hooks were never registered. It must not be dropped merely because the
best-instrumented corpus does not currently exercise it — the tests exercise it
directly instead.

### The window

`SESSION_COLD_AFTER_SECONDS = 3600`, its own constant rather than a reuse of
the agent-level `STALE_AFTER_SECONDS = 900`. Sessions idle far longer than
agents do; a 15-minute window drops a session you are still in into the fold
over a coffee, and pops it back when you type. An hour survives lunch and does
not survive overnight, which is the distinction that matters.

## The second problem: live and empty

Folding the dead sessions takes the view from 25 rows to 12. It does not fix
it, because those 12 are real but nearly all idle (sampled a few hours after
the figures above, which is why teamworks has grown):

```
teamworks      0.0h    88 agents
regulatoryAi   0.0h     0 agents
vive           0.0h     0 agents
tare           0.0h     0 agents
… 8 more, all 0 agents
```

Eleven of the twelve live sessions have dispatched nothing. The process table
confirms they are genuinely open — 13 `claude` processes are running, one per
repository — so this is not a detection error to fix but a presentation
question to answer.

**A live session that dispatched nothing gets no heading.** Those sessions
collapse into one line stating how many are open and idle. The information is
preserved; the canvas is not spent on it.

## Components

### 1. `swarm/reader.py` — session state

```python
SESSION_COLD_AFTER_SECONDS = 3600

@dataclass
class SessionState:
    session: str
    project: str
    state: str          # "ended" | "live" | "cold"
    by: str             # "session_end" | "mtime" — which signal decided
    age: float          # seconds since the transcript's last write
    reason: str | None  # the session_end reason, when by == "session_end"
```

`session_state(project, session, *, now=None) -> SessionState` looks for
`runs_dir()/*-{session}.jsonl` and scans for `event == "session_end"`, keeping
the LATEST such record rather than the first — a resumed session can carry
more than one. Found, and fresh (the transcript was not written more than
`SESSION_END_STALE_AFTER_SECONDS` after it — see the correction above) →
`ended`, `by="session_end"`, carrying the reason. Not found, or found but
stale → `age` against the window → `live` or `cold`, `by="mtime"`.

A session's stream can span several files when it crosses UTC midnight, so all
matching files are scanned. Results are cached on `(path, mtime, size)`,
following the existing `_SPAN_CACHE` pattern.

`reason: "clear"` correctly yields `ended`. `/clear` retires that session id
even though the operator keeps working; the new work has a new id.

### 2. `swarm/reader.py` — `fleet()` keeps sessions

```python
@dataclass
class SessionRuns:
    session: str
    project: str
    state: SessionState
    runs: list[AgentRun]   # empty when unread
    read: bool             # False = cold, deliberately not parsed
```

`fleet(*, redact=False, now=None, sessions=25, include_cold=False)` returns
`list[SessionRuns]`, live sessions first and newest-first within each group.

State is computed for all 25 candidates first — a `stat` and at most one small
file read each. A transcript is parsed only when `state.state == "live"`, or
when `include_cold=True`.

`read=False` is load-bearing. Without it the UI cannot distinguish *this
session dispatched no agents* from *nobody looked*, and would render "0 agents"
as a fact about a session that was never opened.

**This is a breaking change to `fleet()`'s return type.** Its only callers are
`console._fleet` and the tests.

### 3. `tare/console.py`

`_fleet` emits sessions nested under projects, each carrying its state, and
passes `include_cold` through.

A cold session's agent count costs exactly the parse being skipped, so the
default payload cannot carry one. Cold and ended sessions are served by a
second request — `/api/data?earlier=1`, following the `/api/timeline?agent=`
pattern already in `do_GET` — made once when the fold is opened. The 4-second
poll never pays for them.

`_PAYLOAD_CACHE` keys on `(redact, include_cold)` so the two variants do not
evict each other.

### 4. `tare/web/console.html`

```
AGENTS

● teamworks                    live · 88 agents
    <agent rows as today>

  11 other sessions open, none dispatching

▸ earlier — 13 sessions · last over 12m ago      [expand]
```

Live sessions that dispatched agents render as they do today. Live sessions
with none collapse to the count line above — not hidden, not itemised.

The fold covers **every** session the rule calls `ended` or `cold`, not only
those that dispatched agents: which of them did cannot be known without the
parse being skipped. Its summary carries a session count and the age of the
most recent, not an agent count, for the cost reason above.

Expanding fetches `earlier=1` and renders those sessions dimmed, each labelled
with why it is considered over — `ended · prompt_input_exit` against `cold · no
write in 33h`. Once expanded the parse has run, so *dispatched nothing* is a
fact rather than an unknown, and those sessions collapse to a trailing count
line there too. The two that matter on this corpus — tare at 33h, vive at 46h —
render in full.

The **ALL** tab requests `include_cold` and is unchanged in what it shows.

## Data flow

```
all_sessions()  ──▶  session_state() per candidate   (stat + small read)
                          │
                     live │ ended/cold
                          ▼
                    read_session()          skipped, read=False
                          │                        │
                          ▼                        ▼
                  default payload          /api/data?earlier=1
                          │                        │
                          └──────▶ console.html ◀──┘
                            dispatching first, idle counted, fold below
```

## Failure modes

- **No `runs/` directory** (swarm's hooks were never installed, or the session
  predates them): every session falls through to mtime, `by="mtime"`
  throughout. Degrades; does not break. This is the path the 110 uninstrumented
  streams on this machine would take.
- **`stat` fails**: treated as cold. A transcript that cannot be read is not a
  session we should claim is live.
- **Corrupt stream line**: `_iter_json` skips it. A session whose `session_end`
  was lost is inferred by mtime like any other.
- **Future mtime** (clock skew): `age` clamps at 0 and the session reads live.
  Wrong in the safe direction; a live-looking dead session is a smaller lie
  than a hidden live one.
- **`/api/data?earlier=1` fails**: the fold shows the error in place and the
  live section is untouched.

## Tests

`tests/swarm_reader.py`:

- each state and each deciding signal: `session_end` present and fresh →
  `ended`; absent and warm → `live`; absent and cold → `cold`
- the window boundary, either side of 3600s
- a stream spanning two days still resolves its `session_end`
- `reason` is carried through, and `"clear"` yields `ended`
- missing `runs/` → every session resolves `by="mtime"`, exercising the tier
  the live corpus does not reach
- a `session_end` stale by more than `SESSION_END_STALE_AFTER_SECONDS` falls
  through to `mtime` — the resumed-session case the correction above exists
  for — with both sides of that boundary asserted
- two `session_end` records for one session, across two day-files, report the
  LATEST reason, not the oldest

**Nothing is lost.** `fleet(include_cold=True)` returns exactly the agents the
current `fleet()` returns, for the same corpus. This is the README's bargain
applied to the agent half: a view that hides a session must still be able to
produce it.

**Cold sessions are not parsed.** Assert `read=False` and that the parse never
ran, so the saving cannot silently regress into a full walk.

`tests/test_console.py`:

- the payload shape carries `state`, `by` and `read` per session
- a live session with no agents is counted, not itemised
- `include_cold` reaches `fleet` and the two cache variants do not collide

## Out of scope

`console._orchestration` has the same defect — `all_sessions()[:14]`, keep the
first 4 that dispatched anything — so it will happily build a dispatch tree out
of a session that ended on Sunday. The same `session_state` call fixes it, but
it is a different surface with its own rendering, and widening this spec to
cover it would blur what is being tested here. It should be its own change,
next.
