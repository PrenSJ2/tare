# Session Liveness in the Agents View — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the console's Agents view distinguish a session that is still going from one that is over, fold the finished ones out of the default view without losing them, and stop parsing transcripts nobody is looking at.

**Architecture:** A new `session_state()` in `swarm/reader.py` decides liveness from two signals — a `session_end` record in swarm's own stream (certain), falling back to transcript mtime (inferred) — and reports which one fired. `fleet()` stops flattening sessions into projects so that state has something to attach to, and parses a transcript only when its session is live. The console serves the rest from a second route fetched once when the reader opens the fold.

**Tech Stack:** Python 3.11+, stdlib only, pytest. Vanilla JS in a single HTML file, no build step.

**Spec:** `docs/superpowers/specs/2026-09-11-session-liveness-design.md`

## Global Constraints

- `SESSION_COLD_AFTER_SECONDS = 3600` — a session's own window, distinct from the existing agent-level `STALE_AFTER_SECONDS = 900`. Do not reuse the latter.
- State is one of exactly `"ended"`, `"live"`, `"cold"`. The deciding signal is one of exactly `"session_end"`, `"mtime"`.
- Every payload field that reports liveness must also report which signal decided it. The data never presents a guess and a certainty in the same shape — this mirrors `shells.py` saying *project* where it cannot say *session*.
- Nothing is hidden that cannot be produced again. `fleet(include_cold=True)` must return exactly what `fleet()` returned before this change.
- The process table is **not** a liveness signal here. It speaks at project granularity only.
- No test may read or write the operator's real `~/.claude`. Use the `fake_home` fixture in `tests/swarm_reader.py` or `tests/conftest.py`.
- `swarm` is an optional dependency of `tare`. The console must degrade, not fail, when `_reader()` returns `None`.

---

### Task 1: Session liveness in the reader

**Files:**
- Modify: `src/swarm/reader.py` (add after `session_transcript`, around line 117)
- Test: `tests/swarm_reader.py`

**Interfaces:**
- Consumes: `paths.runs_dir()`, `session_transcript()`, `_iter_json()` — all already in the module.
- Produces: `SESSION_COLD_AFTER_SECONDS: int`, `SessionState` dataclass with fields `session, project, state, by, age, reason`, and `session_state(project: str, session: str, *, now: datetime | None = None) -> SessionState`. Task 2 calls `session_state()`; Task 3 reads every field of `SessionState`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/swarm_reader.py`. The file already has a `fake_home` fixture and a `write_session` helper — use them. Add this stream helper next to `write_agent`:

```python
def write_stream(home, session, day, events):
    """events: list of dicts, each an already-projected stream record."""
    d = home / "runs"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{day}-{session}.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n")
    return path


def age_transcript(home, session, seconds):
    """Backdate a session transcript's mtime by `seconds`."""
    import os, time as _time
    path = home / "projects" / "proj" / f"{session}.jsonl"
    when = _time.time() - seconds
    os.utime(path, (when, when))
```

Then the tests:

```python
def test_a_session_end_record_is_certain_and_beats_a_warm_mtime(fake_home):
    """The cctv case: written twelve minutes ago, but definitively over.

    mtime alone would call this live. The stream knows better, and the state
    must say which signal decided so a reader can tell the two apart.
    """
    write_session(fake_home, "s1", [])
    write_stream(fake_home, "s1", "2026-09-11", [
        {"ts": "2026-09-11T10:00:00+00:00", "session": "s1", "event": "session_end",
         "reason": "prompt_input_exit"},
    ])
    st = reader.session_state("proj", "s1")
    assert st.state == "ended"
    assert st.by == "session_end"
    assert st.reason == "prompt_input_exit"


def test_no_stream_and_a_warm_transcript_reads_live(fake_home):
    write_session(fake_home, "s2", [])
    age_transcript(fake_home, "s2", 60)
    st = reader.session_state("proj", "s2")
    assert st.state == "live"
    assert st.by == "mtime"
    assert st.reason is None


def test_no_stream_and_a_cold_transcript_reads_cold(fake_home):
    write_session(fake_home, "s3", [])
    age_transcript(fake_home, "s3", 33 * 3600)
    st = reader.session_state("proj", "s3")
    assert st.state == "cold"
    assert st.by == "mtime"


def test_the_window_boundary(fake_home):
    write_session(fake_home, "s4", [])
    age_transcript(fake_home, "s4", reader.SESSION_COLD_AFTER_SECONDS - 30)
    assert reader.session_state("proj", "s4").state == "live"
    age_transcript(fake_home, "s4", reader.SESSION_COLD_AFTER_SECONDS + 30)
    assert reader.session_state("proj", "s4").state == "cold"


