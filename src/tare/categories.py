"""Roll capabilities up into a handful of domains.

The graph carries 641 distinct tags across 180 capabilities — accurate, and
useless as a grouping: a list of 641 headings is not a taxonomy. This maps them
onto a small set of domains you would actually filter by.

## Why keywords rather than clustering

The rules below are plain and inspectable, which matters more here than being
clever. A capability landing in the wrong domain should be a line someone can
read and correct, not an embedding nobody can argue with. `explain()` returns
the term that decided it, so a surprising answer can always be traced.

Order matters: the first domain whose terms match wins, so the more specific
domains are checked before the general ones. `video` before `design`, because a
motion-graphics skill is video work that happens to be visual; `code` last of
the specific ones, because "testing" and "performance" appear in almost
everything.
"""

from __future__ import annotations

import sqlite3

# (domain, terms). Checked in order; first match wins.
DOMAINS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("video", (
        "video", "motion", "hyperframes", "footage", "caption", "subtitle",
        "render", "remotion", "animation", "animate", "cinematic", "shot",
        "storyboard", "higgsfield", "voiceover", "talking-head", "slideshow",
    )),
    ("marketing", (
        "marketing", "seo", "growth", "brand", "copywriting", "ad", "ads",
        "campaign", "email", "outreach", "launch", "pricing", "conversion",
        "cro", "social", "content-strategy", "prospect", "churn", "referral",
        "positioning", "audience", "naming", "ab", "attribution", "paywall",
        "signup", "competitor", "customer-research", "prospecting", "offer",
    )),
    ("design", (
        "ui", "ux", "design", "visual", "typography", "layout", "color",
        "colour", "accessibility", "interface", "frontend", "polish",
        "delight", "aesthetic", "spacing", "component", "mermaid", "diagram",
    )),
    ("infra", (
        "deploy", "terraform", "kubernetes", "docker", "cloud", "aws", "gcp",
        "azure", "infrastructure", "iac", "ci", "pipeline", "devops",
        "monitoring", "incident", "network", "database", "sql", "migration",
    )),
    ("data", (
        "data", "analytics", "ml", "machine-learning", "model", "etl",
        "warehouse", "embedding", "vector", "rag", "llm", "prompt", "ai",
    )),
    ("writing", (
        "documentation", "docs", "tutorial", "reference", "api-docs",
        "writing", "editing", "explainer", "guide", "readme",
    )),
    ("process", (
        "workflow", "planning", "review", "onboarding", "setup", "git",
        "worktree", "debugging", "systematic", "orchestration", "agent",
        "checklist", "process",
    )),
    ("code", (
        "code", "refactor", "testing", "performance", "concurrency",
        # Languages and runtimes, spelled as the capability names spell them:
        # `golang-pro` splits to "golang", which never matches "go".
        "rust", "python", "typescript", "javascript", "go", "golang", "java",
        "swift", "kotlin", "cpp", "c", "csharp", "php", "scala", "ruby",
        "elixir", "erlang", "perl", "lua", "dart", "flutter",
        "systems-programming", "memory-safety", "api-design", "backend",
        "architecture", "security", "payments", "stripe", "react-native",
        "mobile", "ios", "android", "unity", "graphql",
        # Last: the `-pro` suffix marks a language expert across the whole
        # agent set, and bounded matching keeps it off "prospecting".
        "pro",
    )),
)

FALLBACK = "other"


def _normalize(text: str) -> str:
    """Lowercase, and hyphens/underscores/commas to spaces.

    Applied to BOTH the text and the term. Normalizing only one side means a
    two-word term can never match: `content-strategy` reaches the name pass as
    "content strategy", and `react-native-best-practices` as "react native
    best practices" — neither containing the hyphen the term is written with.
    Both landed in `other`.
    """
    out = (text or "").lower()
    for ch in "-_,/":
        out = out.replace(ch, " ")
    return f" {' '.join(out.split())} "


def _match(text: str) -> tuple[str, str] | None:
    haystack = _normalize(text)
    for domain, terms in DOMAINS:
        for term in terms:
            needle = _normalize(term).strip()
            # Bounded on both sides so "ad" does not match "readme" and "ai"
            # does not match "explain" -- substring matching over a 641-term
            # vocabulary produces exactly that kind of nonsense. The plural is
            # a separate candidate rather than a suffix strip because bounding
            # is what rejects it: `referrals`, `emails` and `popups` all fell
            # through to `other` against singular terms.
            if f" {needle} " in haystack or f" {needle}s " in haystack:
                return domain, term
    return None


def explain(name: str, tags: str = "", purpose: str = "") -> tuple[str, str | None]:
    """(domain, the term that decided it). The term is None for the fallback.

    Three passes, weakest signal last: the name, then the tags, then the
    purpose line. Each pass is a fallback for the one above finding nothing.

    The ordering is what it is because both weaker sources mislead in
    practice. Prose triggers domain terms incidentally — `rust-pro` describes
    "writing and reviewing idiomatic Rust" and landed in `writing`,
    `docs-architect` in `design` on "design patterns". Tags are curated but
    describe what a capability *touches* rather than what it is, which
    scattered the language agents across four domains: `golang-pro` tagged
    "design" went to design, `elixir-pro` tagged "agent" to process, `php-pro`
    tagged "data" to data. Their names say exactly what they are.
    """
    named = _match((name or "").lower().replace("-", " ").replace("_", " "))
    if named:
        return named
    tagged = _match((tags or "").lower())
    if tagged:
        return tagged
    loose = _match((purpose or "").lower())
    if loose:
        return loose
    return FALLBACK, None


def of(name: str, tags: str = "", purpose: str = "") -> str:
    return explain(name, tags, purpose)[0]


def counts(conn: sqlite3.Connection) -> dict[str, int]:
    """How many live capabilities fall in each domain."""
    tally: dict[str, int] = {}
    for row in conn.execute("SELECT name, tags, purpose_line FROM nodes"):
        domain = of(row["name"], row["tags"] or "", row["purpose_line"] or "")
        tally[domain] = tally.get(domain, 0) + 1
    return dict(sorted(tally.items(), key=lambda kv: -kv[1]))
