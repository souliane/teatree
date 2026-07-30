"""Scoped ``os.environ`` mutation that always restores the prior state."""

import os
from collections.abc import Iterator, Mapping
from contextlib import contextmanager


@contextmanager
def patched_environ(overrides: Mapping[str, str], *, remove: tuple[str, ...] = ()) -> Iterator[None]:
    """Apply *overrides* to ``os.environ`` (dropping *remove* keys) for the block, then restore.

    Only the touched keys are saved and restored, so a concurrent, unrelated
    ``os.environ`` change survives. A permanent ``os.environ`` mutation in the
    long-lived loop process would bleed one worktree's overlay env into every
    later provision; scoping it to the block is the fix.
    """
    touched = set(overrides) | set(remove)
    saved = {key: os.environ.get(key) for key in touched}
    try:
        os.environ.update(overrides)
        for key in remove:
            os.environ.pop(key, None)
        yield
    finally:
        for key, previous in saved.items():
            if previous is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = previous
