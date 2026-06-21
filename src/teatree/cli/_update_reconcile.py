"""Content-safe self-heal of a clone whose ff-pull failed on squash-merge divergence (#2400).

``t3 update`` (``teatree.cli.update``) does a per-clone ``git pull --ff-only``.
When a clone's local commits already landed *squash-merged* upstream (their
patches are on ``origin/<default>`` under a new SHA), the clone shows ``[ahead
N, behind M]`` and ``--ff-only`` aborts. :func:`reconcile_squash_merged`
self-heals it — but only when the unique commits are provably already upstream.

The subject classifier
(:func:`teatree.core.branch_classification.classify_branch_commits`) is only a
cheap PRE-FILTER: it buckets ``squash_merged`` by canonicalized-subject
membership alone, with no content/patch-id/tree check, so a genuine commit can
slip past it (subject collision, amended content, evil-merge). The destructive
``git reset --hard`` is authorized by an AUTHORITATIVE *content* gate instead —
``git cherry`` (patch-id) plus a merge-commit check — never by subject. A
recoverable backup ref is created at the pre-reset HEAD as defense-in-depth.

This module is a leaf helper of ``teatree.cli.update``: it imports the result
types and small git helpers from there; ``update`` calls
:func:`reconcile_squash_merged` via a function-local import to break the cycle.
"""

from pathlib import Path
from typing import TYPE_CHECKING

import typer

from teatree.core.branch_classification import classify_branch_commits

if TYPE_CHECKING:
    from teatree.cli.update import RepoUpdate

# How many commit shas to list in a divergence warning before eliding —
# mirrors the clean-all reaper's preview cap (``core.cleanup._SUBJECT_PREVIEW_LIMIT``).
_SUBJECT_PREVIEW_LIMIT = 3

# A full git sha is 40 hex chars; below this a "sha" is a fail-safe diagnostic
# string (e.g. "(git cherry failed …)") that must be surfaced verbatim, not sliced.
_SHORT_SHA_LEN = 7


def _cherry_not_upstream(repo: Path, target: str) -> list[str]:
    """Return the sha(s) of unique non-merge commits whose patch is NOT upstream.

    ``git cherry <target> HEAD`` compares each commit reachable from HEAD but
    not from ``target`` by **patch-id** (content), not by SHA or subject: a
    ``-`` prefix means the patch already landed upstream (typical squash-merge),
    a ``+`` prefix means the patch is genuinely un-upstreamed work. This is the
    authoritative content check the subject classifier cannot do — a genuine
    commit whose subject merely collides with an upstream subject (vector B) or
    an amended commit that added content after the original was squashed
    (vector C) both show ``+`` here even though the subject matcher buckets them
    ``squash_merged``. Returns the ``+`` sha(s); an empty list means every
    non-merge unique commit is patch-equivalent upstream.
    """
    from teatree.cli.update import _git  # noqa: PLC0415

    result = _git(repo, "cherry", target, "HEAD", expected_codes=None)
    if result.returncode != 0:
        # A failed `git cherry` is inconclusive — degrade safely by reporting an
        # opaque genuine sha so the caller refuses the reset (never reaps work
        # on an uncertain content check).
        return ["(git cherry failed — content check inconclusive)"]
    plus: list[str] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("+"):
            plus.append(stripped[1:].strip())
    return plus


def _merge_commits_in_range(repo: Path, target: str) -> list[str]:
    """Return merge-commit sha(s) reachable from HEAD but not from ``target``.

    ``git rev-list --merges <target>..HEAD`` lists merge commits unique to the
    branch. A merge commit can carry content present in neither parent (an
    evil-merge, vector D) and has no single patch-id ``git cherry`` can compare,
    so its content cannot be cheaply proven already-upstream. Any merge commit
    in the unique range therefore blocks the reset conservatively.
    """
    from teatree.cli.update import _git  # noqa: PLC0415

    result = _git(repo, "rev-list", "--merges", f"{target}..HEAD", expected_codes=None)
    if result.returncode != 0:
        return ["(git rev-list --merges failed — merge check inconclusive)"]
    return [sha.strip() for sha in result.stdout.splitlines() if sha.strip()]


def _create_reconcile_backup_ref(repo: Path, head_sha: str) -> str:
    """Create a recoverable ref at *head_sha* before a reconcile reset; return its name.

    Defense-in-depth (#2400): even with the content gate, a reset is destructive,
    so the pre-reset HEAD is captured under a ``refs/t3-reconcile-backup/<sha>``
    ref. ``git update-ref`` force-creates (overwrites) the ref, so a re-run on the
    same HEAD never fails on a name clash. The ref keeps the old commits
    reachable (not just in the reflog), making any future misclassification
    trivially recoverable via ``git reset --hard <ref>``.
    """
    from teatree.cli.update import _git  # noqa: PLC0415

    ref = f"refs/t3-reconcile-backup/{head_sha}"
    _git(repo, "update-ref", ref, head_sha, expected_codes=None)
    return ref


