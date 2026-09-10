"""An absent ``SWEEP_POLICY`` is reported, not silently defaulted (#4677).

The PR-sweep skill reads the map from ``~/.ac-reviewing-codebase`` and, finding
nothing, treats every repo as ``bulk-update`` — refresh branches, never merge. That is
a defensible default and an indefensible silence: a sweep does half of what the
operator expects on a repo they own, and nothing anywhere distinguishes "no policy was
configured" from "bulk-update was chosen".

An INFO rather than a gate: the file is an owner preference, and a box that genuinely
wants the conservative default is not misconfigured.
"""

from pathlib import Path

import typer

_CONFIG_NAME = ".ac-reviewing-codebase"
_POLICY_KEY = "SWEEP_POLICY"


def _check_sweep_policy_declared(*, config_path: Path | None = None) -> bool:
    """INFO when no ``SWEEP_POLICY`` is declared; silent when one is. Never gates."""
    try:
        path = (Path.home() / _CONFIG_NAME) if config_path is None else config_path
        if not _declares_policy(path):
            typer.echo(
                f"INFO  No {_POLICY_KEY} is declared in {path} — every repo defaults to `bulk-update`, so a "
                f"sweep refreshes branches and merges nothing. Declare one to drain a repo you own, e.g. "
                f'{_POLICY_KEY}="<owner>/(repo-a|repo-b):serial-merge". Its absence gates nothing.'
            )
    except Exception:  # noqa: BLE001 — an optional advisory must never crash or gate the doctor run
        return True
    return True


def _declares_policy(path: Path) -> bool:
    """Whether *path* carries an active (non-commented) ``SWEEP_POLICY`` assignment."""
    try:
        body = path.read_text(encoding="utf-8")
    except OSError:
        return False
    return any(line.lstrip().startswith(f"{_POLICY_KEY}=") for line in body.splitlines())
