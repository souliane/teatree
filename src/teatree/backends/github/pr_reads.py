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
from collections.abc import Sequence
from typing import TypedDict, cast
from urllib.parse import quote, urlparse

from teatree.backends.github.api import _FORGE_READ_TIMEOUT_SECONDS, _run_gh
from teatree.backends.github.api import gh_api_get as _gh_api_get
from teatree.backends.github.payloads import _GitHubPullRequestSummary, pr_open_state_from_payload
from teatree.backends.types import dig
from teatree.core.backend_protocols import ApprovalState, PrOpenState
from teatree.types import RawAPIDict
from teatree.utils.run import CommandFailedError
from teatree.utils.throttled_log import warn_throttled

logger = logging.getLogger(__name__)

ISSUE_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")
PR_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls?/(?P<number>\d+)/?$")

# A PR's review threads — GitHub DOES enforce conversation resolution as a merge
# gate (unlike the stale docstring's "GitHub lacks it"), so the unresolved-thread
# count must be surfaced, not hard-coded to zero. The connection caps a page at
# 100 nodes, so a PR with more threads must be walked by cursor: reading only the
# first page on a 100+-thread PR reports the unresolved thread past it as absent,
# which is the hard-coded zero again with extra steps.
_REVIEW_THREADS_QUERY = """\
query {{
    repository(owner: "{owner}", name: "{repo}") {{
        pullRequest(number: {number}) {{
            reviewThreads(first: 100{after}) {{
                pageInfo {{ hasNextPage endCursor }}
                nodes {{ id isResolved comments(first: 1) {{ nodes {{ body url path line }} }} }}
            }}
        }}
    }}
}}"""

# Pages of 100 threads. A PR beyond this is not read short and reported low — the
# walk returns None so `approval_state` fails closed exactly as it does for an
# unreadable page.
_REVIEW_THREADS_MAX_PAGES = 20

_ENGAGED_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "COMMENTED", "DISMISSED"})
_STANDING_REVIEW_STATES = frozenset({"APPROVED", "CHANGES_REQUESTED", "DISMISSED"})


class _BranchOrPullItem(TypedDict, total=False):
    ref: str
    object: RawAPIDict
    merged_at: str | None
    head: RawAPIDict


