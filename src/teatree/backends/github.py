"""GitHub backend — code host and project board sync via ``gh`` CLI."""

import json
import os
import re
from dataclasses import dataclass
from typing import TypedDict, cast
from urllib.parse import quote_plus, urlparse

from teatree.backends.protocols import ApprovalState, PrOpenState, PullRequestSpec, ReviewState
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
            items(first: 100) {{
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
    """Fetch all items from a GitHub Projects v2 board, preserving board order."""
    query = _PROJECT_ITEMS_QUERY.format(owner=owner, project_number=project_number)
    data = _gh_graphql(query, token=token)
    items: list[ProjectItem] = []

    project = data.get("data", {}).get("user", {}).get("projectV2", {})  # type: ignore[union-attr]
    if not project:
        return items

    nodes = project.get("items", {}).get("nodes", [])
    for position, node in enumerate(nodes):
        if not isinstance(node, dict):
            continue
        content = node.get("content")
        if not isinstance(content, dict) or "number" not in content:
            continue  # skip draft items or non-issue content

        status_field = node.get("fieldValueByName")
        status = status_field.get("name", "") if isinstance(status_field, dict) else ""

        label_nodes = content.get("labels", {}).get("nodes", [])
        labels = [ln["name"] for ln in label_nodes if isinstance(ln, dict) and "name" in ln]

        items.append(
            ProjectItem(
                issue_number=int(content["number"]),
                title=str(content.get("title", "")),
                url=str(content.get("url", "")),
                status=status,
                position=position,
                labels=labels,
                updated_at=str(content.get("updatedAt", "")),
            ),
        )

    return items


def _record_github_note_claim(
    *,
    repo: str,
    target_number: int,
    comment_id: int,
    body: str,
    target_url: str,
) -> None:
    """Audit one successful GitHub-comment publish for the drift verifier (#1198).

    Mirrors :func:`teatree.cli.review_audit.record_note_claim` for the
    GitLab side: best-effort write, never raises into the caller.
    ``payload_digest`` lets the verifier detect silent body-divergence
    without storing the full body in the claim row.

    The idempotency key encodes ``repo``, target number (PR or issue —
    GitHub uses the same ``/issues/<n>/comments`` endpoint for both), and
    the server-assigned ``comment_id`` so a retried POST that the API
    collapsed to the same comment no-ops at the ledger layer.

    Best-effort: any exception (Django not booted, DB outage, integrity
    race) is swallowed. The publish has already succeeded by the time we
    get here — failing to audit it must not turn that success into a
    user-visible failure.
    """
    import hashlib  # noqa: PLC0415 — stdlib, cheap, used only here

    try:
        from django.db import (  # noqa: PLC0415 — keep Django out of module-load if bootstrap fails
            DatabaseError,
            IntegrityError,
            transaction,
        )

        from teatree.core.models import OutboundClaim  # noqa: PLC0415
    except Exception:  # noqa: BLE001 — must never break the publish path
        return

    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    idempotency_key = f"github_note:{repo}#{target_number}:{comment_id}"
    try:
        with transaction.atomic():
            OutboundClaim.objects.get_or_create(
                idempotency_key=idempotency_key,
                defaults={
                    "kind": OutboundClaim.Kind.GITHUB_NOTE.value,
                    "target_url": target_url,
                    "extra": {
                        "repo": repo,
                        "target_number": target_number,
                        "artifact_id": str(comment_id),
                        "payload_digest": digest,
                    },
                },
            )
    except (IntegrityError, DatabaseError):
        return
    except Exception:  # noqa: BLE001 — must never break the publish path
        return


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
        data = _gh_api_get(f"repos/{repo}/issues/{pr_iid}/comments", token=self._token)
        return cast("list[RawAPIDict]", data) if isinstance(data, list) else []

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
