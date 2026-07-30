"""GitHub half of the ``t3 review run`` review-shape audit (#1206).

Reads go through :class:`~teatree.backends.github.client.GitHubCodeHost`, the
maintained ``gh``-backed host the scanners already use, so there is no second
forge client to keep in sync. The result is the SAME
:class:`~teatree.cli.review.run.ReviewRunResult` the GitLab path builds — same
keys, same complexity classifier, same findings catalog — so a reviewer
sub-agent parses one contract regardless of which forge the URL names.
"""

import os
from typing import cast

from teatree.cli.review.run import (
    JSONObject,
    ReviewRunResult,
    _classify_complexity,
    _DiffStats,
    _gather_findings,
    _ReviewRunAPIError,
    _ReviewState,
)
from teatree.core.backend_protocols import PrOpenState, ReviewState
from teatree.url_classify import repo_and_iid
from teatree.utils.run import CommandFailedError


def diff_stats_from_files(files: list[JSONObject]) -> _DiffStats:
    """Aggregate GitHub's ``pulls/{n}/files`` response into a :class:`_DiffStats`.

    GitHub reports per-file ``additions``/``deletions`` directly, so the counts
    come from the API rather than from re-parsing patch hunks — a file whose
    patch GitHub omits (too large, binary) still contributes its real counts.
    """
    additions = 0
    deletions = 0
    touched: list[str] = []
    for entry in files:
        raw_added = entry.get("additions")
        raw_removed = entry.get("deletions")
        additions += raw_added if isinstance(raw_added, int) else 0
        deletions += raw_removed if isinstance(raw_removed, int) else 0
        filename = entry.get("filename")
        if isinstance(filename, str) and filename:
            touched.append(filename)
    return _DiffStats(files=len(files), additions=additions, deletions=deletions, touched=tuple(touched))


def review_state_from_reviews(reviews: list[JSONObject], *, unresolved: int) -> _ReviewState:
    """Aggregate GitHub's submitted reviews into the counts GitLab reports.

    ``draft_notes`` counts ``PENDING`` reviews — GitHub returns a pending review
    only to the account that owns it, which is exactly the GitLab draft-note
    semantic. Approvals are counted per distinct login through
    :func:`latest_review_state_from_reviews`, so a login that approved and later
    requested changes is not still counted as an approver.
    """
    from teatree.backends.github.payloads import (  # noqa: PLC0415 — deferred: keeps CLI startup light
        latest_review_state_from_reviews,
    )

    draft_notes = 0
    logins: list[str] = []
    for entry in reviews:
        if str(entry.get("state") or "").upper() == "PENDING":
            draft_notes += 1
        user = entry.get("user")
        login = cast("JSONObject", user).get("login") if isinstance(user, dict) else None
        if isinstance(login, str) and login and login not in logins:
            logins.append(login)
    approvers = tuple(
        login for login in logins if latest_review_state_from_reviews(reviews, login) is ReviewState.APPROVED
    )
    return _ReviewState(
        open_discussions=unresolved,
        draft_notes=draft_notes,
        approvals=len(approvers),
        approved_by=approvers,
    )


def skip_verdict_for_open_state(open_state: PrOpenState) -> str:
    """Return ``skipped_merged`` / ``skipped_closed`` for a dead PR, else ``""`` (#2081).

    A merged or closed PR can never take a review note, so the audit reports a
    skip verdict rather than driving a doomed review.
    """
    if open_state is PrOpenState.MERGED:
        return "skipped_merged"
    if open_state is PrOpenState.CLOSED:
        return "skipped_closed"
    return ""


def audit_github_pr(url: str) -> ReviewRunResult:
    """Fetch metadata for a GitHub PR and build the audit result.

    An ``UNKNOWN`` live state is treated as a FAILED read, not as "open": the
    host maps auth/network errors to ``UNKNOWN`` because the orphan sweep must
    never reap on doubt, but an audit that answered ``ready_to_review`` off that
    same value would be a fabricated clean bill of health on a PR it never read.
    """
    from teatree.backends.github.client import GitHubCodeHost  # noqa: PLC0415 — deferred: keeps CLI startup light
    from teatree.core.models.live_post_approval import canonical_mr_scope  # noqa: PLC0415 — deferred: ORM/app-registry

    parsed = repo_and_iid(url)
    if parsed is None:
        msg = "bad_url"
        raise ValueError(msg)
    repo, pr_iid = parsed
    host = GitHubCodeHost(token=os.environ.get("TEATREE_GH_TOKEN", ""))

    try:
        open_state = host.get_pr_open_state(pr_url=url)
        if open_state is PrOpenState.UNKNOWN:
            msg = f"could not read the live state of {repo}#{pr_iid} — token missing or PR inaccessible"
            raise _ReviewRunAPIError(msg)
        diff = diff_stats_from_files(host.get_pr_diff(repo=repo, pr_iid=pr_iid))
        approvals = host.get_mr_approvals(repo=repo, pr_iid=pr_iid)
        state = review_state_from_reviews(
            host.list_pr_reviews(repo=repo, pr_iid=pr_iid),
            unresolved=approvals["unresolved_resolvable"],
        )
    except CommandFailedError as exc:
        msg = f"GitHub backend refused the audit for {repo}#{pr_iid}: {exc}"
        raise _ReviewRunAPIError(msg) from exc

    complexity = _classify_complexity(files=diff.files, additions=diff.additions, deletions=diff.deletions)
    findings = _gather_findings(complexity=complexity, files=diff.files, touched_paths=diff.touched)
    skip_verdict = skip_verdict_for_open_state(open_state)
    verdict = skip_verdict or ("needs_attention" if findings or state.open_discussions else "ready_to_review")
    return ReviewRunResult(
        mr=canonical_mr_scope(url),
        forge="github",
        url=url,
        diff=diff,
        complexity=complexity,
        state=state,
        findings=findings,
        verdict=verdict,
    )
