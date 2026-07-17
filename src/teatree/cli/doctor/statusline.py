"""The doctor's statusLine configuration check (PR-17).

Kept in its own module by concern: everything statusline lives here, next to the
installer (:mod:`teatree.cli.setup.statusline_installer`) that writes the block
this check verifies.
"""

from pathlib import Path

import typer

_STATUSLINE_REMEDY = "run `t3 setup` to (re)install it"


def _statusline_command(path: Path) -> str | None:
    """Return the configured statusLine command string, or ``None`` (already WARNed).

    ``None`` covers the three unconfigured states — no settings file, an
    unparsable file, or no ``statusLine.command`` block — each of which is a
    WARN (not a hard failure) since ``t3 setup`` installs the block. A string is
    the command for :func:`check_statusline` to validate.
    """
    import json  # noqa: PLC0415 — deferred, matching the sibling _check_* helpers' cold-import style

    if not path.is_file():
        typer.echo(f"WARN  No statusLine configured ({path} absent) — {_STATUSLINE_REMEDY}.")
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        typer.echo(f"WARN  {path} is unparsable — cannot verify statusLine; {_STATUSLINE_REMEDY}.")
        return None
    block = data.get("statusLine") if isinstance(data, dict) else None
    command = block.get("command") if isinstance(block, dict) else None
    if not isinstance(command, str) or not command:
        typer.echo(f"WARN  No statusLine command configured in {path} — {_STATUSLINE_REMEDY}.")
        return None
    return command


def check_statusline(settings_path: Path | None = None) -> bool:
    """Verify the ``statusLine`` block in ``~/.claude/settings.json`` (PR-17).

    Claude Code reads the statusline command from the user's ``settings.json``.
    This check flags the three failure modes with exact remediation: a missing /
    unconfigured block is a WARN (``t3 setup`` installs it); a relative path (it
    resolves against Claude's cwd and silently breaks) or a missing / non-
    executable target is a hard FAIL. ``settings_path`` defaults to
    ``~/.claude/settings.json`` (parameterised for tests).
    """
    import shlex  # noqa: PLC0415 — deferred, matching the sibling _check_* helpers' cold-import style

    path = settings_path or (Path.home() / ".claude" / "settings.json")
    command = _statusline_command(path)
    if command is None:
        return True
    # The configured command is a shell command line, not a bare path: it can
    # carry arguments (`/abs/statusline.sh --loop`) and a `~` home shorthand.
    # Validate only the executable — the first shell token, home-expanded — so a
    # valid flags-carrying / `~`-anchored command is not falsely flagged (#3313).
    try:
        tokens = shlex.split(command)
    except ValueError:
        typer.echo(f"FAIL  statusLine command is not a valid shell command: {command!r} — {_STATUSLINE_REMEDY}.")
        return False
    if not tokens:
        typer.echo(f"WARN  No statusLine command configured in {path} — {_STATUSLINE_REMEDY}.")
        return True
    return _validate_executable(tokens[0])


def _validate_executable(token: str) -> bool:
    """Validate the statusLine executable — the first shell token, home-expanded.

    A relative path (resolves against Claude's cwd and silently breaks) or a
    missing / non-executable target is a hard FAIL with exact remediation.
    """
    import os  # noqa: PLC0415 — deferred, matching the sibling _check_* helpers' cold-import style

    target = Path(os.path.expanduser(token))  # noqa: PTH111 — expand ~ before the absolute-path check
    if not target.is_absolute():
        typer.echo(f"FAIL  statusLine command is not an absolute path: {token!r} — {_STATUSLINE_REMEDY}.")
        return False
    if not target.is_file():
        typer.echo(f"FAIL  statusLine command target is missing: {target} — {_STATUSLINE_REMEDY}.")
        return False
    if not os.access(target, os.X_OK):
        typer.echo(
            f"FAIL  statusLine command is not executable: {target} — `chmod +x {target}` or {_STATUSLINE_REMEDY}.",
        )
        return False
    return True
