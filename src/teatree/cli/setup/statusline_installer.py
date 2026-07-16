"""Install the top-level ``statusLine`` block into ``~/.claude/settings.json`` (PR-17).

Claude Code reads the statusline command from the user's ``settings.json``, not
from a plugin-distributed one — so ``t3 setup`` writes the block pointing at the
main clone's ``hooks/scripts/statusline.sh`` (absolute path, computed portably).
The plugin's own ``settings.json`` must NOT carry a ``statusLine`` block, or it
would try to distribute one statusline command to every user of the plugin.

Never clobbers: an existing ``statusLine`` block (the user may have pointed it
somewhere deliberate) is left untouched. The companion doctor check
(:func:`teatree.cli.doctor.statusline.check_statusline`) verifies presence,
absolute path, and executability with exact remediation.
"""

import json
from enum import StrEnum
from pathlib import Path
from typing import Any

_STATUSLINE_REL = Path("hooks") / "scripts" / "statusline.sh"


class StatuslineInstall(StrEnum):
    """Outcome of an :func:`install_statusline` call."""

    INSTALLED = "installed"
    ALREADY_PRESENT = "already-present"
    UNREADABLE = "unreadable"
    UNWRITABLE = "unwritable"


def statusline_command_path(repo: Path) -> str:
    """Return the absolute, portable path to the main clone's ``statusline.sh``."""
    return str((repo / _STATUSLINE_REL).resolve())


def install_statusline(settings_path: Path, repo: Path) -> StatuslineInstall:
    """Write the ``statusLine`` block into *settings_path*, never clobbering.

    A settings file already carrying a ``statusLine`` key is left untouched
    (:attr:`StatuslineInstall.ALREADY_PRESENT`) — the user's own choice wins.
    A missing file is created with just the block. An unparsable file is left
    alone (:attr:`StatuslineInstall.UNREADABLE`) so setup never corrupts hand-
    edited JSON. A target the process cannot write — e.g. a root-owned parent
    dir in the headless container, where only ``~/.claude/projects`` is bind-
    mounted so Docker creates ``~/.claude`` root-owned — is reported as
    :attr:`StatuslineInstall.UNWRITABLE` rather than raising, so this
    convenience write is never fatal to ``t3 setup``. The block is
    ``{"type": "command", "command": <abs path>}``.
    """
    data: dict[str, Any] = {}
    if settings_path.is_file():
        try:
            loaded = json.loads(settings_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return StatuslineInstall.UNREADABLE
        if not isinstance(loaded, dict):
            return StatuslineInstall.UNREADABLE
        data = loaded
        if "statusLine" in data:
            return StatuslineInstall.ALREADY_PRESENT

    data["statusLine"] = {"type": "command", "command": statusline_command_path(repo)}
    try:
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        settings_path.write_text(json.dumps(data, indent=4) + "\n", encoding="utf-8")
    except OSError:
        return StatuslineInstall.UNWRITABLE
    return StatuslineInstall.INSTALLED
