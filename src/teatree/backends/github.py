"""GitHub backend — code host and project board sync via ``gh`` CLI."""

import json
import os
import re
from dataclasses import dataclass
from typing import TypedDict, cast
from urllib.parse import quote_plus, urlparse

from teatree.backends.github_claims import record_github_note_claim as _record_github_note_claim
from teatree.backends.protocols import ApprovalState, PrOpenState, PullRequestSpec, ReviewState
from teatree.backends.types import dig
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError, CompletedProcess, run_checked

_ISSUE_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/issues/(?P<number>\d+)/?$")
_PR_URL_RE = re.compile(r"^/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls?/(?P<number>\d+)/?$")

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


@dataclass(frozen=True, slots=True)
class ProjectItem:
    """A single item from a GitHub Projects v2 board."""

    issue_number: int
    title: str
    url: str
    status: str
    position: int
    labels: list[str]
    updated_at: str = ""


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


def _gh_graphql(query: str, *, token: str = "") -> dict[str, object]:
    """Execute a GraphQL query via ``gh api graphql``."""
    cmd = ["gh", "api", "graphql", "-f", f"query={query}"]
    if token:
        cmd.extend(["--header", f"Authorization: Bearer {token}"])
    result = run_checked(cmd)
    return json.loads(result.stdout)


_PROJECT_ITEMS_QUERY = """\
{{
    user(login: "{owner}") {{
        projectV2(number: {project_number}) {{
            items(first: 100{after}) {{
                pageInfo {{ hasNextPage endCursor }}
                nodes {{
                    fieldValueByName(name: "Status") {{
                        ... on ProjectV2ItemFieldSingleSelectValue {{ name }}
                    }}
                    content {{
                        ... on Issue {{
                            number
                            title
                            url
                            updatedAt
                            labels(first: 10) {{ nodes {{ name }} }}
                        }}
                    }}
                }}
            }}
        }}
    }}
}}"""


def fetch_project_items(
    owner: str,
    project_number: int,
    *,
    token: str = "",
) -> list[ProjectItem]:
    """Fetch all items from a GitHub Projects v2 board, preserving board order.

    The ``items`` connection caps each page at 100 nodes, so a board with more
    than 100 items must be walked page by page via the ``pageInfo`` cursor —
    otherwise every item past the first page is silently dropped from the sync.
    """
    items: list[ProjectItem] = []
    position = 0
    after = ""
    while True:
        query = _PROJECT_ITEMS_QUERY.format(owner=owner, project_number=project_number, after=after)
        data = _gh_graphql(query, token=token)
        # ``dig`` null-guards each hop: GraphQL returns ``null`` (not ``{}``) for
        # a user/project the token cannot see, where a chained ``.get(k, {})``
        # would call ``.get`` on ``None`` and crash the board sync.
        raw_items = dig(data, "data", "user", "projectV2", "items", "nodes")
        nodes = raw_items if isinstance(raw_items, list) else []
        for node in nodes:
            if (item := _project_item_from_node(node, position)) is not None:
                items.append(item)
            position += 1
        if dig(data, "data", "user", "projectV2", "items", "pageInfo", "hasNextPage") is not True:
            return items
        end_cursor = dig(data, "data", "user", "projectV2", "items", "pageInfo", "endCursor")
        if not isinstance(end_cursor, str) or not end_cursor:
            return items
        after = f', after: "{end_cursor}"'


def _project_item_from_node(node: object, position: int) -> ProjectItem | None:
    """Build a :class:`ProjectItem` from one board node, or ``None`` to skip.

    Every field read goes through :func:`dig`, which null-guards each hop and
    returns ``object`` — so a draft item (no ``content``) or a node the token
    cannot fully see degrades to a skip rather than crashing the board sync.
    """
    number = dig(node, "content", "number")
    if not isinstance(number, int):
        return None  # draft item or non-issue content
    status_name = dig(node, "fieldValueByName", "name")
    raw_labels = dig(node, "content", "labels", "nodes")
    label_nodes = raw_labels if isinstance(raw_labels, list) else []
    labels = [str(name) for ln in label_nodes if isinstance(name := dig(ln, "name"), str)]
    return ProjectItem(
        issue_number=number,
        title=str(dig(node, "content", "title") or ""),
        url=str(dig(node, "content", "url") or ""),
        status=str(status_name or ""),
        position=position,
        labels=labels,
        updated_at=str(dig(node, "content", "updatedAt") or ""),
    )


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
        data = _gh_api_get(f"search/issues?q={query}&per_page=100", token=self._token)
        if not isinstance(data, dict):
            return []
        items = cast("RawAPIDict", data).get("items")
        if not isinstance(items, list):
            return []
        return cast("list[RawAPIDict]", items)

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
        data = _gh_api_get(f"search/issues?q={query}&per_page=100", token=self._token)
        if not isinstance(data, dict):
            return []
        items = cast("RawAPIDict", data).get("items")
        if not isinstance(items, list):
            return []
        return cast("list[RawAPIDict]", items)

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
        data = _gh_api_get(f"search/issues?q={query}&per_page=100", token=self._token)
        if not isinstance(data, dict):
            return []
        items = cast("RawAPIDict", data).get("items")
        if not isinstance(items, list):
            return []
        return cast("list[RawAPIDict]", items)

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
        data = _gh_api_get(f"repos/{repo}/issues/{match['number']}/comments?per_page=100", token=self._token)
        return cast("list[RawAPIDict]", data) if isinstance(data, list) else []

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
        reviews = _gh_api_get(f"{base}/reviews?per_page=100", token=self._token)
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