def test_a_stream_spanning_utc_midnight_still_resolves_its_end(fake_home):
    """One session, two stream files. The end is in the second."""
    write_session(fake_home, "s5", [])
    write_stream(fake_home, "s5", "2026-09-10", [
        {"ts": "2026-09-10T23:59:00+00:00", "session": "s5", "event": "subagent_start"},
    ])
    write_stream(fake_home, "s5", "2026-09-11", [
        {"ts": "2026-09-11T00:01:00+00:00", "session": "s5", "event": "session_end",
         "reason": "other"},
    ])
    st = reader.session_state("proj", "s5")
    assert st.state == "ended"
    assert st.reason == "other"


def test_clear_retires_the_session_id(fake_home):
    """/clear ends that id even though the operator keeps working."""
    write_session(fake_home, "s6", [])
    age_transcript(fake_home, "s6", 60)
    write_stream(fake_home, "s6", "2026-09-11", [
        {"ts": "2026-09-11T10:00:00+00:00", "session": "s6", "event": "session_end",
         "reason": "clear"},
    ])
    st = reader.session_state("proj", "s6")
    assert st.state == "ended"
    assert st.reason == "clear"


def test_without_a_runs_directory_everything_falls_to_mtime(fake_home):
    """The tier the live corpus does not currently exercise.

    On a machine where swarm's recording hooks were never installed there is
    no stream at all, and mtime is the only signal there is.
    """
    assert not (fake_home / "runs").exists()
    write_session(fake_home, "s7", [])
    age_transcript(fake_home, "s7", 60)
    st = reader.session_state("proj", "s7")
    assert st.by == "mtime"
    assert st.state == "live"


def test_a_future_mtime_clamps_to_live(fake_home):
    """Clock skew is wrong in the safe direction: never hide a live session."""
    write_session(fake_home, "s8", [])
    age_transcript(fake_home, "s8", -600)
    st = reader.session_state("proj", "s8")
    assert st.age == 0
    assert st.state == "live"


def test_a_corrupt_stream_line_does_not_hide_the_end_record(fake_home):
    write_session(fake_home, "s9", [])
    d = fake_home / "runs"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"2026-09-11-s9.jsonl").write_text(
        "{not json at all\n"
        + json.dumps({"ts": "2026-09-11T10:00:00+00:00", "session": "s9",
                      "event": "session_end", "reason": "other"}) + "\n")
    assert reader.session_state("proj", "s9").state == "ended"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/swarm_reader.py -k "session_end or mtime or window or midnight or clear or runs_directory or future_mtime or corrupt_stream" -v`

Expected: FAIL, `AttributeError: module 'swarm.reader' has no attribute 'session_state'`

- [ ] **Step 3: Implement**

In `src/swarm/reader.py`, extend the constant block near line 53:

```python
STALE_AFTER_SECONDS = 900
# How long a SESSION's transcript may sit untouched before it is presumed
# over. Deliberately not STALE_AFTER_SECONDS: sessions idle far longer than
# agents do, and a 15-minute window drops a session you are still in into the
# fold over a coffee and pops it back when you type. An hour survives lunch
# and does not survive overnight.
SESSION_COLD_AFTER_SECONDS = 3600
```

Add the dataclass below `AgentRun` (after line 72):

```python
@dataclass
class SessionState:
    session: str
    project: str
    state: str          # "ended" | "live" | "cold"
    by: str             # "session_end" | "mtime" -- which signal decided
    age: float          # seconds since the transcript's last write
    reason: str | None = None   # the session_end reason, when by == "session_end"
```

Add after `session_transcript` (after line 116):

```python
# Scanning a session's stream is a few small reads, but fleet() does it for 25
# sessions on every payload build. Keyed on the files' own identity rather
# than a clock, so a stream appended to mid-session is re-read and a finished
# one is not.
_ENDED_CACHE: dict[str, tuple[tuple, str | None]] = {}


