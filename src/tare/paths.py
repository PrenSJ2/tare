"""Where everything lives under ~/.claude, and how big it is.

Every path is derived from `claude_home()` so the whole tool can be pointed at
a fixture directory by setting TARE_HOME. Tests rely on that; nothing here
should ever hard-code an absolute path.
"""

from __future__ import annotations

import os
from pathlib import Path


def claude_home() -> Path:
    """The Claude Code config directory this run operates on."""
    override = os.environ.get("TARE_HOME")
    if override:
        return Path(override)
    return Path.home() / ".claude"


def db_path() -> Path:
    # Renamed from harness.db when the tool became `tare`. The old file is
    # still read if it is the only one present, so an existing install keeps
    # its usage history and learned memory instead of silently starting over
    # with an empty graph.
    new = claude_home() / "tare.db"
    old = claude_home() / "harness.db"
    if not new.exists() and old.exists():
        return old
    return new


def skills_dir() -> Path:
    return claude_home() / "skills"


def agents_dir() -> Path:
    return claude_home() / "agents"


def plugins_cache_dir() -> Path:
    return claude_home() / "plugins" / "cache"


def marketplaces_dir() -> Path:
    return claude_home() / "plugins" / "marketplaces"


def projects_dir() -> Path:
    return claude_home() / "projects"


def settings_path() -> Path:
    return claude_home() / "settings.json"


def vault_dir() -> Path:
    return claude_home() / "vault"


def skill_install_path() -> Path:
    return skills_dir() / "tare" / "SKILL.md"


def est_tokens(text: str) -> int:
    """Rough token count for a piece of always-loaded text.

    Deliberately crude -- four characters per token. The audit reports these
    figures to the operator as approximations ("~19,437 tok") and every
    decision made from them is comparative, so precision buys nothing.
    """
    if not text:
        return 0
    return len(text) // 4