def _strict_list_pages(endpoint: str, *, token: str) -> list[_BranchOrPullItem]:
    response = _run_gh(
        "gh",
        "api",
        endpoint,
        "--paginate",
        "--slurp",
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    pages = json.loads(response.stdout)
    if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
        msg = f"GitHub {endpoint} returned a non-list page"
        raise TypeError(msg)
    items = [item for page in pages for item in page]
    if any(not isinstance(item, dict) for item in items):
        msg = f"GitHub {endpoint} returned a non-object item"
        raise TypeError(msg)
    return cast("list[_BranchOrPullItem]", items)


def _matching_merged_head(prs: list[_BranchOrPullItem], *, sha: str, repo: str) -> bool:
    matched = False
    for pr in prs:
        merged_at = pr.get("merged_at")
        head = pr.get("head")
        head_sha = head.get("sha") if isinstance(head, dict) else None
        valid_merged_at = merged_at is None or (isinstance(merged_at, str) and bool(merged_at))
        valid_head = isinstance(head_sha, str) and bool(head_sha)
        if "merged_at" not in pr or not valid_merged_at or not valid_head:
            msg = f"GitHub {repo} returned an invalid closed pull request"
            raise TypeError(msg)
        matched |= merged_at is not None and head_sha == sha
    return matched


class BranchReads:
    @staticmethod
    def list_unshipped_branches(*, repo: str, prefix: str, token: str) -> list[str]:
        metadata = _gh_api_get(f"repos/{repo}", token=token)
        default = metadata.get("default_branch") if isinstance(metadata, dict) else None
        if not isinstance(default, str) or not default:
            msg = f"GitHub {repo} has no readable default branch"
            raise TypeError(msg)
        endpoint = f"repos/{repo}/git/matching-refs/heads/{quote(prefix, safe='')}?per_page=100"
        refs = _strict_list_pages(endpoint, token=token)
        owner = repo.split("/", 1)[0]
        unshipped = []
        for ref in refs:
            full_name = ref.get("ref")
            item = ref.get("object")
            sha = item.get("sha") if isinstance(item, dict) else None
            if not isinstance(full_name, str) or not full_name.startswith("refs/heads/") or not isinstance(sha, str):
                msg = f"GitHub {repo} returned an invalid branch ref"
                raise TypeError(msg)
            name = full_name.removeprefix("refs/heads/")
            if not name.startswith(prefix):
                continue
            comparison = _gh_api_get(f"repos/{repo}/compare/{quote(default, safe='')}...{sha}", token=token)
            ahead = comparison.get("ahead_by") if isinstance(comparison, dict) else None
            if not isinstance(ahead, int):
                msg = f"GitHub {repo} returned an invalid branch comparison"
                raise TypeError(msg)
            if ahead == 0:
                continue
            prs = _strict_list_pages(
                f"repos/{repo}/pulls?state=closed&head={quote(f'{owner}:{name}', safe='')}&per_page=100",
                token=token,
            )
            if not _matching_merged_head(prs, sha=sha, repo=repo):
                unshipped.append(name)
        return unshipped


def approval_count(reviews: object) -> int | None:
    if not isinstance(reviews, list):
        return None
    standing: dict[str, str] = {}
    for review in reviews:
        if not isinstance(review, dict):
            return None
        author = review.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        if not isinstance(login, str) or not login:
            return None
        state = str(review.get("state") or "").upper()
        if state in _STANDING_REVIEW_STATES:
            standing[login] = state
    return sum(state == "APPROVED" for state in standing.values())


def _required_approval_count(*, repo: str, base_ref: str, token: str) -> int | None:
    if not base_ref:
        return None
    try:
        rules = _gh_api_get(f"repos/{repo}/rules/branches/{quote(base_ref, safe='')}", token=token)
    except CommandFailedError:
        warn_throttled(
            logger,
            f"github-review-rules-unreadable:{repo}:{base_ref}",
            "GitHub review rules unreadable for %s %s",
            repo,
            base_ref,
        )
        return None
    if not isinstance(rules, list):
        return None
    counts = [
        rule.get("parameters", {}).get("required_approving_review_count")
        for rule in rules
        if isinstance(rule, dict) and rule.get("type") == "pull_request" and isinstance(rule.get("parameters"), dict)
    ]
    return max(counts) if counts and all(type(count) is int and count >= 0 for count in counts) else None


class ReviewerReads:
    @staticmethod
    def requested(pr: object, *, repo: str, pr_iid: int) -> list[str]:
        reviewers = pr.get("requested_reviewers") if isinstance(pr, dict) else None
        if not isinstance(reviewers, list):
            msg = f"Could not read requested reviewers for {repo}#{pr_iid}"
            raise TypeError(msg)
        return [login for item in reviewers if isinstance(item, dict) if isinstance(login := item.get("login"), str)]

    @staticmethod
    def engaged(requested: list[str], reviews: list[RawAPIDict]) -> list[str]:
        names = list(dict.fromkeys(requested))
        seen = set(names)
        for review in reviews:
            if not isinstance(review, dict) or str(review.get("state") or "").upper() not in _ENGAGED_REVIEW_STATES:
                continue
            user = review.get("user")
            login = user.get("login") if isinstance(user, dict) else None
            if isinstance(login, str) and login and login not in seen:
                names.append(login)
                seen.add(login)
        return names


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
    vocabulary :func:`teatree.core.review.mr_ci_state.pipeline_status` speaks:
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
    "no CI". A ``gh`` read fails either with a non-zero exit
    (:class:`CommandFailedError`) or an absent binary
    (:class:`FileNotFoundError`); both degrade to the un-enriched hit so a
    missing ``gh`` can never crash the whole ``list_my_prs`` scan.
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
            "headRefOid,baseRefOid,headRefName,statusCheckRollup,mergeable,mergeStateStatus",
            token=token,
            timeout=_FORGE_READ_TIMEOUT_SECONDS,
        )
    except (CommandFailedError, FileNotFoundError):
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
    base_oid = detail.get("baseRefOid")
    if isinstance(base_oid, str) and base_oid:
        enriched["base_sha"] = base_oid
    enriched["source_branch"] = detail.get("headRefName")
    # Present the aggregate CI verdict under the key the scanner already reads.
    enriched["status_check_rollup"] = {"state": rollup_state(detail.get("statusCheckRollup"))}
    # Kept for `raw`-payload consumers; NOT fed into ``mergeable_state`` — the
    # scanner treats that as a pipeline word and "clean"/"blocked" would misfire.
    enriched["mergeable"] = detail.get("mergeable")
    enriched["merge_state_status"] = detail.get("mergeStateStatus")
    return enriched


def count_unresolved_review_threads(*, repo: str, pr_iid: int, token: str) -> int | None:
    """Count the PR's UNRESOLVED review threads, walking every page of the connection.

    Returns the number of ``reviewThreads`` whose ``isResolved`` is ``false``,
    or ``None`` when the read could not be completed — a malformed ``repo``
    slug, a non-zero ``gh`` exit (auth/network/ratelimit), an unparsable body,
    an unexpected shape, or a thread list longer than the page budget. ``None``
    lets :func:`approval_state` fail closed rather than report a count drawn
    from a truncated read, which under-reports and authorises a merge the forge
    then refuses over the open conversation.
    """
    owner, _, name = repo.partition("/")
    if not owner or not name:
        return None
    unresolved = 0
    after = ""
    for _ in range(_REVIEW_THREADS_MAX_PAGES):
        page = _review_threads_page(owner=owner, name=name, pr_iid=pr_iid, after=after, token=token)
        if page is None:
            return None
        nodes, end_cursor = page
        unresolved += sum(
            1 for node in nodes if isinstance(node, dict) and cast("RawAPIDict", node).get("isResolved") is False
        )
        if end_cursor is None:
            return unresolved
        after = f', after: "{end_cursor}"'
    return None