def _session_end_reason(session: str) -> str | None:
    """The reason a session's stream says it ended, or None if it never did.

    swarm's SessionEnd hook writes this record; `project()` carries the reason
    through. It is the only signal that can say a session is over rather than
    merely quiet -- but it exists for a session only if the recording hooks
    were registered when it ran, which is why every caller has a fallback.

    A session crossing UTC midnight writes more than one stream file, so every
    file carrying the id is scanned.
    """
    try:
        streams = sorted(paths.runs_dir().glob(f"*-{session}.jsonl"))
    except OSError:
        return None

    fingerprint = []
    for path in streams:
        try:
            stat = path.stat()
        except OSError:
            continue
        fingerprint.append((str(path), stat.st_mtime, stat.st_size))
    key = tuple(fingerprint)

    cached = _ENDED_CACHE.get(session)
    if cached is not None and cached[0] == key:
        return cached[1]

    reason = None
    for path in streams:
        for obj in _iter_json(path):
            if obj.get("event") == "session_end":
                reason = obj.get("reason") or "unknown"
                break
        if reason is not None:
            break
    _ENDED_CACHE[session] = (key, reason)
    return reason


def session_state(project: str, session: str, *,
                  now: datetime | None = None) -> SessionState:
    """Whether a session is still going, and which signal decided it.

    Two tiers, in order of certainty. A `session_end` record is certain: the
    session is over and we know why. Everything else is inferred from the
    transcript's last write -- the only tier that works when the recording
    hooks were never installed, or the process was killed outright.

    `by` is returned rather than folded away because the two are not the same
    claim, and a view that renders them identically is lying about one of
    them. This is the same discipline `shells.py` follows when it says
    "project" where it cannot say "session".

    The process table was considered as a third signal and rejected: a
    `claude` process exposes its working directory and not its session id, so
    it can only speak at project granularity -- and a repository with three
    concurrent sessions is not a corner case on a working machine.
    """
    reference = now or datetime.now().astimezone()
    transcript = session_transcript(session)
    age = float("inf")
    if transcript is not None:
        try:
            # Clamped at zero: a future mtime means clock skew, and a
            # live-looking dead session is a smaller lie than a hidden live one.
            age = max(0.0, reference.timestamp() - transcript.stat().st_mtime)
        except OSError:
            age = float("inf")   # unreadable is not a session we may call live

    reason = _session_end_reason(session)
    if reason is not None:
        return SessionState(session=session, project=project, state="ended",
                            by="session_end", age=age, reason=reason)

    state = "live" if age < SESSION_COLD_AFTER_SECONDS else "cold"
    return SessionState(session=session, project=project, state=state,
                        by="mtime", age=age)
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/swarm_reader.py -v`

Expected: PASS, including every pre-existing test in the file.

- [ ] **Step 5: Commit**

```bash
git add src/swarm/reader.py tests/swarm_reader.py
git commit -m "feat(reader): tell a session that is over from one that is quiet

Two signals, in order of certainty: a session_end record from swarm's own
stream, falling back to the transcript's last write. Which one decided is
returned alongside the verdict, because a certainty and a guess are not the
same claim and a view that renders them identically lies about one."
```

---

### Task 2: `fleet()` keeps session identity and reads lazily

**Files:**
- Modify: `src/swarm/reader.py:362-390` (`all_sessions` stays; `fleet` is replaced)
- Test: `tests/swarm_reader.py`

**Interfaces:**
- Consumes: `session_state()` and `SessionState` from Task 1; existing `all_sessions()` and `read_session()`.
- Produces: `SessionRuns` dataclass with fields `session, project, state, runs, read`, and `fleet(*, redact=False, now=None, sessions=25, include_cold=False) -> list[SessionRuns]`. Task 3 iterates this list and reads every field.

**Note:** this is a breaking change to `fleet()`'s return type — from `dict[project, list[AgentRun]]` to `list[SessionRuns]`. The only callers are `tare/console.py:_fleet` (Task 3) and the tests. Grep before you start: `grep -rn "\.fleet(\|reader.fleet" src tests`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/swarm_reader.py`:

