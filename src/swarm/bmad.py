"""Reading BMAD's plan, and nothing else.

BMAD owns the plan; tare owns the loop, the boundary and the record. This
module is the read-only half of that seam: it locates a BMAD install, finds
the spec folders, and turns `stories.yaml` into `Story` objects. It never
writes into `_bmad/` or `_bmad-output/`, and it holds no execution state --
`stories-schema.md` validity rule 3 is "No `status` field, ever", so where a
story has got to is tare's ledger's business, not this file's.

## Why validation refuses the whole file

A plan is read in list order and executed top to bottom. Half a plan is not a
smaller plan, it is a different one -- so a file that breaks any of the four
validity rules is refused entire, naming the rule, rather than yielding the
entries that happened to parse.

Rule 4 is the one that will actually bite. YAML coerces an unquoted `id: 1`
to an integer, string comparison against the ledger silently stops matching,
and the story re-dispatches every night looking untouched.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

# Rule 4: "Ids are YAML strings, always quoted, containing only letters,
# digits, and dashes." Characters like `/` or `*` break the filename match
# against `stories/<id>-*.md`.
_ID_OK = re.compile(r"\A[A-Za-z0-9-]+\Z")


class BmadFormatError(Exception):
    """A `stories.yaml` that cannot be trusted as a plan.

    Carries the schema rule number so `doctor` can report drift as drift
    rather than as an empty queue.
    """

    def __init__(self, rule: int, detail: str):
        super().__init__(f"stories.yaml violates validity rule {rule}: {detail}")
        self.rule = rule
        self.detail = detail


@dataclass(frozen=True)
class Story:
    id: str
    title: str
    description: str
    spec_dir: Path
    spec_checkpoint: bool = False
    done_checkpoint: bool = False
    invoke_dev_with: str = ""

    @property
    def slug(self) -> str:
        """The spec folder's name -- the namespace an id is unique within."""
        return self.spec_dir.name

    @property
    def key(self) -> str:
        """Identity across spec folders, and the ledger's join key."""
        return f"{self.slug}/{self.id}"


def parse_stories(text: str, *, spec_dir: Path) -> list[Story]:
    """A top-level YAML list, one entry per story, in execution order.

    Validated against all four rules from `stories-schema.md`. Raises
    `BmadFormatError` rather than returning a partial list.
    """
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BmadFormatError(1, f"file does not parse as YAML: {exc}") from exc

    if raw is None:
        return []
    if not isinstance(raw, list):
        raise BmadFormatError(1, f"top level is {type(raw).__name__}, expected a list")

    stories: list[Story] = []
    seen: list[str] = []
    for index, entry in enumerate(raw):
        if not isinstance(entry, dict):
            raise BmadFormatError(1, f"entry {index} is {type(entry).__name__}, expected a mapping")

        # Rule 3, checked before anything else: a status field means this file
        # is trying to be a record as well as a plan, and we would then have
        # two sources of truth for where a story has got to.
        if "status" in entry:
            raise BmadFormatError(3, f"entry {index} carries a `status` field, which is never valid")

        raw_id = entry.get("id")
        if raw_id is None:
            raise BmadFormatError(1, f"entry {index} has no `id`")
        # Rule 4: an unquoted `id: 1` arrives here as an int.
        if not isinstance(raw_id, str):
            raise BmadFormatError(
                4, f"entry {index} has an unquoted id ({raw_id!r} parsed as "
                   f"{type(raw_id).__name__}); ids must be quoted YAML strings")
        if not _ID_OK.match(raw_id):
            raise BmadFormatError(4, f"id {raw_id!r} has characters outside letters, digits and dashes")

        if raw_id in seen:
            raise BmadFormatError(1, f"id {raw_id!r} appears more than once")

        # Rule 2: prefix-free under the `<id>-` filename convention. "3" and
        # "3-2" cannot coexist, because `stories/3-*.md` would match both.
        for other in seen:
            if raw_id.startswith(other + "-") or other.startswith(raw_id + "-"):
                raise BmadFormatError(2, f"ids {other!r} and {raw_id!r} are not prefix-free")
        seen.append(raw_id)

        title = entry.get("title")
        description = entry.get("description")
        if not isinstance(title, str) or not title.strip():
            raise BmadFormatError(1, f"story {raw_id!r} has no usable `title`")
        if not isinstance(description, str) or not description.strip():
            raise BmadFormatError(1, f"story {raw_id!r} has no usable `description`")

        stories.append(Story(
            id=raw_id,
            title=title.strip(),
            description=description.strip(),
            spec_dir=spec_dir,
            spec_checkpoint=bool(entry.get("spec_checkpoint", False)),
            done_checkpoint=bool(entry.get("done_checkpoint", False)),
            invoke_dev_with=str(entry.get("invoke_dev_with", "") or "").strip(),
        ))
    return stories


# ---------------------------------------------------------------------------
# Finding the install
# ---------------------------------------------------------------------------
#
# Paths come from `_bmad/bmm/config.yaml`, never from a constant. BMAD's own
# skills resolve `implementation_artifacts` and friends out of that file, so a
# hardcoded `_bmad-output/` would work on a default install and quietly find
# nothing on any other -- which is the failure this module is most likely to
# produce, and the least likely to be noticed, because "no stories" and "wrong
# directory" look identical from the outside.

DEFAULT_OUTPUT_FOLDER = "_bmad-output"


def config_path(repo: Path) -> Path:
    return repo / "_bmad" / "bmm" / "config.yaml"


def is_installed(repo: Path) -> bool:
    return config_path(repo).is_file()


def read_config(repo: Path) -> dict:
    path = config_path(repo)
    if not path.is_file():
        return {}
    try:
        loaded = yaml.safe_load(path.read_text(encoding="utf-8", errors="replace"))
    except yaml.YAMLError as exc:
        raise BmadFormatError(1, f"{path} does not parse as YAML: {exc}") from exc
    return loaded if isinstance(loaded, dict) else {}


def output_root(repo: Path) -> Path:
    """Where BMAD writes. `output_folder` if the config sets it, else default."""
    folder = str(read_config(repo).get("output_folder") or DEFAULT_OUTPUT_FOLDER)
    candidate = Path(folder)
    return candidate if candidate.is_absolute() else repo / candidate


def spec_folders(repo: Path) -> list[Path]:
    """Every spec folder holding a `stories.yaml`, sorted by name.

    A folder with a `SPEC.md` and no `stories.yaml` has been planned but not
    broken down, which is not the same as having no work -- it is work this
    loop cannot dispatch, and it is skipped silently on purpose. `doctor`
    reports it; the queue does not guess.
    """
    root = output_root(repo) / "specs"
    if not root.is_dir():
        return []
    return sorted((d for d in root.iterdir() if d.is_dir() and (d / "stories.yaml").is_file()),
                  key=lambda p: p.name)


def stories_for(repo: Path) -> list[Story]:
    """Every story across every spec folder, in folder order then list order."""
    out: list[Story] = []
    for folder in spec_folders(repo):
        text = (folder / "stories.yaml").read_text(encoding="utf-8", errors="replace")
        out.extend(parse_stories(text, spec_dir=folder))
    return out
