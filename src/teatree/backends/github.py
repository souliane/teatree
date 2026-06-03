"""GitHub backend — code host via the ``gh`` CLI.

The Projects v2 board reads (``ProjectItem``, ``fetch_project_items``) live
in :mod:`teatree.backends.github_projects` and are re-exported here so the
historical ``from teatree.backends.github import …`` import sites are unchanged.
"""

import json
import os
import re
from typing import TypedDict, cast
from urllib.parse import quote_plus, urlparse

from teatree.backends.github_claims import record_github_note_claim as _record_github_note_claim
from teatree.backends.github_projects import ProjectItem, fetch_project_items
from teatree.backends.protocols import ApprovalState, PrOpenState, PullRequestSpec, ReviewState
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError, CompletedProcess, run_checked

__all__ = ["GitHubCodeHost", "ProjectItem", "fetch_project_items", "issue_repo_short"]

_ISSUE_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")
_PR_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls?/(?P<number>\d+)/?$")


def issue_repo_short(url: str) -> str:
    """The repo short-name from a GitHub issue/PR URL, or ``""`` if unparsable.

    The board item's URL (e.g. ``https://github.com/souliane/teatree/issues/4``)
    is the authoritative source of the issue's repo. The project *owner* is NOT
    a reliable repo (a Projects v2 board spans repos), so scoping a ticket by
    the owner mis-classifies UI-visibility for the DoD gate (#1426).
    """
    path = urlparse(url).path
    match = _ISSUE_URL_RE.match(path) or _PR_URL_RE.match(path)
    return match.group("repo") if match else ""


_GH_REVIEW_STATE_MAP: dict[str, ReviewState] = {
    "APPROVED": ReviewState.APPROVED,
    "CHANGES_REQUESTED": ReviewState.CHANGES_REQUESTED,
    "DISMISSED": ReviewState.DISMISSED,
    "PENDING": ReviewState.PENDING,
}


class _GitHubUser(TypedDict, total=False):
    """Subset of the GitHub ``/user`` response that teatree reads."""

    login: str


class _GitHubReviewEntry(TypedDict, total=False):
    """Subset of the GitHub PR-review response that teatree reads."""

    user: _GitHubUser
    state: str


class _GitHubPullRequestSummary(TypedDict, total=False):
    """Subset of the GitHub PR response read for the review state lookup."""

    requested_reviewers: list[_GitHubUser]
    state: str
    merged: bool
    user: _GitHubUser


def _run_gh(*args: str, token: str = "") -> CompletedProcess[str]:
    """Run a ``gh`` CLI command and return the result.

    Auth via ``GH_TOKEN`` env, never ``--header``: only ``gh api`` accepts
    ``--header``; injecting it into ``gh pr create`` fails with
    ``unknown flag --header``.
    """
    env = {**os.environ, "GH_TOKEN": token} if token else None
    return run_checked(list(args), env=env)


def _latest_review_state_from_reviews(reviews: object, reviewer: str) -> ReviewState | None:
    """Return the most recent terminal review state by *reviewer*, or ``None``."""
    if not isinstance(reviews, list):
        return None
    for raw_entry in reversed(reviews):
        if not isinstance(raw_entry, dict):
            continue
        entry = cast("_GitHubReviewEntry", raw_entry)
        user = entry.get("user")
        login = user.get("login") if isinstance(user, dict) else None
        if login != reviewer:
            continue
        state_str = entry.get("state")
        if not isinstance(state_str, str):
            continue
        mapped = _GH_REVIEW_STATE_MAP.get(state_str.upper())
        if mapped is not None:
            return mapped
    return None