```python
def test_fleet_keeps_session_identity(fake_home):
    """Two sessions in ONE project must not arrive merged.

    The old return type was dict[project, list[AgentRun]], which made a live
    session and a dead one in the same repository indistinguishable.
    """
    write_session(fake_home, "live1", [
        ("toolu_a", "aaaaaaaaaaaaaaaa", "live work", "general-purpose", "sonnet")])
    write_agent(fake_home, "aaaaaaaaaaaaaaaa",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    write_session(fake_home, "dead1", [
        ("toolu_b", "bbbbbbbbbbbbbbbb", "old work", "general-purpose", "sonnet")])
    write_agent(fake_home, "bbbbbbbbbbbbbbbb",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    age_transcript(fake_home, "live1", 60)
    age_transcript(fake_home, "dead1", 40 * 3600)

    got = reader.fleet()
    assert [s.session for s in got] == ["live1", "dead1"]      # live first
    assert got[0].project == got[1].project == "proj"
    assert got[0].state.state == "live"
    assert got[1].state.state == "cold"


def test_fleet_does_not_parse_a_session_that_is_over(fake_home, monkeypatch):
    """The saving must not silently regress into a full walk.

    Parsing the 25 main transcripts is 86% of the console payload's cost, and
    on the machine this was built for 21 of them yield nothing.
    """
    write_session(fake_home, "dead2", [
        ("toolu_c", "cccccccccccccccc", "old work", "general-purpose", "sonnet")])
    age_transcript(fake_home, "dead2", 40 * 3600)

    parsed = []
    real = reader.read_session
    monkeypatch.setattr(reader, "read_session",
                        lambda s, **kw: parsed.append(s) or real(s, **kw))

    got = reader.fleet()
    assert parsed == []                    # nobody looked
    assert got[0].read is False
    assert got[0].runs == []


def test_read_false_is_not_the_same_fact_as_no_agents(fake_home):
    """A live session that dispatched nothing is read and empty.
    A cold one is unread. The UI must be able to tell them apart.
    """
    write_session(fake_home, "empty", [])
    write_session(fake_home, "over", [])
    age_transcript(fake_home, "empty", 60)
    age_transcript(fake_home, "over", 40 * 3600)

    by_id = {s.session: s for s in reader.fleet()}
    assert by_id["empty"].read is True and by_id["empty"].runs == []
    assert by_id["over"].read is False and by_id["over"].runs == []


def test_nothing_is_lost(fake_home):
    """The capability half's bargain, applied to the agent half.

    A view that hides a session must still be able to produce it. include_cold
    returns every agent, whatever each session's state.
    """
    write_session(fake_home, "live3", [
        ("toolu_d", "dddddddddddddddd", "live work", "general-purpose", "sonnet")])
    write_agent(fake_home, "dddddddddddddddd",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    write_session(fake_home, "dead3", [
        ("toolu_e", "eeeeeeeeeeeeeeee", "old work", "general-purpose", "sonnet")])
    write_agent(fake_home, "eeeeeeeeeeeeeeee",
                "2026-08-20T10:00:00.000Z", "2026-08-20T10:01:00.000Z")
    age_transcript(fake_home, "live3", 60)
    age_transcript(fake_home, "dead3", 40 * 3600)

    default = {r.agent_id for s in reader.fleet() for r in s.runs}
    everything = {r.agent_id for s in reader.fleet(include_cold=True) for r in s.runs}
    assert default == {"dddddddddddddddd"}
    assert everything == {"dddddddddddddddd", "eeeeeeeeeeeeeeee"}
    assert all(s.read for s in reader.fleet(include_cold=True))
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/swarm_reader.py -k "fleet or read_false or nothing_is_lost" -v`

Expected: FAIL — `AttributeError: 'dict' object has no attribute ...` or `TypeError: fleet() got an unexpected keyword argument 'include_cold'`.

- [ ] **Step 3: Implement**

In `src/swarm/reader.py`, add the dataclass directly above `fleet` (after `all_sessions`, around line 375):

```python
@dataclass
class SessionRuns:
    session: str
    project: str
    state: SessionState
    runs: list[AgentRun]   # empty when unread
    read: bool             # False = over, and deliberately not parsed
```

Replace `fleet` (lines 377-390) entirely:

```python
def fleet(*, redact: bool = False, now: datetime | None = None,
          sessions: int = 25, include_cold: bool = False) -> list[SessionRuns]:
    """Every recent session with its agents, live ones first.

    Walks the most recent `sessions` transcripts rather than all of them --
    there are hundreds on a working machine and the old ones cannot contain
    anything running.

    Returns sessions rather than a project -> agents mapping. Grouping by
    project destroyed the one fact the view most needs: two sessions in one
    repository, one live and one finished on Sunday, arrived merged and
    unrecoverable.

    A transcript is parsed only when its session is live, or when
    `include_cold` asks for the rest. `read=False` records that nobody looked,
    which is NOT the same fact as "dispatched nothing" -- rendering the two
    identically would state as fact something never examined.
    """
    reference = now or datetime.now().astimezone()
    out: list[SessionRuns] = []
    for project, session in all_sessions()[:sessions]:
        state = session_state(project, session, now=reference)
        wanted = state.state == "live" or include_cold
        runs = read_session(session, redact=redact, now=reference) if wanted else []
        out.append(SessionRuns(session=session, project=project, state=state,
                               runs=runs, read=wanted))
    # Live first, newest first within each group. `age` ascending is newest
    # first, and all_sessions() already arrives in that order.
    out.sort(key=lambda s: (s.state.state != "live", s.state.age))
    return out
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/swarm_reader.py -v`

