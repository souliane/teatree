"""Per-ref isolation, locking and retention for external E2E specs checkouts.

One shared clone per repo NAME, reset with ``git reset --hard FETCH_HEAD`` on
every run, is a silent data race: two agents on two branches interleave their
resets inside one 13-minute run, so a spec executes against a tree that is not
its own and reports a result about the wrong code. The failure never crashes —
it produces a confident, plausible, wrong answer.

The isolation key is the REF, not the run. Different refs get different working
directories, so the collision that actually happened cannot occur and costs no
wait; the same ref keeps ONE warm checkout, so a repeat run does not re-pay for
``npm ci``. Two runs of the same ref genuinely do share a tree, and that residual
is serialised by a kernel flock — refused loudly, never silently interleaved.

Everything here is pure with respect to teatree's data dir: the caller passes the
root in, so this module has no opinion about where the cache lives.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from teatree.utils.singleton import current_context, flock_is_held, read_holder

#: The data-dir namespace holding per-ref checkouts. Deliberately NOT the
#: pre-isolation ``e2e-repos`` namespace: that layout keys a git working tree at
#: ``<root>/<name>``, exactly where this one needs a directory of refs.
SPECS_NAMESPACE = "e2e-specs"

#: How many ref checkouts to keep per repo. Each is a full clone plus its
#: ``node_modules`` (~400MB for a real suite), so an unbounded set of branches
#: would trade a correctness bug for a disk-exhaustion one.
DEFAULT_KEEP_CHECKOUTS = 3

_SLUG_SAFE = re.compile(r"[^A-Za-z0-9._-]+")
_SLUG_READABLE_CHARS = 40
_SLUG_DIGEST_CHARS = 10
_LOCK_SUFFIX = ".lock"


class SpecsCheckoutBusyError(RuntimeError):
    """Another live run holds this repo+ref checkout.

    Refused rather than queued: the holder's run lasts minutes, so a silent block
    is indistinguishable from a hang. The message names the ref and the holder so
    the caller can wait deliberately or pick another ref.
    """

    def __init__(self, *, name: str, ref: str, detail: str) -> None:
        super().__init__(
            f"E2E specs checkout for '{name}' at ref '{ref}' is in use by another run — "
            f"refusing to share it, because preparing it would reset the tree that run is "
            f"executing against. {detail} Wait for it to finish, or run a different --branch/--ref "
            f"(distinct refs get isolated checkouts and never contend).",
        )
        self.name = name
        self.ref = ref


def ref_slug(ref: str) -> str:
    """A filesystem-safe directory name that is unique per ref.

    Sanitising alone would collide (``team/spec`` and ``team-spec`` both become
    ``team-spec``), and a collision here re-creates the very tree-sharing this
    module exists to prevent — so a digest of the exact ref is appended.
    """
    readable = _SLUG_SAFE.sub("-", ref).strip("-")[:_SLUG_READABLE_CHARS]
    digest = hashlib.sha256(ref.encode()).hexdigest()[:_SLUG_DIGEST_CHARS]
    return f"{readable}-{digest}" if readable else digest


def repo_root(root: Path, name: str) -> Path:
    return root / _SLUG_SAFE.sub("-", name).strip("-")


def checkout_path(root: Path, name: str, ref: str) -> Path:
    """Where this repo's checkout of this ref lives — one directory per ref."""
    return repo_root(root, name) / ref_slug(ref)


def lock_path(root: Path, name: str, ref: str) -> Path:
    """The flock anchor for a repo+ref, kept BESIDE its checkout, never inside it.

    A lock file inside the tree would be unlinked by a checkout wipe, orphaning a
    live holder's kernel lock on the removed inode.
    """
    return repo_root(root, name) / f"{ref_slug(ref)}{_LOCK_SUFFIX}"


def is_locked(root: Path, name: str, ref: str) -> bool:
    return flock_is_held("", pid_path=lock_path(root, name, ref))


#: Descriptors held until this process exits, keyed by lock path. A CLI run owns its
#: specs checkout for the whole run and the run IS the process, so the kernel's
#: release-on-exit (crash included) is exactly the right lifetime.
_process_locks: dict[Path, int] = {}


def hold_for_process(root: Path, name: str, ref: str) -> None:
    """Claim this repo+ref checkout for the rest of the process, or refuse loudly.

    Process-lifetime rather than scoped to a ``with``, because a CLI run prepares the
    checkout in one function and hands the path to Playwright in another: a lock
    released when preparation returns would leave the tree unguarded for exactly the
    minutes that matter. The kernel drops it when the run exits, crash included.
    """
    path = lock_path(root, name, ref)
    if path in _process_locks:
        return  # this process already owns it; re-asserting ownership is not a rival
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as exc:
        holder = read_holder(path)
        os.close(fd)
        detail = f"Held by PID {holder.pid} ({holder.context.describe()})." if holder else "The holder is unrecorded."
        raise SpecsCheckoutBusyError(name=name, ref=ref, detail=detail) from exc
    os.ftruncate(fd, 0)
    os.write(fd, f"{os.getpid()}\n{json.dumps(current_context().as_json())}\n".encode())
    _process_locks[path] = fd


def release_process_locks() -> None:
    """Drop every process-lifetime claim this process holds.

    The kernel does this on exit, so a CLI run never calls it. A test process runs many
    "runs" in one interpreter and needs the claims cleared between them.
    """
    for fd in _process_locks.values():
        with contextlib.suppress(OSError):
            os.close(fd)
    _process_locks.clear()


@contextmanager
def _claim_for_reap(lock: Path) -> Iterator[bool]:
    """Hold *lock* exclusively for the body, yielding whether the claim succeeded.

    The reaper cannot ask :func:`flock_is_held` and then delete: that probe acquires
    and releases, so between its answer and the ``rmtree`` a rival run is free to take
    the lock and start executing against the tree — the same check-then-act race, on
    the same trees, that this module exists to close. Deleting UNDER the exclusive
    lock removes the window: a rival either loses the race and is refused with
    :class:`SpecsCheckoutBusyError`, or wins it and keeps a tree we then skip.
    """
    lock.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True
    finally:
        with contextlib.suppress(OSError):
            os.close(fd)  # releases the flock with the last descriptor


def prune_stale_checkouts(root: Path, name: str, *, keep: int = DEFAULT_KEEP_CHECKOUTS) -> list[Path]:
    """Drop the least-recently-used ref checkouts beyond *keep*, never a live one.

    Recency is each checkout's own lock-file mtime, which every acquire rewrites —
    so "last used" is recorded by the mechanism that guards it, with no extra
    bookkeeping file inside the git tree. Each removal happens while this process
    holds that checkout's own lock, so "not in use" cannot go stale mid-delete.
    """
    parent = repo_root(root, name)
    if not parent.is_dir():
        return []
    checkouts = sorted((path for path in parent.iterdir() if path.is_dir()), key=_last_used, reverse=True)
    removed: list[Path] = []
    for path in checkouts[max(keep, 1) :]:
        with _claim_for_reap(path.with_name(f"{path.name}{_LOCK_SUFFIX}")) as claimed:
            if not claimed:
                continue  # a live run holds it
            with contextlib.suppress(OSError):
                shutil.rmtree(path)
                removed.append(path)
    return removed


def _last_used(checkout: Path) -> float:
    lock = checkout.with_name(f"{checkout.name}{_LOCK_SUFFIX}")
    probe = lock if lock.exists() else checkout
    try:
        return probe.stat().st_mtime
    except OSError:
        return 0.0
