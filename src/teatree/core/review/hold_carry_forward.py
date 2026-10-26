"""Carry a held finding only when every cited file has the same Git blob at the new head."""

from pathlib import Path

from django.db import transaction

from teatree.core.models.review_verdict import ReviewVerdict
from teatree.core.worktree.clone_paths import matching_clone
from teatree.utils.git_run import git_env_non_interactive
from teatree.utils.run import run_allowed_to_fail


class HoldCarryUnreadableError(RuntimeError):
    pass


def _git(repo: Path, *args: str, timeout: float = 10) -> tuple[int, str]:
    try:
        result = run_allowed_to_fail(
            ["git", "-C", str(repo), *args], expected_codes=None, env=git_env_non_interactive(), timeout=timeout
        )
    except (OSError, RuntimeError) as exc:
        message = f"could not read Git blobs ({type(exc).__name__})"
        raise HoldCarryUnreadableError(message) from exc
    return result.returncode, result.stdout.strip()


def _ensure_commit(repo: Path, sha: str) -> None:
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] == 0:
        return
    if _git(repo, "fetch", "--no-tags", "origin", sha, timeout=30)[0] != 0:
        message = f"review head {sha} is unavailable in the matching clone"
        raise HoldCarryUnreadableError(message)
    if _git(repo, "cat-file", "-e", f"{sha}^{{commit}}")[0] != 0:
        message = f"review head {sha} is unreadable after fetch"
        raise HoldCarryUnreadableError(message)


def _same_blobs(repo: Path, old: str, new: str, files: set[str]) -> bool:
    _ensure_commit(repo, old)
    _ensure_commit(repo, new)
    for path in files:
        old_status, old_entry = _git(repo, "ls-tree", old, "--", path)
        new_status, new_entry = _git(repo, "ls-tree", new, "--", path)
        if old_status or new_status:
            message = f"could not inspect {path} at both review heads"
            raise HoldCarryUnreadableError(message)
        if not old_entry or old_entry != new_entry:
            return False
    return True


def carry_unchanged_holds(*, slug: str, pr_id: int, head_sha: str) -> int:
    rows = (
        ReviewVerdict.objects.for_pr(slug, pr_id)
        .filter(verdict=ReviewVerdict.Verdict.HOLD)
        .order_by("-recorded_at", "-pk")
    )
    latest_by_reviewer = {row.reviewer_identity_normalized: row for row in reversed(list(rows))}
    if not latest_by_reviewer:
        return 0
    at_head = set(
        ReviewVerdict.objects.for_pr(slug, pr_id)
        .filter(reviewed_sha=head_sha)
        .values_list("reviewer_identity_normalized", flat=True)
    )
    candidates = [
        row for reviewer, row in latest_by_reviewer.items() if reviewer not in at_head and row.reviewed_sha != head_sha
    ]
    eligible = [(row, row.structured_findings) for row in candidates]
    eligible = [(row, findings) for row, findings in eligible if findings and all(finding.file for finding in findings)]
    if not eligible:
        return 0
    repo = matching_clone(slug)
    if repo is None:
        message = f"no matching clone for {slug}#{pr_id} with a stale HOLD"
        raise HoldCarryUnreadableError(message)
    carried = 0
    for row, findings in eligible:
        if row.ticket is None:
            message = f"HOLD {row.pk} has no ticket for carry provenance"
            raise HoldCarryUnreadableError(message)
        if not _same_blobs(repo, row.reviewed_sha, head_sha, {finding.file for finding in findings}):
            continue
        with transaction.atomic():
            row.carry_forward(reviewed_sha=head_sha)
            row.ticket.merge_extra(
                merge_into_dicts={
                    "hold_carry_forward": {
                        f"{slug.casefold()}#{pr_id}:{head_sha}:{row.reviewer_identity_normalized}": row.reviewed_sha
                    }
                }
            )
        carried += 1
    return carried
