"""GitHub backend — code host via the ``gh`` CLI."""

import json
import re
from typing import cast
from urllib.parse import quote_plus, urlparse

from teatree.backends import forge_merge_rpc as _forge_merge
from teatree.backends.errors import IssueNotFoundError
from teatree.backends.github.api import (
    _gh_api_get,
    _gh_api_get_paginated,
    _gh_api_patch,
    _gh_api_post,
    _gh_api_search_paginated,
    _parse_issue_ref,
    _run_gh,
)
from teatree.backends.github.claims import record_github_note_claim as _record_github_note_claim
from teatree.backends.github.payloads import (
    _GitHubPullRequestSummary,
    _GitHubUser,
    latest_review_state_from_reviews,
    pr_open_state_from_payload,
    reviewer_is_requested,
)
from teatree.core.backend_protocols import (
    ApprovalState,
    ForgeMergeResult,
    PrMergeState,
    PrOpenState,
    PullRequestSpec,
    ReviewState,
    UploadVerification,
)
from teatree.types import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CommandFailedError

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


# ast-grep-ignore: ac-django-no-complexity-suppressions
class GitHubCodeHost:  # noqa: PLR0904 — method count reflects the CodeHostBackend Protocol surface, not poor encapsulation.
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

    def is_assignable(self, *, repo: str, login: str) -> bool:
        """Whether *login* can be assigned on *repo* (#3100).

        ``GET /repos/{slug}/assignees/{login}`` answers 204 for an
        assignable login and 404 otherwise; any probe failure (network,
        auth, no slug) reads as not-assignable so PR creation degrades to
        an unassigned PR instead of failing at ``gh --assignee``.
        """
        slug = git.remote_slug(repo=repo)
        if not slug or not login:
            return False
        try:
            _run_gh("gh", "api", f"repos/{slug}/assignees/{login}", "--silent", token=self._token)
        except CommandFailedError:
            return False
        return True

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        terms = [f"is:pr is:open author:{author}"]
        if updated_after:
            terms.append(f"updated:>={updated_after}")
        query = quote_plus(" ".join(terms))
        return _gh_api_search_paginated(f"search/issues?q={query}&per_page=100", token=self._token)

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        terms = [f"is:pr is:merged author:{author}"]
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

    def list_prs(self, *, repo: str, state: str = "", author: str = "") -> list[RawAPIDict]:
        """Return PRs on ``owner/repo`` filtered by *state* and *author*.

        *state* is GitHub's search qualifier (``open`` / ``closed`` / ``merged``);
        an empty *state* lists every state. Uses the issue-search API so an
        ``author`` filter is a first-class qualifier (the ``pulls`` list endpoint
        cannot filter by author).
        """
        terms = [f"repo:{repo} is:pr"]
        if state:
            terms.append(f"is:{state}")
        if author:
            terms.append(f"author:{author}")
        query = quote_plus(" ".join(terms))
        return _gh_api_search_paginated(f"search/issues?q={query}&per_page=100", token=self._token)

    def get_pr_diff(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        """Return the PR's changed files (path + per-file additions/deletions/patch).

        Returns ``[]`` when the PR/repo cannot be read — an unknown PR degrades to
        a caught empty result rather than crashing the caller.
        """
        try:
            return _gh_api_get_paginated(f"repos/{repo}/pulls/{pr_iid}/files?per_page=100", token=self._token)
        except CommandFailedError:
            return []

    def list_pr_commits(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        """Return the commits on the PR; ``[]`` when the PR/repo cannot be read."""
        try:
            return _gh_api_get_paginated(f"repos/{repo}/pulls/{pr_iid}/commits?per_page=100", token=self._token)
        except CommandFailedError:
            return []

    def get_repo(self, *, repo: str) -> RawAPIDict:
        """Return ``owner/repo`` metadata (default branch, visibility, …).

        Returns ``{"error": ...}`` when the repo cannot be resolved so an unknown
        repo yields a caught structured error, never an uncaught transport failure.
        """
        try:
            data = _gh_api_get(f"repos/{repo}", token=self._token)
        except CommandFailedError:
            return {"error": f"Could not resolve repo: {repo}"}
        return cast("RawAPIDict", data) if isinstance(data, dict) else {"error": f"Repo not found: {repo}"}

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

    def list_authored_issues(self, *, author: str) -> list[RawAPIDict]:
        """Open issues *author* FILED — the issue-implementer's trusted-author intake query (#3235).

        Distinct from :meth:`list_assigned_issues`: intake is decided by who WROTE
        the issue, not by who it landed on, so the factory can pick up an untriaged,
        unassigned, unlabelled issue the moment a trusted human files it.
        """
        query = quote_plus(f"is:issue is:open author:{author}")
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

    def close_issue(self, *, issue_url: str, comment: str = "") -> RawAPIDict:
        """Close a GitHub issue, optionally leaving an audit-trail comment first.

        Idempotent: ``PATCH state=closed`` is a no-op on an already-closed issue.
        Returns ``{"error": ...}`` when the URL is not a recognised GitHub issue URL.
        """
        ref = _parse_issue_ref(issue_url)
        if ref is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}
        repo, number = ref
        if comment:
            self.post_issue_comment(issue_url=issue_url, body=comment)
        data = _gh_api_patch(
            f"repos/{repo}/issues/{number}",
            {"state": "closed", "state_reason": "not_planned"},
            token=self._token,
        )
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    def update_issue(self, *, issue_url: str, body: str) -> RawAPIDict:
        """Replace a GitHub issue's body (description) in place.

        Used to keep ONE auto-managed checkbox ledger in a standing umbrella
        issue's body: the dream-promote flow re-fetches the body, upserts a
        gap checkbox keyed on a stable HTML-comment marker, and writes the
        whole body back. Returns ``{"error": ...}`` when the URL is not a
        recognised GitHub issue URL.
        """
        ref = _parse_issue_ref(issue_url)
        if ref is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}
        repo, number = ref
        data = _gh_api_patch(f"repos/{repo}/issues/{number}", {"body": body}, token=self._token)
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    def upload_file(self, *, repo: str, filepath: str) -> RawAPIDict:
        msg = f"File upload to {repo} not supported (token={'set' if self._token else 'unset'}, file={filepath})"
        raise NotImplementedError(msg)

    def verify_upload(self, *, repo: str, upload: RawAPIDict) -> UploadVerification:
        msg = f"Upload verification for {repo} not supported (GitHub has no project upload API; upload={upload})"
        raise NotImplementedError(msg)

    def get_issue(self, issue_url: str) -> RawAPIDict:
        """Fetch a GitHub issue from its full URL.

        Supports ``https://github.com/<owner>/<repo>/issues/<number>``.
        Returns ``{"error": ...}`` when the URL is not a recognised GitHub
        issue URL.

        Raises:
            IssueNotFoundError: when ``gh api`` reports HTTP 404 (issue
                permanently deleted or never existed).  Any other failure
                (5xx, timeout, network error) propagates as the original
                ``CommandFailedError`` so the scanner keeps retrying it.
        """
        ref = _parse_issue_ref(issue_url)
        if ref is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}
        repo, number = ref
        endpoint = f"repos/{repo}/issues/{number}"
        try:
            data = _gh_api_get(endpoint, token=self._token)
        except CommandFailedError as exc:
            # ``gh api`` exits non-zero for ALL HTTP errors (404, 5xx alike).
            # The only reliable signal for a permanent 404 is the literal
            # "HTTP 404" string in stderr — returncode is always 1.
            if "HTTP 404" in exc.stderr:
                raise IssueNotFoundError(issue_url) from exc
            raise
        return cast("RawAPIDict", data) if isinstance(data, dict) else {"error": f"Issue not found: {issue_url}"}

    def repo_for_issue_url(self, issue_url: str) -> str:  # noqa: PLR6301 — pure URL parse, on the host for the Protocol surface.
        """Return the ``<owner>/<repo>`` that owns *issue_url*, or ``""`` when unparsable."""
        ref = _parse_issue_ref(issue_url)
        return ref[0] if ref is not None else ""

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict:
        """Post a comment to a GitHub issue; returns ``{"error": ...}`` on a non-issue URL."""
        ref = _parse_issue_ref(issue_url)
        if ref is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}
        repo, target_number = ref
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
        """List the comments on a GitHub issue; returns ``[]`` on a non-issue URL."""
        ref = _parse_issue_ref(issue_url)
        if ref is None:
            return []
        repo, number = ref
        return _gh_api_get_paginated(f"repos/{repo}/issues/{number}/comments?per_page=100", token=self._token)

    def update_issue_comment(self, *, issue_url: str, comment_id: int, body: str) -> RawAPIDict:
        """Edit a GitHub issue comment in place via /repos/{repo}/issues/comments/{id}.

        Returns ``{"error": ...}`` when the URL is not a recognised GitHub issue URL.
        """
        ref = _parse_issue_ref(issue_url)
        if ref is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}
        repo = ref[0]
        data = _gh_api_patch(
            f"repos/{repo}/issues/comments/{comment_id}",
            {"body": body},
            token=self._token,
        )
        return cast("RawAPIDict", data) if isinstance(data, dict) else {}

    def delete_issue_comment(self, *, issue_url: str, comment_id: int) -> RawAPIDict:
        ref = _parse_issue_ref(issue_url)
        if ref is None:
            return {"error": f"Not a GitHub issue URL: {issue_url}"}
        repo = ref[0]
        _run_gh(
            "gh",
            "api",
            f"repos/{repo}/issues/comments/{comment_id}",
            "--method",
            "DELETE",
            "--header",
            "Accept: application/vnd.github+json",
            token=self._token,
        )
        return {}

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        """Return GitHub's aggregate review decision as an approval snapshot (#8).

        GitHub exposes no numeric "approvals remaining" counter; its aggregate
        ``reviewDecision`` (``gh pr view --json reviewDecision``) is the
        merge-authorising signal — ``APPROVED`` means every required review is
        satisfied. Maps to ``approvals_left=0`` on ``APPROVED`` and ``1``
        otherwise, so a not-yet-approved (or a payload with no decision) is
        never mis-read as merge-authorised. ``unresolved_resolvable`` is ``0``:
        GitHub has no resolvable-thread merge block like GitLab's "must resolve"
        policy, so nothing there gates the M7 waiting lane.
        """
        result = _run_gh("gh", "pr", "view", str(pr_iid), "--repo", repo, "--json", "reviewDecision", token=self._token)
        try:
            data = json.loads(result.stdout or "{}")
        except json.JSONDecodeError:
            data = {}
        decision = str(data.get("reviewDecision") or "").upper() if isinstance(data, dict) else ""
        return ApprovalState(
            approvals_left=0 if decision == "APPROVED" else 1,
            approved_by=[],
            unresolved_resolvable=0,
        )

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
        terminal = latest_review_state_from_reviews(reviews, reviewer)
        if terminal is not None:
            return terminal

        pr = _gh_api_get(base, token=self._token)
        if reviewer_is_requested(pr, reviewer):
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
        return pr_open_state_from_payload(pr)

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

    def _merge_rpc(self) -> _forge_merge.GhMergeRpc:
        return _forge_merge.GhMergeRpc(_forge_merge.gh_runner(self._token))

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        return self._merge_rpc().fetch_live_head_sha(slug=slug, pr_id=pr_id)

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        return self._merge_rpc().fetch_pr_merge_state(slug=slug, pr_id=pr_id)

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        return self._merge_rpc().fetch_pr_is_draft(slug=slug, pr_id=pr_id)

    def fetch_pr_author(self, *, slug: str, pr_id: int) -> str:
        return self._merge_rpc().fetch_pr_author(slug=slug, pr_id=pr_id)

    def fetch_pr_same_repo(self, *, slug: str, pr_id: int) -> bool | None:
        return self._merge_rpc().fetch_pr_same_repo(slug=slug, pr_id=pr_id)

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        return self._merge_rpc().fetch_required_checks_rollup(slug=slug, pr_id=pr_id)

    def fetch_required_status_check_contexts(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        return self._merge_rpc().fetch_required_status_check_contexts(slug=slug, pr_id=pr_id)

    def fetch_pr_changed_paths(self, *, slug: str, pr_id: int) -> list[str]:
        return self._merge_rpc().fetch_pr_changed_paths(slug=slug, pr_id=pr_id)

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> ForgeMergeResult:
        return self._merge_rpc().merge_pr_squash_bound(slug=slug, pr_id=pr_id, expected_head_oid=expected_head_oid)
