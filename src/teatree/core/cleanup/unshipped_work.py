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

The patches are captured VERBATIM — never through ``git.run_strict``, whose
``.strip()`` leaves a patch ``git apply`` rejects as corrupt (#4435) — and read
back by ``t3 <overlay> workspace restore``, which is what keeps that contract
honest instead of an operator discovering it mid-recovery.
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypedDict

from teatree.core.modelkit.db_retry import retry_on_locked
from teatree.core.models import UnshippedWorkRecord
from teatree.core.worktree.checkout_liveness import wrong_venue_reason
from teatree.paths import get_data_dir, isolated_slug
from teatree.utils import git
from teatree.utils.run import CommandFailedError

logger = logging.getLogger(__name__)

ARTIFACT_NAMESPACE = "unshipped-work"
UNCOMMITTED_SUFFIX = ".uncommitted.patch"
COMMITS_SUFFIX = ".commits.patch"
FILES_SUFFIX = ".files"
#: The distinct key an unreadable probe writes to, so it never lands on content.
UNREADABLE_SUFFIX = ".unreadable"
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
    return git.run_strict_verbatim(
        repo=str(checkout),
        args=["log", "-p", "--binary", "--no-walk", "--reverse", *shas, "--src-prefix=a/", "--dst-prefix=b/"],
    )


def _uncommitted_patch(checkout: Path) -> str:
    """The staged + unstaged + untracked delta, as a patch ``git apply`` restores.

    Untracked content is IN scope: the bundle is the last copy of the work, and a
    ``force`` delete would otherwise keep the filename and drop the file. The
    untracked-inclusive route needs a ``read-tree`` and an ``add -N`` against a
    throwaway index; if either is refused, degrade to the tracked-only delta
    rather than report the whole checkout unreadable.
    """
    try:
        return git.full_worktree_diff(str(checkout), _INDEX_AWARE_BASE)
    except CommandFailedError:
        return git.run_strict_verbatim(
            repo=str(checkout),
            args=["diff", _INDEX_AWARE_BASE, "--binary", "--src-prefix=a/", "--dst-prefix=b/"],
        )


def probe_unshipped_work(checkout: Path) -> UnshippedWork:
    """Read ``checkout``'s unshipped delta without mutating it.

    Read-only on purpose: the checkout may be KEPT and may hold a live agent, so
    its own index is never touched — the ``git add -N`` that folds untracked files
    into the patch runs against a throwaway index instead (:func:`_uncommitted_patch`).
    A path that is not there holds nothing; only a PRESENT checkout git cannot
    read is ``unreadable``.

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
        uncommitted_patch = _uncommitted_patch(checkout)
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
    """Write the bundle — the four content artifacts ONLY when the read succeeded.

    A read this venue could not complete proves nothing about the delta, so its
    empty patches must never land on top of a good capture: the same checkout
    swept from a venue that cannot resolve its gitdir used to overwrite a real
    152-byte patch with 0 bytes and blank the file list (#4435). The cause goes
    to its own key, which a later successful read clears.
    """
    prefix.parent.mkdir(parents=True, exist_ok=True)
    marker = bundle_path(str(prefix), UNREADABLE_SUFFIX)
    if work.unreadable:
        marker.write_text(f"{work.unreadable}\n", encoding="utf-8")
        return
    meta = f"worktree={checkout}\nbranch={branch}\ndirty={len(work.dirty_paths)} ahead={len(work.unpushed_commits)}\n"
    for suffix, content in (
        (UNCOMMITTED_SUFFIX, work.uncommitted_patch),
        (COMMITS_SUFFIX, work.commits_patch),
        (FILES_SUFFIX, "".join(f"{path}\n" for path in work.dirty_paths)),
        (".meta", meta),
    ):
        bundle_path(str(prefix), suffix).write_text(content, encoding="utf-8")
    marker.unlink(missing_ok=True)


class RecordDefaults(TypedDict, total=False):
    """The ``update_or_create`` defaults for one capture — partial by design.

    An unreadable read omits the delta keys rather than writing them empty, so
    ``update_or_create`` leaves the last good capture's values in place.
    """

    branch: str
    overlay: str
    dirty_paths: list[str]
    unpushed_commits: list[str]
    artifact_prefix: str
    unreadable: str


def _record_defaults(branch: str, overlay: str, prefix: Path, work: UnshippedWork) -> RecordDefaults:
    """The row fields to write — an unreadable read updates the cause, never the delta.

    Blanking ``dirty_paths``/``unpushed_commits`` because THIS venue could not
    read the checkout discards the last successful capture's account of what is
    in there, which is the only account anything has.
    """
    if work.unreadable:
        partial: RecordDefaults = {"artifact_prefix": str(prefix), "unreadable": work.unreadable}
        # A blank branch/overlay is "the caller did not say", not "it has none".
        if branch:
            partial["branch"] = branch
        if overlay:
            partial["overlay"] = overlay
        return partial
    return {
        "branch": branch,
        "overlay": overlay,
        "dirty_paths": work.dirty_paths,
        "unpushed_commits": work.unpushed_commits,
        "artifact_prefix": str(prefix),
        "unreadable": "",
    }


def _record_capture(
    checkout: Path, branch: str, overlay: str, prefix: Path, work: UnshippedWork
) -> UnshippedWorkRecord:
    record, _ = UnshippedWorkRecord.objects.update_or_create(
        checkout_path=str(checkout),
        defaults=_record_defaults(branch, overlay, prefix, work),
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


__all__ = [
    "ARTIFACT_NAMESPACE",
    "COMMITS_SUFFIX",
    "FILES_SUFFIX",
    "UNCOMMITTED_SUFFIX",
    "UNREADABLE_SUFFIX",
    "UnshippedWork",
    "bundle_path",
    "capture_unshipped_work",
    "probe_unshipped_work",
]
