"""GitHub PR read helpers — CI status rollup, review threads, approval snapshot.

The reads :class:`~teatree.backends.github.client.GitHubCodeHost` performs to
turn a PR into the vocabulary the loop scanners speak: aggregate a
``statusCheckRollup`` into one my_prs word (:func:`rollup_state`), enrich a
search hit with its head SHA + CI state (:func:`enrich_pr_pipeline`), count a
PR's unresolved review threads (:func:`count_unresolved_review_threads`), and
map GitHub's aggregate ``reviewDecision`` to an :class:`ApprovalState`
(:func:`approval_state`). Split out of ``client.py`` so the host stays focused on
the cross-host Protocol surface — the same shape as the sibling ``api`` /
``claims`` / ``payloads`` modules and GitLab's ``pr_reads``.
"""

import json
import logging
import re
from typing import cast
from urllib.parse import urlparse

from teatree.backends.github.api import _FORGE_READ_TIMEOUT_SECONDS, _gh_api_get, _run_gh
from teatree.backends.github.payloads import _GitHubPullRequestSummary, _GitHubUser, pr_open_state_from_payload
from teatree.backends.types import dig
from teatree.core.backend_protocols import ApprovalState, PrOpenState
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

ISSUE_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")
PR_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls?/(?P<number>\d+)/?$")

# One bounded read of a PR's review threads — GitHub DOES enforce conversation
# resolution as a merge gate (unlike the stale docstring's "GitHub lacks it"),
# so the unresolved-thread count must be surfaced, not hard-coded to zero.
_REVIEW_THREADS_QUERY = """\
query {{
    repository(owner: "{owner}", name: "{repo}") {{
        pullRequest(number: {number}) {{
            reviewThreads(first: 100) {{
                nodes {{ isResolved }}
            }}
        }}
    }}
}}"""


# CheckRun conclusions / StatusContext states that count as a hard failure — the
# my_pr.failed auto-debug lane must fire on these. Everything COMPLETED-and-not-here
# (SUCCESS / NEUTRAL / SKIPPED) is treated as passing.
_ROLLUP_FAIL_CONCLUSIONS = frozenset(
    {"FAILURE", "CANCELLED", "TIMED_OUT", "ACTION_REQUIRED", "STARTUP_FAILURE", "STALE"}
)
_ROLLUP_FAIL_STATES = frozenset({"FAILURE", "ERROR"})
_ROLLUP_PENDING_STATES = frozenset({"PENDING", "EXPECTED"})


def rollup_state(rollup: object) -> str:
    """Aggregate a GitHub ``statusCheckRollup`` list into one my_prs status word.

    ``gh pr view --json statusCheckRollup`` returns a list of ``CheckRun`` and
    ``StatusContext`` nodes, not a single verdict. This collapses them to the
    vocabulary :func:`teatree.loop.scanners.my_prs._pipeline_status` speaks:
    ``"failure"`` when any required check failed (→ the my_pr.failed lane fires),
    ``"pending"`` when any check is still running, ``"success"`` when every check
    passed, and ``""`` for a PR with no checks at all (a no-CI repo — never
    action-needed). A failing check dominates a pending one dominates success.
    """
    if not isinstance(rollup, list) or not rollup:
        return ""
    any_pending = False
    for node in rollup:
        if not isinstance(node, dict):
            continue
        entry = cast("RawAPIDict", node)
        status = str(entry.get("status") or "").upper()
        conclusion = str(entry.get("conclusion") or "").upper()
        state = str(entry.get("state") or "").upper()
        if conclusion in _ROLLUP_FAIL_CONCLUSIONS or state in _ROLLUP_FAIL_STATES:
            return "failure"
        if state in _ROLLUP_PENDING_STATES or (status and status != "COMPLETED"):
            any_pending = True
    return "pending" if any_pending else "success"


def is_not_found(exc: CommandFailedError) -> bool:
    """Whether a failed ``gh api`` call was a genuine HTTP 404.

    ``gh api`` exits non-zero for EVERY HTTP error (404, 401, 403, 5xx alike)
    with returncode 1, so the only reliable signal for a permanent "no such
    resource" is the literal ``HTTP 404`` string in stderr — the same probe
    :meth:`GitHubCodeHost.get_issue` uses. Everything else is an indeterminate
    failure the caller must NOT swallow as an empty result.
    """
    return "HTTP 404" in exc.stderr


