"""Turn fix commits into findings.

## Why commits, and why only some of them

Measured across 132 repositories and six months: `homelab/CLAUDE.md` holds 117
claim-shaped lines and every other instruction file on the machine holds 45
between them. Those others are documentation -- how to run the thing, what the
architecture is -- which belongs in the repository, not in a knowledge base.

The knowledge is in the commits, but not in all of them. A feature commit says
what was BUILT; a `fix:` commit with a body says what was WRONG and why. That
distinction is the filter, and it matters: 762 commits contain claim-shaped
language, and 138 survive "is a fix AND explains its own cause". The first
number is mostly design prose -- "installation_id, never derived from hostname"
reads like a lesson and is a specification.

## Dedup is a candidate list plus a judgement, not a search

BM25 is lexical, and a near-duplicate finding is a semantic thing. Asked
"cached fallback succeeds silently hiding an outage", `recall` returns
`macos-caches-negative-dns-answers` -- matched on the token "cach".

That is a false positive, and false positives are FINE here: recall is used to
narrow 22 findings to a handful of candidates, and the model then decides
whether the new claim is already covered. What would break this is a false
negative, and OR'd token matching over a small corpus has high recall and poor
precision -- exactly the right way round.

So no embedding model, no service, no new dependency.

## This writes transcripts into the corpus `mine` reads

Same unavoidable cost as `tag`, handled the same way: every prompt opens with
`HARVEST_PROMPT_SIGNATURE`, which `mine` excludes structurally. The constant is
imported from `mine` rather than restated, so the two cannot drift apart.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import brain
from .mine import HARVEST_PROMPT_SIGNATURE

HARVEST_MODEL_TIMEOUT = 120

# A commit worth reading. `fix:` alone is not enough -- a one-line fix with no
# body explains nothing -- and a body alone is not enough either, since feature
# commits have the longest bodies on the machine.
FIX_SUBJECT = re.compile(r"^(fix|bugfix|hotfix)\b", re.I)
CAUSAL = re.compile(
    r"(because|the cause|turned out|was actually|silently|root cause|"
    r"meant that|which is why|the tell|red herring)", re.I)
MIN_BODY_CHARS = 300

# How many existing findings the model is shown when judging duplication.
DEDUP_CANDIDATES = 8

PROMPT = HARVEST_PROMPT_SIGNATURE + """

Below is a fix commit from a real repository, and the findings already in the
knowledge base that might already cover it.

Return a finding ONLY if the commit teaches something that would still be worth
knowing on a different day, in a different project. Return NONE otherwise.

Answer with exactly `NONE` when:
- the commit is routine (a typo, a version bump, a rename, a lint fix)
- what it teaches is already covered by one of the existing findings below,
  even if worded differently
- the lesson is specific to code that no longer exists

Otherwise answer with EXACTLY this shape and nothing else:

NAME: a-short-kebab-case-slug-stating-the-claim
SCOPE: universal | tool | project
TOOLS: comma,separated,or,empty
TAGS: comma,separated
CLAIM: one sentence stating what is true, in the present tense
WHY: two or three sentences on what goes wrong without it, using the concrete
detail from the commit
APPLY: one or two sentences on what to do instead

Rules for the finding:
- SCOPE is `universal` if it would hold in an unrelated codebase, `tool` if it
  is about a named tool's behaviour, `project` if it depends on this specific
  system.
- State a specific behaviour. A finding that could be printed on a poster
  ("test your code", "be careful with caches") is worthless -- return NONE
  rather than write one.
- Keep the concrete numbers and error strings from the commit. They are what
  make a finding recognisable when you hit the problem again.

--- EXISTING FINDINGS THAT MAY ALREADY COVER THIS ---
{existing}

--- COMMIT ({repo} {sha}) ---
{subject}