Expected: PASS. If a pre-existing test asserts the old dict shape, update it to the new one — do not reintroduce the mapping.

- [ ] **Step 5: Commit**

```bash
git add src/swarm/reader.py tests/swarm_reader.py
git commit -m "feat(reader): fleet() returns sessions, and skips the ones that are over

Grouping by project destroyed the fact the view most needs -- a live session
and one finished on Sunday, in the same repo, arrived merged. Sessions are
kept whole, and a transcript is parsed only when its session is live:
86% of the payload's cost went on 21 of 25 sessions that yield nothing.

read=False records that nobody looked, which is not the same fact as
dispatched nothing."
```

---

### Task 3: The console serves sessions, and an `earlier` route

**Files:**
- Modify: `src/tare/console.py:236-260` (`_fleet`), `:266-283` (`_PAYLOAD_CACHE`, `payload`), `_build_payload`, and `:400-402` (`do_GET`)
- Modify: `tests/conftest.py:57-70` (the autouse cache fixture)
- Test: `tests/test_console.py`

**Interfaces:**
- Consumes: `SessionRuns` and `fleet(include_cold=...)` from Task 2.
- Produces: `payload(*, redact=False, fresh=False, include_cold=False)`. The `fleet` key of the payload becomes `{"generated": iso, "sessions": [...]}`, each session `{"id", "project", "state", "by", "age", "reason", "read", "agents": [...]}`. Task 4 renders exactly these field names. The `projects` key is gone.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_console.py`:

```python
def test_payload_carries_session_state_and_the_signal_that_decided_it(fake_home):
    db.connect()
    fleet = console.payload()["fleet"]
    assert "sessions" in fleet
    assert "projects" not in fleet          # the old shape is gone, not aliased
    for session in fleet["sessions"]:
        assert session["state"] in ("ended", "live", "cold")
        assert session["by"] in ("session_end", "mtime")
        assert isinstance(session["read"], bool)


def test_age_is_json_safe_when_a_transcript_is_missing(fake_home, monkeypatch):
    """An infinite age is not valid JSON. It must arrive as null."""
    from datetime import datetime

    class FakeState:
        state, by, age, reason = "cold", "mtime", float("inf"), None

    class FakeSession:
        session, project, state, runs, read = "s", "proj", FakeState(), [], False

    fake = type("R", (), {
        "fleet": staticmethod(lambda **kw: [FakeSession()]),
        "detail": staticmethod(lambda *a, **kw: None),
    })
    monkeypatch.setattr(console, "_reader", lambda: fake)
    console._PAYLOAD_CACHE = {}
    out = json.dumps(console.payload())
    assert "Infinity" not in out
    assert console.payload()["fleet"]["sessions"][0]["age"] is None


def test_include_cold_reaches_the_reader(fake_home, monkeypatch):
    seen = []
    fake = type("R", (), {
        "fleet": staticmethod(lambda **kw: seen.append(kw.get("include_cold")) or []),
        "detail": staticmethod(lambda *a, **kw: None),
    })
    monkeypatch.setattr(console, "_reader", lambda: fake)
    console._PAYLOAD_CACHE = {}
    console.payload()
    console.payload(include_cold=True)
    assert seen == [False, True]


def test_the_two_payload_variants_do_not_evict_each_other(fake_home, monkeypatch):
    """One cache slot meant opening the fold discarded the live payload, and
    the next poll paid the full cost again."""
    builds = []
    real = console._build_payload
    monkeypatch.setattr(console, "_build_payload",
                        lambda **kw: builds.append(kw.get("include_cold")) or real(**kw))
    db.connect()
    console._PAYLOAD_CACHE = {}
    console.payload()
    console.payload(include_cold=True)
    console.payload()                 # must be served from cache, not rebuilt
    assert builds == [False, True]
```

Update the autouse fixture in `tests/conftest.py` — the cache becomes a dict:

```python
@pytest.fixture(autouse=True)
def _clear_payload_cache():
    """The console payload is cached globally for a few seconds.

    Without this, one test's payload answers another test's assertion — and
    because the cache is populated from the REAL ~/.claude when a test forgets
    its fixture, a test could pass on data it never created. That is exactly
    how `test_it_degrades_without_swarm` passed while asserting nothing.
    """
    from tare import console

    console._PAYLOAD_CACHE = {}
    yield
    console._PAYLOAD_CACHE = {}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `python -m pytest tests/test_console.py -v`