def issue_repo_short(url: str) -> str:
    """The repo short-name from a GitHub issue/PR URL, or ``""`` if unparsable.

    The board item's URL (e.g. ``https://github.com/souliane/teatree/issues/4``)
    is the authoritative source of the issue's repo. The project *owner* is NOT
    a reliable repo (a Projects v2 board spans repos), so scoping a ticket by
    the owner mis-classifies UI-visibility for the DoD gate (#1426).
    """
    path = urlparse(url).path
    match = ISSUE_URL_RE.match(path) or PR_URL_RE.match(path)
    return match.group("repo") if match else ""


def enrich_pr_pipeline(hit: RawAPIDict, *, token: str) -> RawAPIDict:
    """Fold a PR's head SHA and aggregate CI state into a search hit.

    Returns *hit* unchanged when the slug/number cannot be parsed or the
    ``gh pr view`` read fails — the caller keeps the raw hit so the downstream
    scanner surfaces the enrichment gap rather than treating an unread PR as
    "no CI".
    """
    html_url = hit.get("html_url")
    match = PR_URL_RE.match(urlparse(html_url).path) if isinstance(html_url, str) else None
    if match is None:
        return hit
    slug = f"{match['owner']}/{match['repo']}"
    number = match["number"]
    try:
        result = _run_gh(
            "gh",
            "pr",
            "view",
            number,
            "--repo",
            slug,
            "--json",
            "headRefOid,statusCheckRollup,mergeable,mergeStateStatus",
            token=token,
            timeout=_FORGE_READ_TIMEOUT_SECONDS,
        )
    except CommandFailedError:
        warn_throttled(
            logger,
            f"github-pr-enrich-failed:{slug}",
            "could not enrich PR %s#%s pipeline state — my_pr.failed lane runs blind for it",
            slug,
            number,
        )
        return hit
    try:
        detail = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return hit
    if not isinstance(detail, dict):
        return hit
    enriched: RawAPIDict = dict(hit)
    head_oid = detail.get("headRefOid")
    if isinstance(head_oid, str) and head_oid:
        enriched["sha"] = head_oid
    # Present the aggregate CI verdict under the key the scanner already reads.
    enriched["status_check_rollup"] = {"state": rollup_state(detail.get("statusCheckRollup"))}
    # Kept for `raw`-payload consumers; NOT fed into ``mergeable_state`` — the
    # scanner treats that as a pipeline word and "clean"/"blocked" would misfire.
    enriched["mergeable"] = detail.get("mergeable")
    enriched["merge_state_status"] = detail.get("mergeStateStatus")
    return enriched