def list_review_threads(*, repo: str, pr_iid: int, token: str) -> list[RawAPIDict]:
    owner, _, name = repo.partition("/")
    if not owner or not name:
        message = "Cannot read review threads for invalid repo"
        raise RuntimeError(message)
    threads: list[RawAPIDict] = []
    after = ""
    for _ in range(_REVIEW_THREADS_MAX_PAGES):
        page = _review_threads_page(owner=owner, name=name, pr_iid=pr_iid, after=after, token=token)
        if page is None:
            message = "Cannot read review threads"
            raise RuntimeError(message)
        nodes, end_cursor = page
        for node in nodes:
            if not isinstance(node, dict):
                message = "Malformed review threads"
                raise TypeError(message)
            comments = dig(node, "comments", "nodes")
            if not isinstance(comments, list):
                message = "Malformed review threads"
                raise TypeError(message)
            threads.append({"id": node.get("id"), "isResolved": node.get("isResolved"), "notes": comments})
        if end_cursor is None:
            return threads
        after = f', after: "{end_cursor}"'
    message = "Review threads exceed page budget"
    raise RuntimeError(message)


def _review_threads_page(
    *, owner: str, name: str, pr_iid: int, after: str, token: str
) -> tuple[Sequence[object], str | None] | None:
    """One page of review threads as ``(nodes, next_cursor)``, or ``None`` when unreadable.

    ``next_cursor`` is ``None`` on the last page. A ``hasNextPage`` with no usable
    ``endCursor`` is UNREADABLE, not a last page: threads exist past this read and
    nothing can advance to them, so returning what was seen is the same partial
    count the walk replaced.
    """
    query = _REVIEW_THREADS_QUERY.format(owner=owner, repo=name, number=pr_iid, after=after)
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
    threads = dig(payload, "data", "repository", "pullRequest", "reviewThreads")
    nodes = dig(threads, "nodes")
    if not isinstance(nodes, list):
        return None
    if dig(threads, "pageInfo", "hasNextPage") is not True:
        return nodes, None
    end_cursor = dig(threads, "pageInfo", "endCursor")
    if not isinstance(end_cursor, str) or not end_cursor:
        return None
    return nodes, end_cursor


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


def open_pr_url_for_branch(*, repo: str, branch: str) -> str | None:
    """The OPEN PR whose head is *branch*, as the shared ``str | None`` tri-state.

    ``gh`` IS in the deploy image, so this arm keeps the CLI probe and its writer
    credential chain (``forge_cli_env``) rather than moving to HTTP — the reason the
    GitLab side had to move does not apply here. *repo* is the repo DIRECTORY, which
    is what ``gh`` resolves the slug from.

    The import is deferred so this module's import graph stays acyclic: ``core``
    reaches this host through the provider seam, and the probe reaches back.
    """
    from teatree.core.forge_pr_probe import probe_github_open_pr  # noqa: PLC0415 — deferred: acyclic graph

    return probe_github_open_pr(repo, branch).url_or_none_on_unknown()


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
        login = user.get("login")
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
        "reviewDecision,reviews,baseRefName",
        token=token,
        timeout=_FORGE_READ_TIMEOUT_SECONDS,
    )
    try:
        data = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        data = {}
    decision = str(data.get("reviewDecision") or "").upper() if isinstance(data, dict) else ""
    count = approval_count(data.get("reviews")) if isinstance(data, dict) else None
    base_ref = data.get("baseRefName") if isinstance(data, dict) else None
    required = (
        _required_approval_count(repo=repo, base_ref=base_ref, token=token) if isinstance(base_ref, str) else None
    )
    unresolved = count_unresolved_review_threads(repo=repo, pr_iid=pr_iid, token=token)
    if unresolved is None:
        warn_throttled(
            logger,
            f"github-review-threads-unreadable:{repo}#{pr_iid}",
            "GitHub review-thread read failed for %s#%s — failing closed (unresolved_resolvable=1)",
            repo,
            pr_iid,
        )
    state = ApprovalState(
        approvals_left=0 if decision == "APPROVED" else 1,
        approved_by=[],
        unresolved_resolvable=1 if unresolved is None else unresolved,
    )
    if count is not None:
        state["count"] = count
    if required is not None:
        state["required"] = required
    return state
