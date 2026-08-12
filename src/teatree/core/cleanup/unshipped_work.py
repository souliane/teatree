"""Capture what a checkout holds that exists nowhere else, before anything may reap it.

Every reaping pass already decides correctly whether to KEEP a checkout holding
work — and a kept checkout is indistinguishable from a busy one, so the work sits
unobserved until a person notices. This module is the observation: it snapshots
the staged/unstaged/untracked delta and the commits on no remote into the salvage
bundle shape (``<name>.uncommitted.patch`` / ``.commits.patch`` / ``.files`` /
``.meta``) and writes a durable :class:`UnshippedWorkRecord`.

It is purely additive — it reads the checkout and writes elsewhere, so no
disposition changes and no #706/#835 guard is loosened.

``git diff`` compares the working tree to the INDEX, so a checkout whose whole
delta is STAGED reports zero bytes while holding real work. Every probe here is
therefore anchored on a revision (``git diff HEAD``) or on ``git status``.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path

from teatree.core.modelkit.db_retry import retry_on_locked
from teatree.core.models import UnshippedWorkRecord
from teatree.core.worktree.checkout_liveness import wrong_venue_reason
from teatree.paths import get_data_dir, isolated_slug
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

ARTIFACT_NAMESPACE = "unshipped-work"
_INDEX_AWARE_BASE = "HEAD"
# A porcelain entry whose status is a rename/copy carries a SECOND path field.
_RENAME_STATUS = frozenset("RC")


@dataclass(frozen=True, slots=True)
class UnshippedWork:
    """One checkout's unshipped delta: the dirty paths, the patches, the unpushed tips.

    ``unreadable`` carries why the probe could not complete, and counts as work:
    a checkout whose state could not be read has not been proven empty.
    """

    dirty_paths: list[str] = field(default_factory=list)
    uncommitted_patch: str = ""
    unpushed_commits: list[str] = field(default_factory=list)
    commits_patch: str = ""
    unreadable: str = ""

    @property
    def exists(self) -> bool:
        return bool(self.dirty_paths or self.unpushed_commits or self.unreadable)


def _porcelain_paths(checkout: Path) -> list[str]:
    """The paths ``git status`` reports — staged, modified, renamed, and untracked alike.

    Read from the NUL-terminated form, whose records are ``XY <path>`` with a
    rename's two endpoints in two adjacent fields — both endpoints are part of
    the delta, so both are recorded. The text form would C-quote a path holding
    a space and collapse a rename to ``old -> new``, neither of which names a
    file on disk.
    """
    fields = [f for f in git.status_porcelain_z_strict(str(checkout)).split("\0") if f]
    paths: list[str] = []
    expect_rename_source = False
    for entry in fields:
        if expect_rename_source:
            paths.append(entry)
            expect_rename_source = False
            continue
        paths.append(entry[3:])
        expect_rename_source = not _RENAME_STATUS.isdisjoint(entry[:2])
    return sorted({path for path in paths if path})


def _commits_patch(checkout: Path, commits: list[str]) -> str:
    """The full patch of ``commits`` (``"<sha> <subject>"`` lines), oldest first."""
    if not commits:
        return ""
    shas = [line.split(maxsplit=1)[0] for line in commits]
    return git.run_strict(
        repo=str(checkout),
        args=["log", "-p", "--binary", "--no-walk", "--reverse", *shas, "--src-prefix=a/", "--dst-prefix=b/"],
    )


def probe_unshipped_work(checkout: Path) -> UnshippedWork:
    """Read ``checkout``'s unshipped delta without mutating it.

    Read-only on purpose: the checkout may be KEPT, so the index is never touched
    (which rules out the ``git add -N`` trick that would fold untracked files into
    the patch — they are named in ``dirty_paths`` instead). A path that is not
    there holds nothing; only a PRESENT checkout git cannot read is ``unreadable``.

    The venue is asked BEFORE git, because git cannot distinguish the two things
    that stop it reading a checkout: a repository that is broken, and one whose
    admin dir belongs to another execution context (#4272). ``t3`` runs in Docker,
    so every container-created checkout reads as the first from the host — and a
    repository verdict sends the operator hunting for corruption instead of
    re-probing from the context that owns it.
    """
    if not checkout.is_dir():
        return UnshippedWork()
    venue = wrong_venue_reason(checkout)
    if venue:
        return UnshippedWork(unreadable=venue)
    if not git.check(repo=str(checkout), args=["rev-parse", "--verify", "--quiet", _INDEX_AWARE_BASE]):
        return UnshippedWork(unreadable=f"{checkout}: no resolvable HEAD to diff against")
    try:
        dirty_paths = _porcelain_paths(checkout)
        uncommitted_patch = git.run_strict(
            repo=str(checkout),
            args=["diff", _INDEX_AWARE_BASE, "--binary", "--src-prefix=a/", "--dst-prefix=b/"],
        )
        unpushed_commits = git.commits_absent_from_all_remotes(str(checkout), _INDEX_AWARE_BASE)
        commits_patch = _commits_patch(checkout, unpushed_commits)
    except CommandFailedError as exc:
        return UnshippedWork(unreadable=f"{checkout}: could not read unshipped work ({exc})")
    return UnshippedWork(
        dirty_paths=dirty_paths,
        uncommitted_patch=uncommitted_patch,
        unpushed_commits=unpushed_commits,
        commits_patch=commits_patch,
    )


def bundle_path(artifact_prefix: str, suffix: str) -> Path:
    """The bundle file for ``suffix`` — concatenated, never :meth:`Path.with_suffix`.

    ``with_suffix`` would REPLACE an existing one, so a checkout named ``fix-v1.2``
    would silently lose its ``.2``.
    """
    return Path(f"{artifact_prefix}{suffix}")


def _write_bundle(prefix: Path, checkout: Path, branch: str, work: UnshippedWork) -> None:
    prefix.parent.mkdir(parents=True, exist_ok=True)
    meta = (
        f"worktree={checkout}\n"
        f"branch={branch}\n"
        f"dirty={len(work.dirty_paths)} ahead={len(work.unpushed_commits)}\n"
        + (f"unreadable={work.unreadable}\n" if work.unreadable else "")
    )
    for suffix, content in (
        (".uncommitted.patch", work.uncommitted_patch),
        (".commits.patch", work.commits_patch),
        (".files", "".join(f"{path}\n" for path in work.dirty_paths)),
        (".meta", meta),
    ):
        bundle_path(str(prefix), suffix).write_text(content, encoding="utf-8")


def _record_capture(
    checkout: Path, branch: str, overlay: str, prefix: Path, work: UnshippedWork
) -> UnshippedWorkRecord:
    record, _ = UnshippedWorkRecord.objects.update_or_create(
        checkout_path=str(checkout),
        defaults={
            "branch": branch,
            "overlay": overlay,
            "dirty_paths": work.dirty_paths,
            "unpushed_commits": work.unpushed_commits,
            "artifact_prefix": str(prefix),
            "unreadable": work.unreadable,
        },
    )
    return record


def _capture(checkout: Path, branch: str, overlay: str, artifact_root: Path | None) -> UnshippedWorkRecord | None:
    work = probe_unshipped_work(checkout)
    if not work.exists:
        return None
    root = artifact_root if artifact_root is not None else get_data_dir(ARTIFACT_NAMESPACE)
    # The path slug, not the bare dir name: two tickets' worktrees of one repo
    # share a leaf name and would otherwise overwrite each other's bundle.
    prefix = root / f"{checkout.name}-{isolated_slug(checkout)}"
    try:
        _write_bundle(prefix, checkout, branch, work)
    except OSError:
        logger.exception("unshipped-work: could not write the salvage bundle for %s", checkout)
    # The control DB is file-backed SQLite and the factory writes it from many
    # agents at once, so a momentary lock here is routine, not a failure (#1520).
    return retry_on_locked(lambda: _record_capture(checkout, branch, overlay, prefix, work))


def capture_unshipped_work(
    checkout: Path,
    *,
    branch: str = "",
    overlay: str = "",
    artifact_root: Path | None = None,
) -> UnshippedWorkRecord | None:
    """Snapshot ``checkout``'s unshipped work to disk + a durable row; ``None`` when nothing was recorded.

    Non-raising by construction, and that is load-bearing rather than tidy: every
    caller places this AHEAD of its disposition — ahead of the ``force=True``
    hard delete's guards, and inside a sweep that must classify every remaining
    checkout — so an exception here would turn an observation into the crash that
    wedges the teardown. A capture that breaks cleanup is worse than no capture.

    A transient SQLite ``database is locked`` is retried (:func:`retry_on_locked`);
    every other failure — an unwritable artifact root, an unmigrated or genuinely
    stuck control DB, an unreadable checkout — is logged with its traceback and
    degrades to ``None``, so the disposition proceeds uncaptured rather than not
    at all. A bundle that cannot be written still leaves the durable row.
    """
    try:
        return _capture(checkout, branch, overlay, artifact_root)
    except Exception:
        logger.exception(
            "unshipped-work: could not capture %s — it goes unobserved; the disposition continues", checkout
        )
        return None


__all__ = ["ARTIFACT_NAMESPACE", "UnshippedWork", "bundle_path", "capture_unshipped_work", "probe_unshipped_work"]