{body}
"""


@dataclass
class Candidate:
    repo: str
    sha: str
    subject: str
    body: str


@dataclass
class Harvested:
    candidate: Candidate
    name: str = ""
    scope: str = "project"
    tools: list[str] | None = None
    tags: list[str] | None = None
    claim: str = ""
    why: str = ""
    apply: str = ""
    skipped: str = ""          # why nothing was written, when nothing was


def candidates(repo: Path, *, since: str = "6 months ago", limit: int = 50) -> list[Candidate]:
    """Fix commits from one repository that explain their own cause."""
    try:
        out = subprocess.run(
            ["git", "-C", str(repo), "log", f"--since={since}",
             "--format=%h%x00%s%x00%b%x01"],
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []

    found = []
    for record in out.stdout.split("\x01"):
        parts = record.strip().split("\x00")
        if len(parts) < 3:
            continue
        sha, subject, body = parts
        if not FIX_SUBJECT.match(subject):
            continue
        if len(body) < MIN_BODY_CHARS or not CAUSAL.search(body):
            continue
        found.append(Candidate(repo=repo.name, sha=sha, subject=subject, body=body))
    # Longest first: a fuller explanation makes a better finding, and the cap
    # is there so one enormous repository cannot consume a whole run.
    found.sort(key=lambda c: -len(c.body))
    return found[:limit]


def _already_harvested(candidate: Candidate) -> bool:
    """Has any finding already recorded this exact commit as its source?"""
    marker = f"{candidate.repo}@{candidate.sha}"
    for finding in brain.load():
        try:
            text = Path(finding.path).read_text(encoding="utf-8")
        except OSError:
            continue
        if marker in text:
            return True
    return False


def _existing_for(conn, candidate: Candidate) -> str:
    """The findings most likely to already cover this commit.

    Queried with the commit's own words. Precision does not matter -- the model
    is being asked to rule things out, and being shown an irrelevant finding
    costs a line of prompt.
    """
    query = f"{candidate.subject} {candidate.body[:400]}"
    results = brain.recall(conn, query, limit=DEDUP_CANDIDATES)
    if not results:
        return "(none)"
    return "\n".join(f"- {r.name}: {r.summary[:160]}" for r in results)


def _ask(prompt: str, timeout: int = HARVEST_MODEL_TIMEOUT) -> str:
    try:
        out = subprocess.run(
            ["claude", "-p", prompt, "--allowedTools", "", "--max-turns", "1"],
            capture_output=True, text=True, timeout=timeout,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def parse_response(text: str, candidate: Candidate) -> Harvested:
    """Turn the model's reply into a finding, or record why there is none."""
    stripped = (text or "").strip()
    if not stripped:
        return Harvested(candidate, skipped="no answer")
    if stripped.upper().startswith("NONE"):
        return Harvested(candidate, skipped="nothing durable to learn")

    def field(key: str) -> str:
        # `[ \t]*` and not `\s*`: the looser version consumed the newline after
        # an EMPTY field, which put the lookahead past the boundary it was
        # meant to stop at, so `TOOLS:` with nothing after it swallowed the
        # whole `TAGS:` line and wrote it into the frontmatter.
        found = re.search(rf"^{key}:[ \t]*(.*?)(?=\n[A-Z]+:|\Z)", stripped, re.M | re.S)
        return " ".join(found.group(1).split()) if found else ""

    name = field("NAME").strip().lower()
    name = re.sub(r"[^a-z0-9-]+", "-", name).strip("-")
    claim = field("CLAIM")
    if not name or not claim:
        # A malformed answer is dropped rather than guessed at: a finding with
        # an invented name is worse than a commit nobody harvested.
        return Harvested(candidate, skipped="unparseable answer")

    scope = field("SCOPE").strip().lower()
    return Harvested(
        candidate=candidate,
        name=name,
        scope=scope if scope in brain.SCOPES else "project",
        tools=[t.strip() for t in field("TOOLS").split(",") if t.strip()],
        tags=[t.strip() for t in field("TAGS").split(",") if t.strip()],
        claim=claim, why=field("WHY"), apply=field("APPLY"),
    )


def render_finding(h: Harvested) -> str:
    body = [h.claim, ""]
    if h.why:
        body += [f"**Why it matters:** {h.why}", ""]
    if h.apply:
        body += [f"**How to apply:** {h.apply}", ""]
    front = "\n".join([
        f"name: {h.name}",
        "type: finding",
        f"scope: {h.scope}",
        f"tools: [{', '.join(h.tools or [])}]",
        f"projects: [{h.candidate.repo}]",
        "confidence: verified",
        f"source: {h.candidate.repo}@{h.candidate.sha} {h.candidate.subject[:70]}",
        "superseded_by: null",
        f"tags: [{', '.join(h.tags or [])}]",
    ])
    return f"---\n{front}\n---\n\n" + "\n".join(body).strip() + "\n"


def harvest(conn, repo: Path, *, since: str = "6 months ago",
            limit: int = 10, apply: bool = False,
            on_event=lambda line: None) -> list[Harvested]:
    """Read fix commits, propose findings, and optionally write them."""
    results = []
    for candidate in candidates(repo, since=since, limit=limit):
        prompt = PROMPT.format(
            existing=_existing_for(conn, candidate),
            repo=candidate.repo, sha=candidate.sha,
            subject=candidate.subject, body=candidate.body[:4000],
        )
        parsed = parse_response(_ask(prompt), candidate)

        if not parsed.skipped and _already_harvested(candidate):
            # A deterministic backstop for the case the model is worst at.
            #
            # Shown the overlapping finding at rank 1, it still proposed a new
            # one from the same commit -- judging "the validator is null" and
            # "nothing invokes the validator" to be different mechanisms. That
            # is arguable, but two findings from one commit is a call for a
            # person to make, not a harvester running unattended.
            parsed.skipped = f"{candidate.sha} already produced a finding"

        if not parsed.skipped and (brain.brain_dir() / f"{parsed.name}.md").exists():
            # The model proposed a name that already exists. Not overwritten:
            # a harvest that silently rewrites a finding you refined by hand is
            # a data-loss bug wearing a feature's clothes.
            parsed.skipped = f"a finding named {parsed.name} already exists"

        if parsed.skipped:
            on_event(f"  -- {candidate.sha} {candidate.subject[:56]}  ({parsed.skipped})")
        else:
            on_event(f"  ++ {candidate.sha} {parsed.name}")
            if apply:
                brain.brain_dir().mkdir(parents=True, exist_ok=True)
                (brain.brain_dir() / f"{parsed.name}.md").write_text(
                    render_finding(parsed), encoding="utf-8")
        results.append(parsed)

    if apply and any(not r.skipped for r in results):
        brain.reindex(conn)
    return results
