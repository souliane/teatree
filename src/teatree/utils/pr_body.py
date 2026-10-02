"""Canonical per-invocation temp file for a PR/MR body.

The ship flow builds a PR/MR description in-process and hands it to
``gh pr create --body-file``. A fixed shared path such as ``/tmp/pr-body.md`` is
raced by concurrent shippers — one session clobbers another's body between write
and create, and a clobbered body re-injecting a default AI-authorship trailer
then trips the banned-terms gate. :func:`pr_body_tempfile` owns a unique
``mkstemp`` path per call so no two invocations ever share a file, and removes it
on exit. Callers pass body *content*, never a path they name themselves.
"""

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

__all__ = ["pr_body_tempfile"]

_PREFIX = "t3-pr-body-"
_SUFFIX = ".md"


@contextmanager
def pr_body_tempfile(content: str) -> Iterator[Path]:
    """Yield a unique temp file holding *content*; remove it on exit.

    ``mkstemp`` guarantees a distinct path per call, so two overlapping shippers
    cannot race a shared body file. The file lives in the system temp dir with a
    ``t3-pr-body-`` prefix — never inside a worktree, so it can neither be staged
    nor committed (the ``check_pr_body_stray`` gate refuses a hand-named
    ``pr-body.*`` staged in the repo; this canonical path sidesteps it).
    """
    fd, name = tempfile.mkstemp(prefix=_PREFIX, suffix=_SUFFIX)
    path = Path(name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
        yield path
    finally:
        path.unlink(missing_ok=True)
