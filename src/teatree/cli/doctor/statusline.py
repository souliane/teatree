"""The doctor's statusLine configuration + freshness checks (PR-17).

Kept in its own module by concern: everything statusline lives here, next to the
installer (:mod:`teatree.cli.setup.statusline_installer`) that writes the block
:func:`check_statusline` verifies. :func:`check_statusline_freshness` is the
silent-freeze guard: it FAILs when the pre-rendered ``statusline.txt`` has aged past
the same stale cutoff the readers use while ``autoload`` is ON, so a headless render
chain that has stopped keeping the file fresh can never regress unnoticed.
"""

from pathlib import Path

import typer

_STATUSLINE_REMEDY = "run `t3 setup` to (re)install it"

_FRESHNESS_REMEDY = (
    "the `t3 worker` render chain (teatree.loops.statusline_refresh) is not keeping it "
    "fresh — check `t3 worker status` and the worker logs"
)


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


def _autoload_on() -> bool:
    """Whether the ``autoload`` #256 owner flag resolves ON. Fail-safe OFF.

    A colleague / opted-out box (``autoload`` off) shows no loop statusline at all, so a
    frozen file there is expected, not a fault — the freshness check is a no-op unless the
    owner has engaged the statusline. A settings-read failure degrades to OFF (skip).
    """
    try:
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred, matching the sibling probes

        return bool(get_effective_settings().autoload)
    except Exception:  # noqa: BLE001 — a broken config read must never turn the check into a spurious FAIL
        return False


def check_statusline_freshness(statusline_path: Path | None = None, *, now: float | None = None) -> bool:
    """FAIL when the pre-rendered statusline is stale while ``autoload`` is ON.

    The shell hook and ``t3 loop status`` read ``statusline.txt`` verbatim, so a render
    chain that has stopped keeping it fresh leaves every reader showing a confident,
    hours-old loop line with no other signal. This check is the silent-freeze backstop: it
    resolves the render age from the SAME ``tick-meta.json`` source and the SAME
    :func:`~teatree.loop.statusline_staleness.stale_cutoff_seconds` math the readers'
    stale banner uses, and hard-FAILs past the cutoff so the never-freeze invariant is
    machine-checked. A no-op (OK) when ``autoload`` is OFF (a box with no loop statusline)
    or when the age cannot be determined (never rendered — a fresh box; fail-open, matching
    the readers). ``statusline_path`` / ``now`` are parameterised for tests.
    """
    if not _autoload_on():
        return True

    from teatree.config import cadence_seconds  # noqa: PLC0415 — deferred, matching the sibling probes
    from teatree.loop.statusline import default_path  # noqa: PLC0415 — deferred, matching the sibling probes
    from teatree.loop.statusline_staleness import (  # noqa: PLC0415 — deferred, matching the sibling probes
        render_age_seconds,
        stale_cutoff_seconds,
    )

    path = statusline_path or default_path()
    age = render_age_seconds(path, now=now)
    if age is None:
        return True
    cutoff = stale_cutoff_seconds(cadence_seconds())
    if age > cutoff:
        typer.echo(
            f"FAIL  statusline is STALE — last rendered {int(age)}s ago (cutoff {cutoff}s) while autoload is on; "
            f"{_FRESHNESS_REMEDY}.",
        )
        return False
    return True