def count_unresolved_review_threads(*, repo: str, pr_iid: int, token: str) -> int | None:
    """Count the PR's UNRESOLVED review threads via one bounded GraphQL read.

    Returns the number of ``reviewThreads`` whose ``isResolved`` is ``false``,
    or ``None`` when the read could not be completed — a malformed ``repo``
    slug, a non-zero ``gh`` exit (auth/network/ratelimit), an unparsable body,
    or an unexpected shape. ``None`` lets :func:`approval_state` fail closed
    rather than report a fabricated zero unresolved threads.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None
    query = _REVIEW_THREADS_QUERY.format(owner=owner, repo=name, number=pr_iid)
    try:
        result = _run_gh(
            "gh",
            "api",
            "graphql",
            "-f",
            f"query={query}",
            token=token,
            timeout=_FORGE_READ_TIMEOUT_SECONDS,
        )
    except CommandFailedError:
        return None
    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None
    nodes = dig(payload, "data", "repository", "pullRequest", "reviewThreads", "nodes")
    if not isinstance(nodes, list):
        return None
    return sum(1 for node in nodes if isinstance(node, dict) and cast("RawAPIDict", node).get("isResolved") is False)


def pr_open_state(*, pr_url: str, token: str) -> PrOpenState:
    """Return whether the PR at *pr_url* is genuinely open/merged/closed (#1074).

    Fetches the PR's real ``state``/``merged`` fields. ``state=="open"``
    → OPEN, ``merged is True`` → MERGED, ``state=="closed"`` without
    ``merged`` → CLOSED. Any exception (``gh api`` failure, auth error),
    unparsable URL, or non-dict / unrecognised payload → ``UNKNOWN`` so
    the orphan sweep fails open (never reaps on doubt). GitLab's
    implementation maps the same ambiguity to ``UNKNOWN`` identically.
    """
    match = PR_URL_RE.match(urlparse(pr_url).path)
    if match is None:
        return PrOpenState.UNKNOWN
    try:
        pr = _gh_api_get(
            f"repos/{match['owner']}/{match['repo']}/pulls/{match['number']}",
            token=token,
        )
    except Exception:  # noqa: BLE001 — fail open: any failure must NOT reap a live review.
        return PrOpenState.UNKNOWN
    return pr_open_state_from_payload(pr)


def pr_author(*, pr_url: str, token: str) -> str:
    """Return the PR author's GitHub login, or ``""`` when it can't be resolved.

    Fetches the PR payload and reads ``user.login``. Any exception
    (``gh api`` failure, auth error), unparsable URL, or non-dict /
    author-less payload returns ``""`` — the reaction scanners treat an
    unresolved author as "not provably self" and skip the reaction, so a
    transient lookup failure can never cause a reaction on the user's
    own MR.
    """
    match = PR_URL_RE.match(urlparse(pr_url).path)
    if match is None:
        return ""
    try:
        pr = _gh_api_get(
            f"repos/{match['owner']}/{match['repo']}/pulls/{match['number']}",
            token=token,
        )
    except Exception:  # noqa: BLE001 — fail safe: an unresolved author must skip the reaction.
        return ""
    if not isinstance(pr, dict):
        return ""
    user = cast("_GitHubPullRequestSummary", pr).get("user")
    if isinstance(user, dict):
        login = cast("_GitHubUser", user).get("login")
        if isinstance(login, str):
            return login
    return ""


def approval_state(*, repo: str, pr_iid: int, token: str) -> ApprovalState:
    """Return GitHub's aggregate review decision as an approval snapshot (#8).

    GitHub exposes no numeric "approvals remaining" counter; its aggregate
    ``reviewDecision`` (``gh pr view --json reviewDecision``) is the
    merge-authorising signal — ``APPROVED`` means every required review is
    satisfied. Maps to ``approvals_left=0`` on ``APPROVED`` and ``1``
    otherwise, so a not-yet-approved (or a payload with no decision) is
    never mis-read as merge-authorised.

    ``unresolved_resolvable`` is the count of the PR's UNRESOLVED review
    threads, read via one bounded ``reviewThreads(isResolved:false)`` GraphQL
    query (:func:`count_unresolved_review_threads`). GitHub DOES enforce
    conversation resolution as a merge gate ("Require conversation resolution
    before merging"), so a positive count must gate the M7 waiting lane just
    as GitLab's does — the old hard-coded ``0`` let teatree loop trying to
    merge a PR the forge was refusing over an open conversation. When the
    thread read cannot be completed the count fails CLOSED to ``1`` so an
    indeterminate conversation state never authorises the merge.
    """
    result = _run_gh(
        "gh",
        "pr",
        "view",
        str(pr_iid),
        "--repo",
        repo,
        "--json",
        "reviewDecision",
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    decision = str(data.get("reviewDecision") or "").upper() if isinstance(data, dict) else ""
    unresolved = count_unresolved_review_threads(repo=repo, pr_iid=pr_iid, token=token)
    if unresolved is None:
        warn_throttled(
            logger,
            f"github-review-threads-unreadable:{repo}#{pr_iid}",
            "GitHub review-thread read failed for %s#%s — failing closed (unresolved_resolvable=1)",
            repo,
            pr_iid,
        )
    return ApprovalState(
        approvals_left=0 if decision == "APPROVED" else 1,
        approved_by=[],
        unresolved_resolvable=1 if unresolved is None else unresolved,
    )