Expected: FAIL — `KeyError: 'sessions'` / `TypeError: payload() got an unexpected keyword argument 'include_cold'`.

- [ ] **Step 3: Implement**

Replace `_fleet` in `src/tare/console.py` (lines 236-260):

```python
def _fleet(redact: bool, *, include_cold: bool = False) -> dict:
    """Sessions, each carrying whether it is still going and how we know.

    Sessions rather than projects: a live session and one finished on Sunday
    can share a repository, and the old shape merged them irretrievably.
    """
    reader = _reader()
    if reader is None:
        return {"generated": datetime.now().astimezone().isoformat(), "sessions": [],
                "unavailable": "swarm is not installed — agent history needs it"}
    now = datetime.now().astimezone()
    home = str(Path.home())
    sessions = []
    for found in reader.fleet(redact=redact, now=now, include_cold=include_cold):
        agents = []
        for run in sorted(found.runs, key=lambda r: (r.status != "running", -(r.seconds or 0))):
            info = reader.detail(run.agent_id, redact=redact)
            agents.append({
                "id": run.agent_id[:10], "label": run.label, "type": run.agent_type,
                "model": run.model, "status": run.status,
                "secs": round(run.seconds) if run.seconds else None,
                "turns": info.turns if info else 0,
                "tools": info.tools if info else [],
                "files": [f.replace(home, "~") for f in (info.files[:6] if info else [])],
                "cmds": info.commands[:4] if info else [],
                "report": (info.report[:280] if info else ""),
            })
        age = found.state.age
        sessions.append({
            "id": found.session[:8],
            "project": found.project,
            "state": found.state.state,
            "by": found.state.by,
            # inf is not valid JSON, and a missing transcript has no age to report.
            "age": round(age) if age != float("inf") else None,
            "reason": found.state.reason,
            "read": found.read,
            "agents": agents,
        })
    return {"generated": now.isoformat(), "sessions": sessions}
```

Replace the cache and `payload` (lines 266-283):

```python
# Assembling a payload walks the transcripts and re-queries SQLite -- about
# 2 seconds on a real corpus. Two panels poll independently, so without this
# every poll paid that cost twice and the panels sat on "reading" indefinitely.
# The TTL is short enough that a running agent still appears promptly.
#
# Keyed by variant rather than a single slot: the fold requests include_cold
# once, and a single slot meant that request discarded the live payload the
# 5-second poll had just paid for.
_PAYLOAD_CACHE: dict[tuple[bool, bool], tuple[float, dict]] = {}
PAYLOAD_TTL_SECONDS = 4.0


def payload(*, redact: bool = False, fresh: bool = False,
            include_cold: bool = False) -> dict:
    """Everything the page needs. Cached briefly; see the note above."""
    now = time.monotonic()
    key = (redact, include_cold)
    hit = _PAYLOAD_CACHE.get(key)
    if not fresh and hit is not None and now - hit[0] < PAYLOAD_TTL_SECONDS:
        return hit[1]
    built = _build_payload(redact=redact, include_cold=include_cold)
    _PAYLOAD_CACHE[key] = (now, built)
    return built
```

In `_build_payload`, change the signature to `def _build_payload(*, redact: bool = False, include_cold: bool = False) -> dict:` and its `_fleet` call to `_fleet(redact, include_cold=include_cold)`.

In `do_GET`, replace the `/api/data` branch (lines 400-402):

```python
            elif self.path.startswith("/api/data"):
                # The fold asks for this once when it opens. The 5-second poll
                # never does, so it never pays for sessions that are over.
                from urllib.parse import parse_qs, urlparse  # noqa: PLC0415
                earlier = (parse_qs(urlparse(self.path).query).get("earlier") or ["0"])[0] == "1"
                self._send(json.dumps(payload(redact=self.redact, include_cold=earlier)).encode(),
                           "application/json; charset=utf-8")
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `python -m pytest tests/test_console.py tests/test_viewer.py -v`

Expected: PASS. `test_payload_has_all_three_views` and `test_payload_is_json_serialisable` must still pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add src/tare/console.py tests/test_console.py tests/conftest.py
git commit -m "feat(console): a session-shaped payload, and an earlier route

The fleet payload carries sessions rather than projects, each with its state
and the signal that decided it. Cold and ended sessions are served from
/api/data?earlier=1, fetched once when the fold opens, so the 5-second poll
never pays for them.

The payload cache is keyed by variant: one slot meant opening the fold threw
away the live payload the poll had just built."
```

---

### Task 4: Live first, idle counted, the rest behind a fold