def _pr_open_state_from_payload(pr: object) -> PrOpenState:
    """Map a GitHub PR payload to a :class:`PrOpenState` (#1074).

    ``state=="open"`` → OPEN; ``merged is True`` → MERGED; ``state=="closed"``
    without ``merged`` → CLOSED. Any non-dict or unrecognised shape →
    ``UNKNOWN`` so the orphan sweep fails open.
    """
    if not isinstance(pr, dict):
        return PrOpenState.UNKNOWN
    summary = cast("_GitHubPullRequestSummary", pr)
    if summary.get("state") == "open":
        return PrOpenState.OPEN
    if summary.get("merged") is True:
        return PrOpenState.MERGED
    if summary.get("state") == "closed":
        return PrOpenState.CLOSED
    return PrOpenState.UNKNOWN


def _reviewer_is_requested(pr: object, reviewer: str) -> bool:
    """Return True iff *reviewer* appears on the PR's ``requested_reviewers``."""
    if not isinstance(pr, dict):
        return False
    requested = cast("_GitHubPullRequestSummary", pr).get("requested_reviewers")
    if not isinstance(requested, list):
        return False
    return any(isinstance(entry, dict) and entry.get("login") == reviewer for entry in requested)


def _gh_api_get(endpoint: str, *, token: str = "") -> object:
    """Call ``gh api`` (GET) and return parsed JSON."""
    result = _run_gh(
        "gh",
        "api",
        endpoint,
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
    )
    return json.loads(result.stdout)


def _gh_api_get_paginated(endpoint: str, *, token: str = "") -> list[RawAPIDict]:
    """Fetch EVERY page of a list endpoint and return one flat list.

    A plain ``gh api`` GET returns only the first page — GitHub's default
    page size silently caps the result, so a comment older than the most
    recent page goes unseen and the find-then-update dedup re-posts a
    duplicate. ``--paginate`` follows the ``Link`` header to the last page;
    ``--slurp`` wraps each page's JSON array into one outer array
    (``[[page1…], [page2…]]``), which this flattens into a single list.

    Non-list pages (a single-object body, an error payload) are skipped so
    a malformed page can never raise. Returns ``[]`` when the outer payload
    is not an array.
    """
    result = _run_gh(
        "gh",
        "api",
        endpoint,
        "--paginate",
        "--slurp",
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list):
        return []
    flattened: list[RawAPIDict] = []
    for page in pages:
        if isinstance(page, list):
            flattened.extend(cast("list[RawAPIDict]", page))
    return flattened


def _gh_api_search_paginated(endpoint: str, *, token: str = "") -> list[RawAPIDict]:
    """Fetch every page of a GitHub search endpoint and return a flat item list.

    Search responses wrap results in ``{"items": [...], "total_count": N}``
    rather than a bare JSON array, so ``_gh_api_get_paginated`` (which expects
    bare arrays per page via ``--slurp``) cannot be used here.
    ``--paginate`` + ``--slurp`` emits each page as a search-object element;
    this pulls the ``items`` list from each page and flattens them.
    """
    result = _run_gh(
        "gh",
        "api",
        endpoint,
        "--paginate",
        "--slurp",
        "--header",
        "Accept: application/vnd.github+json",
        token=token,
    )
    pages = json.loads(result.stdout)
    if not isinstance(pages, list):
        return []
    items: list[RawAPIDict] = []
    for page in pages:
        if not isinstance(page, dict):
            continue
        page_items = cast("RawAPIDict", page).get("items")
        if isinstance(page_items, list):
            items.extend(cast("list[RawAPIDict]", page_items))
    return items


def _gh_api_post(endpoint: str, payload: dict[str, object], *, token: str = "") -> object:
    """Call ``gh api`` (POST) and return parsed JSON."""
    cmd = [
        "gh",
        "api",
        endpoint,
        "--method",
        "POST",
        "--header",
        "Accept: application/vnd.github+json",
        "--input",
        "-",
    ]
    if token:
        cmd.extend(["--header", f"Authorization: Bearer {token}"])
    result = run_checked(cmd, stdin_text=json.dumps(payload))
    return json.loads(result.stdout)