def reconcile_squash_merged(name: str, repo: Path, old_sha: str, pull_stderr: str) -> "RepoUpdate":
    """Self-heal a clone whose ff-pull failed purely on squash-merged divergence (#2400).

    The recurring brick: a clone shows ``[ahead N, behind M]`` because its
    local-unique commits were squash-merged upstream — their patches already landed
    under a NEW SHA on ``origin/<default>``. ``git pull --ff-only`` then aborts with
    "Not possible to fast-forward", bricking the clone's update.

    This is reached ONLY after the precondition gate confirmed the clone is on its
    default branch with an upstream (a feature-branch checkout short-circuits to
    SKIPPED earlier), so a reconcile here always acts on the default branch.

    Data-loss-free by construction. The subject classifier
    (:func:`classify_branch_commits`) is only a cheap PRE-FILTER — it must NOT
    authorize the destructive ``git reset --hard``, because it buckets by
    canonicalized-subject membership alone with NO content/patch-id/tree check: a
    genuine un-upstreamed commit whose subject collides with an unrelated upstream
    subject (vector B), an amended commit that added content after the original
    squash (vector C), or a merge commit carrying unique content (vector D) all
    slip past it. So before any reset an AUTHORITATIVE content gate runs — the
    reset proceeds ONLY if (1) ``git cherry`` reports every unique non-merge
    commit as patch-equivalent upstream (no ``+`` line) AND (2) there are no merge
    commits in the unique range. If either fails the clone is kept and a LOUD
    multi-line warning names the genuine sha(s), so genuine work is never
    destroyed. Immediately before the authorized reset a recoverable
    ``refs/t3-reconcile-backup/<sha>`` ref is created at the pre-reset HEAD (and
    named in the log) for trivial recovery.
    """
    from teatree.cli.update import (  # noqa: PLC0415
        RepoUpdate,
        UpdateStatus,
        _commit_count,
        _current_branch,
        _default_branch,
        _git,
        _short_sha,
    )

    default_branch = _default_branch(repo)
    target = f"origin/{default_branch}" if default_branch else "origin/main"
    branch = _current_branch(repo)
    classification = classify_branch_commits(str(repo), branch, target=target)

    if classification.genuinely_ahead:
        return _refuse_reconcile(
            name,
            repo,
            target,
            reason="genuine un-upstreamed commit(s)",
            shas=[c.sha for c in classification.genuinely_ahead],
        )

    # AUTHORITATIVE content gate — the subject classifier above is only a cheap
    # pre-filter; the reset is authorized solely by content, never by subject.
    cherry_plus = _cherry_not_upstream(repo, target)
    if cherry_plus:
        return _refuse_reconcile(
            name,
            repo,
            target,
            reason="commit(s) whose patch is NOT upstream (subject collision / amended content)",
            shas=cherry_plus,
        )
    merge_shas = _merge_commits_in_range(repo, target)
    if merge_shas:
        return _refuse_reconcile(
            name,
            repo,
            target,
            reason="merge commit(s) that may carry content not provably upstream",
            shas=merge_shas,
        )

    dropped = len(classification.squash_merged) + len(classification.merge_commits)
    head_sha = _git(repo, "rev-parse", "HEAD").stdout.strip()
    backup_ref = _create_reconcile_backup_ref(repo, head_sha)
    reset = _git(repo, "reset", "--hard", target, expected_codes=None)
    if reset.returncode != 0:
        return RepoUpdate(
            name,
            UpdateStatus.FAILED,
            reason=f"git pull --ff-only failed: {pull_stderr} (reconcile reset failed: {reset.stderr.strip()})",
        )
    plural = "commit" if dropped == 1 else "commits"
    typer.echo(
        f"OK    reconciled squash-merged clone {repo} -> {target} "
        f"(dropped {dropped} already-upstream duplicate {plural}; "
        f"pre-reset HEAD {head_sha[:7]} backed up at {backup_ref} — recover with "
        f"git -C {repo} reset --hard {backup_ref})"
    )
    new_sha = _short_sha(repo)
    advanced = _commit_count(repo, old_sha, new_sha) if new_sha != old_sha else 0
    return RepoUpdate(name, UpdateStatus.UPDATED, old_sha=old_sha, new_sha=new_sha, advanced=advanced)


def _refuse_reconcile(name: str, repo: Path, target: str, *, reason: str, shas: list[str]) -> "RepoUpdate":
    """Emit the loud refuse-and-keep warning and return a hard FAILED outcome.

    The single loud-warn path for every reason the reconcile must NOT reset —
    subject-classified genuine work, ``git cherry`` ``+`` patches, and merge
    commits in the unique range all funnel here, so genuine work is surfaced
    identically and never destroyed.
    """
    from teatree.cli.update import RepoUpdate, UpdateStatus  # noqa: PLC0415

    preview = shas[:_SUBJECT_PREVIEW_LIMIT]
    listed = ", ".join(sha[:7] if len(sha) >= _SHORT_SHA_LEN else sha for sha in preview)
    if len(shas) > _SUBJECT_PREVIEW_LIMIT:
        listed += ", …"
    typer.echo("")
    typer.echo(f"!! WARNING: {name} has diverged from {target} with {reason}.")
    typer.echo(f"!! Commit(s) NOT provably on {target}: {listed}")
    typer.echo(f"!! Refusing to reconcile {repo} — pushing them to a new branch preserves the work.")
    typer.echo(f"!! Fix: git -C {repo} push origin HEAD:refs/heads/<a-new-branch>, then re-run `t3 update`.")
    typer.echo("")
    return RepoUpdate(
        name,
        UpdateStatus.FAILED,
        reason=(f"diverged with {len(shas)} {reason} ({listed}) — push them to a new branch, then re-run `t3 update`"),
    )