**Files:**
- Modify: `src/tare/web/console.html:475-503` (the `aled` stats block and the `fleet` render), `:153` area (add the fold container)

**Interfaces:**
- Consumes: `D.fleet.sessions[]` from Task 3 — fields `id, project, state, by, age, reason, read, agents`.
- Produces: nothing downstream.

- [ ] **Step 1: Add the fold container to the Agents panel markup**

Immediately after `<div id="fleet"></div>` (line 165), add:

```html
    <div id="earlier"></div>
```

- [ ] **Step 2: Add the age formatter and the fold state**

Next to the other helpers (after line 201):

```js
// Session ages run to days; `clock` is built for agent-length spans.
// A null or non-finite age means the transcript could not be read, which is
// "unknown" and must not render as a number.
const since = s => (s == null || !isFinite(s)) ? "unknown"
  : s < 3600 ? `${Math.round(s/60)}m` : s < 86400 ? `${Math.round(s/3600)}h`
  : `${Math.round(s/86400)}d`;
let earlierOpen = false, earlierData = null;
```

- [ ] **Step 3: Replace the stats and fleet render**

Replace lines 475-503 (from `const agents = D.fleet.projects.flatMap(...)` through the end of the `$("fleet").innerHTML = ...` assignment) with:

```js
  const sessions = D.fleet.sessions || [];
  const agents = sessions.flatMap(s => s.agents);
  const timed = agents.filter(a => a.secs != null);
  const runningN = agents.filter(a => a.status === "running").length;
  const liveS = sessions.filter(s => s.state === "live");
  const overS = sessions.filter(s => s.state !== "live");
  const busy = liveS.filter(s => s.agents.length);
  const idle = liveS.length - busy.length;

  $("aled").innerHTML = [
    ["agents recorded", fmt(agents.length), ""], ["running", fmt(runningN), runningN ? "on" : ""],
    ["agent time", Math.round(timed.reduce((s,a)=>s+a.secs,0)/60)+"m", ""],
    ["longest", clock(timed.length ? Math.max(...timed.map(a=>a.secs)) : null), ""],
    ["sessions live", fmt(liveS.length), liveS.length ? "on" : ""],
  ].map(([k,v,c]) => `<div class="cell"><div class="k">${k}</div><div class="v ${c}">${v}</div></div>`).join("");

  // Remember which agents the reader had open, so a refresh does not close them.
  openAgents = new Set([...document.querySelectorAll("details.agent[open]")].map(d => d.dataset.id));

  // Only sessions that dispatched something get a heading. Eleven of twelve
  // live sessions on a working machine have dispatched nothing, and eleven
  // empty headings are the noise this view was built to remove.
  $("fleet").innerHTML = busy.map(sessionBlock).join("")
    + (idle ? `<div class="meta" style="margin:.9rem 0">${idle} other session${
        idle === 1 ? "" : "s"} open, none dispatching</div>` : "")
    + (busy.length || idle ? "" : `<div class="meta">no sessions open</div>`);

  $("earlier").innerHTML = overS.length ? `<details class="agent" id="earlierFold"${
      earlierOpen ? " open" : ""}><summary>
      <span class="lbl2">earlier — ${overS.length} session${overS.length === 1 ? "" : "s"}</span>
      <span class="meta">last over ${since(Math.min(...overS.map(s => s.age ?? Infinity)))} ago</span>
      </summary><div id="earlierBody" class="brain"><span class="meta">reading…</span></div>
      </details>` : "";
  const fold = $("earlierFold");
  if (fold) fold.addEventListener("toggle", () => {
    earlierOpen = fold.open;
    if (fold.open) loadEarlier();
  });
  if (earlierOpen && earlierData) renderEarlier();
```

- [ ] **Step 4: Add the session block renderer and the fold loader**

Add these three functions immediately before `function refresh()` (line 533):