def _gh_api_patch(endpoint: str, payload: dict[str, object], *, token: str = "") -> object:
    """Call ``gh api`` (PATCH) and return parsed JSON."""
    cmd = [
        "gh",
        "api",
        endpoint,
        "--method",
        "PATCH",
        "--header",
        "Accept: application/vnd.github+json",
        "--input",
        "-",
    ]
    if token:
        cmd.extend(["--header", f"Authorization: Bearer {token}"])
    result = run_checked(cmd, stdin_text=json.dumps(payload))
    return json.loads(result.stdout)


class GitHubCodeHost:
    """CodeHost implementation backed by the ``gh`` CLI."""

    def __init__(self, *, token: str = "") -> None:
        self._token = token

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict:
        repo_slug = git.remote_slug(repo=spec.repo)
        cmd = [
            "gh",
            "pr",
            "create",
            "--repo",
            repo_slug,
            "--head",
            spec.branch,
            "--title",
            spec.title,
            "--body",
            spec.description,
        ]
        if spec.target_branch:
            cmd.extend(["--base", spec.target_branch])
        if spec.labels:
            cmd.extend(["--label", ",".join(spec.labels)])
        if spec.assignee:
            cmd.extend(["--assignee", spec.assignee])
        if spec.draft:
            cmd.append("--draft")

        result = _run_gh(*cmd, token=self._token)
        # #1222 / #1226: align with the cross-host canonical key (``web_url``)
        # that ``ShipExecutor`` reads — returning ``url`` silently produced
        # empty PR rows because the consumer never looked at that field. The
        # producer also enforces the verify-by-re-read invariant: an empty /
        # non-URL stdout (e.g. the ``no commits between`` pre-push race that
        # exits 0) is rejected so ``ok=True`` never escapes with no PR.
        url = result.stdout.strip()
        if not url.startswith(("http://", "https://")):
            raise CommandFailedError(
                cmd,
                result.returncode,
                result.stdout,
                f"gh pr create produced no PR URL (stdout={url!r})",
            )
        return {"web_url": url}

    def current_user(self) -> str:
        """Return the authenticated GitHub login (e.g. ``souliane``)."""
        data = _gh_api_get("user", token=self._token)
        if not isinstance(data, dict):
            return ""
        user = cast("_GitHubUser", data)
        return user.get("login", "")

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        terms = [f"is:pr is:open author:{author}"]
        if updated_after:
            terms.append(f"updated:>={updated_after}")
        query = quote_plus(" ".join(terms))
        return _gh_api_search_paginated(f"search/issues?q={query}&per_page=100", token=self._token)

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]:
        terms = [f"is:pr is:open review-requested:{reviewer}"]
        if updated_after:
            terms.append(f"updated:>={updated_after}")
        query = quote_plus(" ".join(terms))
        return _gh_api_search_paginated(f"search/issues?q={query}&per_page=100", token=self._token)

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        data = _gh_api_post(
            f"repos/{repo}/issues/{pr_iid}/comments",
            {"body": body},
            token=self._token,
        )
        result: RawAPIDict = cast("RawAPIDict", data) if isinstance(data, dict) else {}
        comment_id = result.get("id")
        if isinstance(comment_id, int):
            _record_github_note_claim(
                repo=repo,
                target_number=pr_iid,
                comment_id=comment_id,
                body=body,
                target_url=str(result.get("html_url") or ""),
            )
        return result

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        _ = pr_iid  # GitHub comment IDs are globally unique
        data = _gh_api_patch(
            f"repos/{repo}/issues/comments/{comment_id}",
            {"body": body},
            token=self._token,
        )
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        # Paginate: a busy PR has >30 comments (GitHub's default page size),
        # and a non-paginated GET would hide the ``## Test Plan`` note the
        # evidence-poster looks up to UPDATE — re-posting a duplicate instead.
        return _gh_api_get_paginated(f"repos/{repo}/issues/{pr_iid}/comments?per_page=100", token=self._token)

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        query = quote_plus(f"is:issue is:open assignee:{assignee}")
        return _gh_api_search_paginated(f"search/issues?q={query}&per_page=100", token=self._token)

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> RawAPIDict:
        """Open a GitHub issue on ``owner/repo`` and return the created payload.

        ``repo`` is the ``owner/repo`` slug. The returned dict carries the
        forge's ``html_url`` (the clickable issue link) and ``number``.
        """
        payload: RawAPIDict = {"title": title, "body": body}
        if labels:
            payload["labels"] = labels
        data = _gh_api_post(f"repos/{repo}/issues", payload, token=self._token)
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    def create_sub_issue(
        self,
        *,
        parent_url: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        child_type: str = "Task",
    ) -> RawAPIDict:
        _ = (title, body, labels)
        return {
            "error": (
                f"GitHub child work items are not supported (token={'set' if self._token else 'unset'}, "
                f"parent={parent_url}, type={child_type})"
            ),
        }

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        """Return open issues on ``owner/repo`` matching the free-text *query*.

        Uses GitHub's issue search so a dedup caller can find an
        already-filed enforcement issue by a fingerprint marker embedded in
        its body, without paging the whole issue list.
        """
        terms = quote_plus(f"repo:{repo} is:issue is:open {query}")
        return _gh_api_search_paginated(f"search/issues?q={terms}&per_page=100", token=self._token)

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        msg = f"File upload to {repo} not supported (token={'set' if self._token else 'unset'}, file={filepath})"
        raise NotImplementedError(msg)

    def get_issue(self, issue_url: str) -> RawAPIDict:
        """Fetch a GitHub issue from its full URL.

        Supports ``https://github.com/<owner>/<repo>/issues/<number>``.
        Returns ``{"error": ...}`` when the URL is not a recognised GitHub
        issue URL.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_URL_RE.match(path)
        if match is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}

        endpoint = f"repos/{match['owner']}/{match['repo']}/issues/{match['number']}"
        data = _gh_api_get(endpoint, token=self._token)
        return cast("RawAPIDict", data) if isinstance(data, dict) else {"error": f"Issue not found: {issue_url}"}

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict:
        """Post a comment to a GitHub issue.

        Supports ``https://github.com/<owner>/<repo>/issues/<number>``.
        Returns ``{"error": ...}`` when the URL is not a recognised GitHub
        issue URL.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_URL_RE.match(path)
        if match is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}

        repo = f"{match['owner']}/{match['repo']}"
        target_number = int(match["number"])
        data = _gh_api_post(
            f"repos/{repo}/issues/{target_number}/comments",
            {"body": body},
            token=self._token,
        )
        result: RawAPIDict = cast("RawAPIDict", data) if isinstance(data, dict) else {}
        comment_id = result.get("id")
        if isinstance(comment_id, int):
            _record_github_note_claim(
                repo=repo,
                target_number=target_number,
                comment_id=comment_id,
                body=body,
                target_url=str(result.get("html_url") or ""),
            )
        return result

    def list_issue_comments(self, *, issue_url: str) -> list[RawAPIDict]:
        """List the comments on a GitHub issue.

        Supports ``https://github.com/<owner>/<repo>/issues/<number>``.
        Returns an empty list when the URL is not a recognised GitHub issue
        URL — the caller treats "no comments" and "unresolvable" identically.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_URL_RE.match(path)
        if match is None:
            return []

        repo = f"{match['owner']}/{match['repo']}"
        return _gh_api_get_paginated(f"repos/{repo}/issues/{match['number']}/comments?per_page=100", token=self._token)

    def update_issue_comment(self, *, issue_url: str, comment_id: int, body: str) -> RawAPIDict:
        """Edit an existing GitHub issue comment in place.

        GitHub issue-comment ids are globally unique within a repo, edited
        via ``/repos/{repo}/issues/comments/{id}`` (the issue number is not
        part of the path). Returns ``{"error": ...}`` when the URL is not a
        recognised GitHub issue URL.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_URL_RE.match(path)
        if match is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}

        repo = f"{match['owner']}/{match['repo']}"
        data = _gh_api_patch(
            f"repos/{repo}/issues/comments/{comment_id}",
            {"body": body},
            token=self._token,
        )
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    @staticmethod
    def get_mr_approvals(*, repo: str, pr_iid: int) -> ApprovalState:
        """Out of scope for #936 — GitLab-only approval polling for now.

        Raising rather than returning a vacuous zero state forces the caller
        (``GitLabApprovalsScanner``) to skip GitHub PRs silently rather than
        silently treating them as "approved with zero approvals_left".
        """
        _ = (repo, pr_iid)
        msg = "GitHub approval state is not yet wired up (#936 is GitLab-scoped)"
        raise NotImplementedError(msg)

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        """Return *reviewer*'s current review state on the PR at *pr_url*.

        Walks the PR's review timeline (most recent first) and returns the
        latest non-comment state the reviewer has submitted: ``APPROVED``,
        ``CHANGES_REQUESTED``, ``DISMISSED``, or ``PENDING``. When the
        reviewer has no terminal state but is still listed as a requested
        reviewer (e.g. a re-request after a dismissal), the result is
        ``PENDING``. Unparsable URLs and unknown reviewers yield ``NONE``.
        """
        path = urlparse(pr_url).path
        match = _PR_URL_RE.match(path)
        if match is None or not reviewer:
            return ReviewState.NONE

        base = f"repos/{match['owner']}/{match['repo']}/pulls/{match['number']}"
        reviews = _gh_api_get_paginated(f"{base}/reviews?per_page=100", token=self._token)
        terminal = _latest_review_state_from_reviews(reviews, reviewer)
        if terminal is not None:
            return terminal

        pr = _gh_api_get(base, token=self._token)
        if _reviewer_is_requested(pr, reviewer):
            return ReviewState.PENDING
        return ReviewState.NONE

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        """Return whether the PR at *pr_url* is genuinely open/merged/closed (#1074).

        Fetches the PR's real ``state``/``merged`` fields. ``state=="open"``
        → OPEN, ``merged is True`` → MERGED, ``state=="closed"`` without
        ``merged`` → CLOSED. Any exception (``gh api`` failure, auth error),
        unparsable URL, or non-dict / unrecognised payload → ``UNKNOWN`` so
        the orphan sweep fails open (never reaps on doubt). GitLab's
        implementation maps the same ambiguity to ``UNKNOWN`` identically.
        """
        match = _PR_URL_RE.match(urlparse(pr_url).path)
        if match is None:
            return PrOpenState.UNKNOWN
        try:
            pr = _gh_api_get(
                f"repos/{match['owner']}/{match['repo']}/pulls/{match['number']}",
                token=self._token,
            )
        except Exception:  # noqa: BLE001 — fail open: any failure must NOT reap a live review.
            return PrOpenState.UNKNOWN
        return _pr_open_state_from_payload(pr)

    def get_pr_author(self, *, pr_url: str) -> str:
        """Return the PR author's GitHub login, or ``""`` when it can't be resolved.

        Fetches the PR payload and reads ``user.login``. Any exception
        (``gh api`` failure, auth error), unparsable URL, or non-dict /
        author-less payload returns ``""`` — the reaction scanners treat an
        unresolved author as "not provably self" and skip the reaction, so a
        transient lookup failure can never cause a reaction on the user's
        own MR.
        """
        match = _PR_URL_RE.match(urlparse(pr_url).path)
        if match is None:
            return ""
        try:
            pr = _gh_api_get(
                f"repos/{match['owner']}/{match['repo']}/pulls/{match['number']}",
                token=self._token,
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
