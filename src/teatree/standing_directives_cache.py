"""The resolved standing directives, published where the Django-free hooks read them.

The worker resolves the directives (the owner's ``Prompt`` overrides and the mode brake) and the
asking session's hooks run on the host, so the bind-mounted primary data dir is the one place both
reach. One file, replaced atomically and only when the published list changes; a missing, corrupt or
unknown-schema file reads as "nothing published", which the hooks answer with the compiled defaults.
Every publisher resolves and writes under one ``flock`` on :data:`LOCK_FILENAME` beside the file, so a
resolution read before an owner's edit never lands after the edit's own publication, whichever process
or container each runs in.
"""

import fcntl
import json
import os
import tempfile
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TypedDict, TypeGuard

from teatree.paths import ControlDb

CACHE_FILENAME = "standing-directives.json"
LOCK_FILENAME = ".standing-directives.lock"
SCHEMA = 1


class StandingDirectivePayload(TypedDict):
    """The cross-harness directive contract — what ``t3 loop directives show --json`` prints, per directive."""

    slot_id: str
    cadence_seconds: int
    text: str
    scope: str


def cache_path(*, env: Mapping[str, str] = os.environ) -> Path:
    return ControlDb(env).primary_data_dir() / CACHE_FILENAME


def _is_payload(entry: object) -> bool:
    return (
        isinstance(entry, dict)
        and set(entry) == set(StandingDirectivePayload.__annotations__)
        and isinstance(entry["slot_id"], str)
        and type(entry["cadence_seconds"]) is int
        and isinstance(entry["text"], str)
        and isinstance(entry["scope"], str)
    )


def _is_payload_list(directives: object) -> TypeGuard[list[StandingDirectivePayload]]:
    return isinstance(directives, list) and all(_is_payload(entry) for entry in directives)


def read(*, env: Mapping[str, str] = os.environ) -> list[StandingDirectivePayload] | None:
    """The published directives, or ``None`` when nothing readable is published."""
    try:
        document = json.loads(cache_path(env=env).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(document, dict) or document.get("schema") != SCHEMA:
        return None
    directives = document.get("directives")
    return directives if _is_payload_list(directives) else None


def publish(resolve: Callable[[], list[StandingDirectivePayload]], *, env: Mapping[str, str] = os.environ) -> bool:
    """Publish what *resolve* returns, resolved under the publisher lock; ``False`` when it is already published."""
    target = cache_path(env=env)
    target.parent.mkdir(parents=True, exist_ok=True)
    with (target.parent / LOCK_FILENAME).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _replace(target, resolve(), env=env)


def write(directives: list[StandingDirectivePayload], *, env: Mapping[str, str] = os.environ) -> bool:
    """Publish *directives*; ``False`` when exactly this list is already published."""
    return publish(lambda: directives, env=env)


def _replace(target: Path, directives: list[StandingDirectivePayload], *, env: Mapping[str, str]) -> bool:
    if read(env=env) == directives:
        return False
    handle, staged = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump({"schema": SCHEMA, "written_at": time.time(), "directives": directives}, stream)
        Path(staged).replace(target)
    finally:
        Path(staged).unlink(missing_ok=True)
    return True


__all__ = [
    "CACHE_FILENAME",
    "LOCK_FILENAME",
    "SCHEMA",
    "StandingDirectivePayload",
    "cache_path",
    "publish",
    "read",
    "write",
]
