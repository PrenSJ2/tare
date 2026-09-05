"""Did the work satisfy the story, or did the process merely end?

`nightshift` used to answer this with an exit code and a commit count. Both
are true of a run that read the story, wrote nothing useful and stopped, which
is why a story could be "done" every night forever.

So a story leaves the queue only when a SEPARATE pass says its acceptance
criteria hold against the diff. Separate matters twice over: the run that did
the work is the worst available judge of whether it did the work, and this
pass gets read-only tools, so it can render an opinion but cannot make the
opinion true.

## Every default is a no

Ambiguity, unparseable output, a timeout, a crash -- all of them are "not
verified". The asymmetry is deliberate and cheap: an unverified story is still
open and runs again tomorrow, costing one night. A story wrongly marked
complete leaves the queue permanently, costing the thing the queue existed
for.

## What this verdict is not

This verdict is one more model's opinion, not a proof. A verifier can be wrong
in both directions: false "no" (unmet actually holds) costs one night; false
"yes" (unmet actually fails) costs the queue forever. The design answers this
with the asymmetry, not with certainty.

## Residual risk

The prompt asks for the JSON and nothing else. A verifier that echoes the
schema after concluding would result in last-match-wins selecting a stale
schema over the real verdict. This is considered rather than missed.

A second, larger one: `acceptance_criteria` below reads `stories/<id>-*.md`
whole into `{criteria}`, and that file never goes through `nightshift.screen`.
`screen` covers `title` + `description` + `invoke_dev_with` -- the fields
`run_story_shift` concatenates itself -- and stops there; it has never seen
the spec markdown this function reads separately, straight off disk. So the
artifact being judged supplies unscreened text to the judge, and this
verdict is what removes a story from the queue PERMANENTLY. The same file
also reaches `bmad-build-auto` under the wide policy at dispatch time,
equally unscreened.

Not fixed here, on purpose: screening free-form spec prose without false-
refusing ordinary acceptance criteria ("delete the stale rows", "drop the
unused index") is a bigger design change than a verifier module should carry
as a side effect, and this repository has already spent several rounds
learning how expensive a wrong keyword gate is (see `nightshift.py`'s
`_PRODUCTION_VERBS` comment). Naming the gap here is the honest move
available this round; closing it is not.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from .bmad import Story

# Read-only by construction. No Write, no Edit, no Bash: a verifier that can
# change the tree can satisfy the criteria it is judging.
VERIFY_TOOLS = ("Read", "Glob", "Grep")

VERDICT_RE = re.compile(r"\{[^{}]*\"verified\"\s*:\s*(?:true|false)[^{}]*\}", re.S)

PROMPT = """You are checking whether one story's acceptance criteria hold. \
You cannot change anything -- you have read-only tools by design.

Be strict. If a criterion is not demonstrably satisfied by the diff and the \
files it touched, it is unmet. "Looks reasonable" is not satisfied. Absence of \
evidence is unmet, not met.

Story: {title}
Story id: {story_id}
Spec folder: {spec_dir}

{criteria}

The diff produced for this story:
```
{diff}
```

Reply with this JSON object and nothing else:
{{"verified": true|false, "reason": "one sentence", "unmet": ["criterion", ...]}}
"""


@dataclass
class Verdict:
    verified: bool
    reason: str = ""
    unmet: list[str] = field(default_factory=list)
    raw: str = ""


def acceptance_criteria(story: Story) -> str:
    """The story's spec file if BMAD wrote one, else the story description.

    `stories.yaml` holds a two-sentence description pointing into `SPEC.md`;
    the detail lives in `stories/<id>-*.md`. The filename match is the
    `<id>-` convention the schema's prefix-free rule exists to protect, which
    is why that rule is enforced at parse time.
    """
    folder = story.spec_dir / "stories"
    if folder.is_dir():
        for path in sorted(folder.glob(f"{story.id}-*.md")):
            return path.read_text(encoding="utf-8", errors="replace")
    return f"Acceptance criteria (from the plan):\n{story.description}"


def build_verify_command(story: Story, *, worktree_path: Path, diff: str) -> list[str]:
    prompt = PROMPT.format(
        title=story.title, story_id=story.id, spec_dir=story.spec_dir,
        criteria=acceptance_criteria(story),
        # A diff can be enormous and the verdict is about the criteria, not
        # about every line. Truncation is stated in the prompt rather than
        # hidden, so a verifier that needs more says so instead of guessing.
        diff=(diff[:60000] + "\n[... diff truncated ...]") if len(diff) > 60000 else diff,
    )
    return [
        "claude", "-p", prompt,
        "--allowedTools", ",".join(VERIFY_TOOLS),
    ]


def parse_verdict(text: str) -> Verdict:
    tail = (text or "")[-800:]
    for match in reversed(list(VERDICT_RE.finditer(text or ""))):
        try:
            parsed = json.loads(match.group(0))
        except json.JSONDecodeError:
            continue
        unmet = parsed.get("unmet")
        return Verdict(
            verified=bool(parsed.get("verified")),
            reason=str(parsed.get("reason") or ""),
            unmet=[str(u) for u in unmet] if isinstance(unmet, list) else [],
            raw=tail,
        )
    return Verdict(verified=False, reason="no verdict in the verifier's output", raw=tail)


def check(story: Story, *, worktree_path: Path, diff: str,
          timeout_minutes: int = 15, runner=None) -> Verdict:
    """Run the verification pass. Any failure to get an answer is a no."""
    argv = build_verify_command(story, worktree_path=worktree_path, diff=diff)

    def _default_runner(argv_):
        from . import nightshift  # local: nightshift imports verify in Task 7
        return subprocess.run(
            argv_, cwd=str(worktree_path), capture_output=True, text=True,
            timeout=timeout_minutes * 60, env=nightshift.child_env())

    run = runner or _default_runner
    try:
        result = run(argv)
    except subprocess.TimeoutExpired:
        return Verdict(verified=False, reason=f"verification timed out after {timeout_minutes}m")
    except OSError as exc:
        return Verdict(verified=False, reason=f"verification could not run: {exc}")
    # Non-zero exit is a third failure signal beside timeout and OSError. It is
    # the only one that still produces output, which is why it was easy to miss:
    # a rate limit or API error after the model emits JSON, a post-response
    # bookkeeping failure, still leaves parseable stdout. But it is still a
    # failure and must not be treated as a pass.
    if result.returncode != 0:
        return Verdict(verified=False, reason=f"verification exited with code {result.returncode}")
    return parse_verdict(result.stdout or "")
