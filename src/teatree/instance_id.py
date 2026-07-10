"""Durable per-installation instance identity (fleet-safety Stage 1).

Two teatree instances (a laptop and a headless box) running against the same
GitHub repo keep their coordination state in per-instance SQLite, while the work
domain (issues, branches, PRs) is global on the forge. Nothing in the local
state names *which* instance holds a claim, so claims are invisible across
instances and the same work gets double-claimed.

This module gives the installation a stable identity: a UUID persisted once in
the machine data dir, read identically by the main clone and every worktree
checkout on the machine (:func:`teatree.paths.machine_data_dir` deliberately
resolves to the non-isolated dir). The id is stamped into claim/lease metadata
so a claim can name its owner, and it is the identity Stage 2's GitHub claim
refs will fence on. It is not a network identity — a persisted UUID4 is
deliberately enough, and it must never require network access to resolve.
"""

import contextlib
import os
import tempfile
import uuid
from pathlib import Path

_INSTANCE_ID_FILENAME = "instance_id"


def machine_data_dir(*, env: dict[str, str], home: Path) -> Path:
    """The per-machine teatree data dir — never the worktree-isolated variant.

    The primary-clone form of the data dir (``$XDG_DATA_HOME/teatree`` or
    ``~/.local/share/teatree``), computed without the worktree auto-isolation
    :func:`teatree.paths.resolve_data_dir` applies, so a worktree checkout and
    the main clone resolve the SAME directory. That is what lets every process
    on the machine read one identical :func:`instance_id`.
    """
    base = Path(env["XDG_DATA_HOME"]) if env.get("XDG_DATA_HOME") else home / ".local" / "share"
    return base / "teatree"


def _read_valid(path: Path) -> str:
    """Return the stored UUID string, or ``""`` when absent/unreadable/malformed."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    try:
        uuid.UUID(text)
    except ValueError:
        return ""
    return text


def read_or_create_instance_id(data_dir: Path) -> str:
    """Read the persisted instance id under *data_dir*, creating it on first call.

    Stable across restarts: the value is written once and read verbatim
    thereafter. Concurrency-safe under a fleet startup: the create publishes via
    an atomic :func:`os.link`, so the first writer wins and every racing writer
    falls through to read the winner's value — the id never forks.
    """
    path = data_dir / _INSTANCE_ID_FILENAME
    stored = _read_valid(path)
    if stored:
        return stored
    data_dir.mkdir(parents=True, exist_ok=True)
    candidate = str(uuid.uuid4())
    fd, tmp_name = tempfile.mkstemp(prefix=".instance-id-", dir=data_dir)
    tmp_path = Path(tmp_name)
    try:
        os.write(fd, candidate.encode("utf-8"))
        os.close(fd)
        # os.link is atomic and fails if the target exists: the first writer
        # wins and every racing writer falls through to read the winner's value.
        with contextlib.suppress(FileExistsError):
            os.link(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)
    return _read_valid(path) or candidate


def instance_id() -> str:
    """The stable id for this teatree installation.

    Resolves the machine data dir at call time (:func:`teatree.paths.machine_data_dir`),
    so every process on the machine — main clone or worktree — reads the same
    persisted file. The read is a few bytes; correctness (respecting the live
    ``HOME``/``XDG_DATA_HOME``) beats caching a frozen path.
    """
    return read_or_create_instance_id(machine_data_dir(env=dict(os.environ), home=Path.home()))