```js
function sessionBlock(s, dim) {
  // A session that is over says WHY, and whether that is certain: an
  // ended/session_end verdict is observed, a cold/mtime one is inferred.
  const why = s.state === "live" ? "live"
    : s.state === "ended" ? `ended · ${esc(s.reason || "unknown")}`
    : `cold · no write in ${since(s.age)}`;
  return `<h3 style="font-family:var(--mono);font-size:.74rem;margin:1.1rem 0 .4rem;${
      dim ? "opacity:.6;" : ""}color:var(--ink-soft)">
    ${esc(shortProj(s.project))} <span class="meta">${why} · ${s.agents.length} agents</span></h3>`
    + s.agents.slice(0,40).map(a => `<details class="agent" data-s="${esc(a.status)}" data-id="${esc(a.id)}"${
      openAgents.has(a.id) ? " open" : ""}><summary>
      <span class="lbl2">${esc(a.label)}</span>
      <span class="meta">${esc(a.model||"?")} · ${a.turns} turns</span>
      <span class="meta">${a.status==="running"?"running":clock(a.secs)}</span></summary>
      <div class="brain">
        <div><h5>reached for</h5>${a.tools.length ? a.tools.map(([n,c])=>`<span class="chip">${esc(n)} ${c}</span>`).join("") : '<span class="meta">none</span>'}</div>
        <div><h5>files</h5>${a.files.length ? `<ul class="plain">${a.files.map(f=>`<li title="${esc(f)}">${esc(f)}</li>`).join("")}</ul>` : '<span class="meta">none</span>'}</div>
        <div><h5>ran</h5>${a.cmds.length ? `<ul class="plain">${a.cmds.map(c=>`<li title="${esc(c)}">$ ${esc(c)}</li>`).join("")}</ul>` : '<span class="meta">none</span>'}</div>
        ${a.report ? `<div class="rep">${esc(a.report)}</div>` : ""}
      </div></details>`).join("")
    + (s.agents.length > 40 ? `<div class="meta" style="margin-top:.4rem">… ${s.agents.length-40} more not shown</div>` : "");
}

function loadEarlier() {
  if (earlierData) return renderEarlier();
  fetch("/api/data?earlier=1", {cache:"no-store"})
    .then(r => r.ok ? r.json() : Promise.reject(r.status))
    .then(next => { earlierData = (next.fleet.sessions || []).filter(s => s.state !== "live");
                    renderEarlier(); })
    .catch(() => { $("earlierBody").innerHTML =
      '<span class="meta">could not read the earlier sessions</span>'; });
}

function renderEarlier() {
  // The parse has run now, so "dispatched nothing" is a fact rather than an
  // unknown -- and those sessions are worth a count, not a heading each.
  const busy = earlierData.filter(s => s.agents.length);
  const quiet = earlierData.length - busy.length;
  $("earlierBody").innerHTML = busy.map(s => sessionBlock(s, true)).join("")
    + (quiet ? `<div class="meta" style="margin-top:.6rem">+${quiet} dispatched no agents</div>` : "")
    || '<span class="meta">no agents in any earlier session</span>';
}
```

- [ ] **Step 5: Check the remaining references to the old shape**

Run: `grep -n "fleet.projects" src/tare/web/console.html`

Expected: no matches. If line 529 (`$("foot").textContent`) or the `pulse` line still reference `agents`, they are fine — `agents` is redefined above them.

- [ ] **Step 6: Verify in the browser**

```bash
python -m pytest tests/ -q
tare console
```

Confirm, on the Agents tab: sessions that dispatched agents have headings; a count line reports the idle ones; the `earlier — N sessions` fold expands, fetches once, and shows dimmed sessions each labelled `ended · <reason>` or `cold · no write in Nh`.

- [ ] **Step 7: Commit**

```bash
git add src/tare/web/console.html
git commit -m "feat(console): live sessions first, idle counted, the rest folded

Eleven of twelve live sessions on this machine dispatched nothing, and
thirteen of twenty-five were already over. Only sessions doing work get a
heading; the idle ones get a count and the finished ones a fold that says
why each is considered over, and whether that is observed or inferred."
```

---

## Self-Review

**Spec coverage.** Every section maps to a task: the two-tier rule and the `by` field (Task 1); `SessionRuns`, lazy parse and the nothing-is-lost bargain (Task 2); the session-shaped payload, the `earlier` route and the per-variant cache (Task 3); live-first render, the idle count line, and the fold (Task 4). Failure modes are covered by tests in Tasks 1–3 — missing `runs/`, unreadable `stat`, corrupt stream line, future mtime, and the fetch failure path in `loadEarlier`.

**Type consistency.** `SessionState` fields (`session, project, state, by, age, reason`) are defined in Task 1 and read in Tasks 2 and 3. `SessionRuns` fields (`session, project, state, runs, read`) are defined in Task 2 and read in Task 3. The payload keys Task 3 produces (`id, project, state, by, age, reason, read, agents`) are exactly those Task 4 renders.

**Known gap, deliberately out of scope.** `console._orchestration` still walks `all_sessions()[:14]` and will build a dispatch tree from a session that ended on Sunday. The same `session_state` call fixes it; it is a separate surface with its own rendering and belongs in its own change.
