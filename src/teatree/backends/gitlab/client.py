import re
from pathlib import Path
from typing import TypedDict, cast
from urllib.parse import quote_plus, urlparse

import httpx

from teatree.backends import forge_merge_rpc as _forge_merge
from teatree.backends.errors import IssueNotFoundError
from teatree.backends.gitlab import api as _gitlab_api
from teatree.backends.gitlab import issue_notes as _issue_notes
from teatree.backends.gitlab import subissues as _subissues
from teatree.backends.gitlab import uploads as _uploads
from teatree.backends.gitlab.api import GitLabAPI, ProjectInfo
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

_ISSUE_URL_RE = re.compile(r"^/(?P<path>.+?)/-/issues/(?P<iid>\d+)/?$")
_MR_URL_RE = re.compile(r"^/(?P<path>.+?)/-/merge_requests/(?P<iid>\d+)/?$")


_GITLAB_MR_STATE_MAP: dict[str, PrOpenState] = {
    "opened": PrOpenState.OPEN,
    "merged": PrOpenState.MERGED,
    "closed": PrOpenState.CLOSED,
    "locked": PrOpenState.CLOSED,
}


def _read_int(data: RawAPIDict, key: str) -> int:
    """Return ``data[key]`` as an int, or ``-1`` when the key is absent / non-int.

    The sentinel ``-1`` lets callers distinguish "field missing in payload" from
    a legitimate zero. GitLab's approvals payload uses both ``int`` and ``str``
    encodings across versions, so we accept either.
    """
    value = data.get(key)
    if isinstance(value, bool):
        return -1
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            return -1
    return -1


def _count_unresolved_resolvable_threads(discussions: list[RawAPIDict]) -> int:
    """Count open ``resolvable`` discussion threads — what blocks an MR merge.

    A thread is "unresolved-resolvable" when at least one of its notes is
    ``resolvable: true`` AND no note carries ``resolved: true``. System notes
    and non-resolvable comments are skipped: the GitLab "must resolve all
    threads" policy is keyed on the same ``resolvable`` flag.
    """
    count = 0
    for disc in discussions:
        if not isinstance(disc, dict):
            continue
        notes_raw = disc.get("notes", [])
        if not isinstance(notes_raw, list):
            continue
        has_resolvable = False
        has_resolved = False
        for note in notes_raw:
            if not isinstance(note, dict):
                continue
            note_dict = cast("RawAPIDict", note)
            if note_dict.get("resolvable") is True:
                has_resolvable = True
            if note_dict.get("resolved") is True:
                has_resolved = True
        if has_resolvable and not has_resolved:
            count += 1
    return count


class _GitLabUser(TypedDict, total=False):
    """Subset of the GitLab user payload that teatree reads."""

    username: str


class _GitLabMergeRequestSummary(TypedDict, total=False):
    """Subset of the GitLab MR response read for the review/open-state lookup."""

    reviewers: list[_GitLabUser]
    state: str
    author: _GitLabUser


def get_client(*, token: str = "", base_url: str = "") -> GitLabAPI:
    """Build a ``GitLabAPI`` from explicit credentials.

    When *token* is empty the ``GitLabAPI`` default (env-var fallback) is used.
    Resolves ``GitLabAPI`` through the module attribute so test patches that
    rebind ``teatree.backends.gitlab.api.GitLabAPI`` apply here too.
    """
    return _gitlab_api.GitLabAPI(
        token=token,
        base_url=base_url or "https://gitlab.com/api/v4",
    )


# ast-grep-ignore: ac-django-no-complexity-suppressions
class GitLabCodeHost:  # noqa: PLR0904 — method count reflects the CodeHostBackend Protocol surface, not poor encapsulation.
    def __init__(
        self,
        *,
        client: GitLabAPI | None = None,
        token: str = "",
        base_url: str = "",
    ) -> None:
        self._client = client or get_client(token=token, base_url=base_url)

    @property
    def client(self) -> GitLabAPI:
        """Underlying ``GitLabAPI``.

        Exposed for ``GitLabSyncBackend`` and other GitLab-specific consumers
        that need calls outside the cross-host Protocol surface (pipeline
        status, approvals, discussions, draft notes count, terminal-state PR
        scans). Cross-host code paths must keep using the Protocol methods.
        """
        return self._client

    def create_pr(self, spec: PullRequestSpec) -> RawAPIDict:
        project = self._resolve_project(spec.repo)
        if project is None:
            return {"error": f"Could not resolve project: {spec.repo}"}

        payload: RawAPIDict = {
            "source_branch": spec.branch,
            "target_branch": spec.target_branch or project.default_branch,
            "title": spec.title,
            "description": spec.description,
        }
        if spec.labels:
            payload["labels"] = ",".join(spec.labels)
        if spec.assignee:
            payload["assignee_username"] = spec.assignee
        if spec.draft and not spec.title.startswith("Draft:"):
            payload["title"] = f"Draft: {spec.title}"

        return self._client.post_json(f"projects/{project.project_id}/merge_requests", payload) or {}

    def current_user(self) -> str:
        """Return the authenticated GitLab username."""
        return self._client.current_username()

    def list_my_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        return self._client.list_all_open_mrs(author, updated_after=updated_after)

    def list_my_merged_prs(self, *, author: str, updated_after: str | None = None) -> list[RawAPIDict]:
        return self._client.list_recently_merged_mrs(author, updated_after=updated_after)

    def list_review_requested_prs(
        self,
        *,
        reviewer: str,
        updated_after: str | None = None,
    ) -> list[RawAPIDict]:
        return self._client.list_open_mrs_as_reviewer(reviewer, updated_after=updated_after)

    def list_assigned_issues(self, *, assignee: str) -> list[RawAPIDict]:
        return self._client.list_open_issues_for_assignee(assignee)

    def create_issue(
        self,
        *,
        repo: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> RawAPIDict:
        """Open a GitLab issue on *repo* and return the created payload.

        The returned dict carries ``web_url`` (the clickable issue link) and
        ``iid``. Returns ``{"error": ...}`` when the project cannot resolve.
        """
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        payload: RawAPIDict = {"title": title, "description": body}
        if labels:
            payload["labels"] = ",".join(labels)
        return self._client.post_json(f"projects/{project.project_id}/issues", payload) or {}

    def create_sub_issue(
        self,
        *,
        parent_url: str,
        title: str,
        body: str,
        labels: list[str] | None = None,
        child_type: str = "Task",
    ) -> RawAPIDict:
        """Create a child work item under the parent at *parent_url*.

        GitLab forbids an Issue→Issue parent link, so the child is created as a
        plain issue, converted to *child_type* (default ``Task``) via
        ``workItemConvert``, then linked under the parent via ``workItemUpdate``
        with ``hierarchyWidget.parentId``. The returned dict carries the child's
        ``web_url`` and ``iid``; any failed hop returns ``{"error": ...}`` and
        leaves the partially-created child as a non-linked issue.
        """
        context = _subissues.resolve_sub_context(self._client, parent_url, child_type)
        if not isinstance(context, _subissues.SubContext):
            return context

        created = self.create_issue(repo=context.repo, title=title, body=body, labels=labels)
        if "error" in created:
            return created
        child_iid = created.get("iid")
        if not isinstance(child_iid, int):
            return {"error": f"Child issue creation returned no iid: {created}"}

        child_gid = _subissues.work_item_gid(self._client, context.project_path, child_iid)
        if child_gid is None:
            return {"error": f"Could not resolve created child work item: {created.get('web_url')}"}

        nest_error = _subissues.convert_and_link(self._client, child_gid, context, child_type)
        return nest_error or created

    def search_open_issues(self, *, repo: str, query: str) -> list[RawAPIDict]:
        """Return open issues on *repo* whose title/description match *query*.

        Returns an empty list when the project cannot resolve — the caller
        treats "no matches" and "unresolvable" identically.
        """
        project = self._resolve_project(repo)
        if project is None:
            return []
        endpoint = f"projects/{project.project_id}/issues?state=opened&search={quote_plus(query)}&per_page=100"
        return self._client.get_json_paginated(endpoint)

    def post_pr_comment(self, *, repo: str, pr_iid: int, body: str) -> RawAPIDict:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}

        payload = {"body": body}
        return self._client.post_json(f"projects/{project.project_id}/merge_requests/{pr_iid}/notes", payload) or {}

    def update_pr_comment(self, *, repo: str, pr_iid: int, comment_id: int, body: str) -> RawAPIDict:
        project = self._resolve_project(repo)
        if project is None:
            return {"error": f"Could not resolve project: {repo}"}
        return (
            self._client.put_json(
                f"projects/{project.project_id}/merge_requests/{pr_iid}/notes/{comment_id}",
                {"body": body},
            )
            or {}
        )

    def list_pr_comments(self, *, repo: str, pr_iid: int) -> list[RawAPIDict]:
        project = self._resolve_project(repo)
        if project is None:
            return []
        return self._client.get_json_paginated(
            f"projects/{project.project_id}/merge_requests/{pr_iid}/notes?per_page=100"
        )

    def upload_file(self, *, repo: str, filepath: str) -> dict[str, object]:
        return _uploads.upload_file(self._client, project=self._resolve_project(repo), repo=repo, filepath=filepath)

    def verify_upload(self, *, repo: str, upload: RawAPIDict) -> UploadVerification:
        """Existence-check an upload; the embed is the relative claimable ref (#2165).

        Delegates to :func:`teatree.backends.gitlab.uploads.verify_upload` (which
        cross-checks the project id and returns the relative ``/uploads/...``
        reference GitLab claims on save), keeping this host on the Protocol surface.
        """
        return _uploads.verify_upload(self._client, project=self._resolve_project(repo), upload=upload)

    def get_review_state(self, *, pr_url: str, reviewer: str) -> ReviewState:
        """Return *reviewer*'s current review state on the MR at *pr_url*.

        GitLab does not expose a per-reviewer review timeline, so we infer
        state from the MR's approval list and reviewer assignment. When the
        reviewer's username is in ``approved_by``, they are ``APPROVED``;
        when they are still a requested reviewer but not in ``approved_by``
        they are ``PENDING`` (a fresh re-request, or an approval that the
        forge dropped on force-push). Unparsable URLs yield ``NONE``.
        """
        path = urlparse(pr_url).path
        match = _MR_URL_RE.match(path)
        if match is None or not reviewer:
            return ReviewState.NONE

        project = self._client.resolve_project(match["path"])
        if project is None:
            return ReviewState.NONE

        approvals = self._client.get_mr_approvals(project.project_id, int(match["iid"]))
        approved_by = approvals.get("approved_by")
        if isinstance(approved_by, list) and reviewer in approved_by:
            return ReviewState.APPROVED

        mr = self._client.get_json(f"projects/{project.project_id}/merge_requests/{match['iid']}")
        if isinstance(mr, dict):
            reviewers = cast("_GitLabMergeRequestSummary", mr).get("reviewers")
            if isinstance(reviewers, list):
                for entry in reviewers:
                    if isinstance(entry, dict) and entry.get("username") == reviewer:
                        return ReviewState.PENDING
        return ReviewState.NONE

    def get_pr_open_state(self, *, pr_url: str) -> PrOpenState:
        """Return whether the MR at *pr_url* is genuinely open/merged/closed (#1074).

        Fetches the MR's real ``state`` field. ``opened`` → OPEN, ``merged``
        → MERGED, ``closed``/``locked`` → CLOSED. Any exception, unresolvable
        project, unparsable URL, or non-dict / unrecognised payload →
        ``UNKNOWN`` so the orphan sweep fails open (never reaps on doubt).
        GitHub's implementation maps the same ambiguity to ``UNKNOWN``.
        """
        match = _MR_URL_RE.match(urlparse(pr_url).path)
        if match is None:
            return PrOpenState.UNKNOWN
        try:
            project = self._client.resolve_project(match["path"])
            if project is None:
                return PrOpenState.UNKNOWN
            mr = self._client.get_json(f"projects/{project.project_id}/merge_requests/{match['iid']}")
        except Exception:  # noqa: BLE001 — fail open: any failure must NOT reap a live review.
            return PrOpenState.UNKNOWN
        if not isinstance(mr, dict):
            return PrOpenState.UNKNOWN
        state = cast("_GitLabMergeRequestSummary", mr).get("state")
        if not isinstance(state, str):
            return PrOpenState.UNKNOWN
        return _GITLAB_MR_STATE_MAP.get(state, PrOpenState.UNKNOWN)

    def get_pr_author(self, *, pr_url: str) -> str:
        """Return the MR author's GitLab username, or ``""`` when it can't be resolved.

        Fetches the MR payload and reads ``author.username``. Any exception,
        unresolvable project, unparsable URL, or non-dict / author-less
        payload returns ``""`` — the reaction scanners treat an unresolved
        author as "not provably self" and skip the reaction, so a transient
        lookup failure can never cause a reaction on the user's own MR.
        """
        match = _MR_URL_RE.match(urlparse(pr_url).path)
        if match is None:
            return ""
        try:
            project = self._client.resolve_project(match["path"])
            if project is None:
                return ""
            mr = self._client.get_json(f"projects/{project.project_id}/merge_requests/{match['iid']}")
        except Exception:  # noqa: BLE001 — fail safe: an unresolved author must skip the reaction.
            return ""
        if not isinstance(mr, dict):
            return ""
        author = cast("_GitLabMergeRequestSummary", mr).get("author")
        if isinstance(author, dict):
            username = cast("_GitLabUser", author).get("username")
            if isinstance(username, str):
                return username
        return ""

    def assign_reviewer(self, *, pr_url: str, username: str) -> bool:
        """Append *username* as a reviewer on the MR at *pr_url* (#1295 cap B).

        Resolves the project from the URL path, looks up *username* via the
        GitLab ``/users`` endpoint, then calls
        :meth:`gitlab_api.GitLabAPI.assign_reviewer` which preserves the
        existing reviewer list. Returns ``False`` on any failure (URL
        parse, project lookup, username lookup, PUT failure) so callers
        can surface the failure to the user instead of silently swallowing
        it.
        """
        if not pr_url or not username:
            return False
        match = _MR_URL_RE.match(urlparse(pr_url).path)
        if match is None:
            return False
        try:
            project = self._client.resolve_project(match["path"])
            if project is None:
                return False
            user_id = self._client.resolve_user_id_by_username(username)
            if user_id <= 0:
                return False
            return self._client.assign_reviewer(project.project_id, int(match["iid"]), user_id)
        except Exception:  # noqa: BLE001 — fail closed: callers must see False on lookup errors.
            return False

    def get_issue(self, issue_url: str) -> RawAPIDict:
        """Fetch a GitLab issue from its full URL.

        Supports the canonical web format ``https://gitlab.example.com/<group>/<repo>/-/issues/<iid>``.
        Returns ``{"error": ...}`` when the URL is not a recognised GitLab issue URL or when
        the project cannot be resolved.

        Raises:
            IssueNotFoundError: when the GitLab API returns HTTP 404 (issue
                permanently deleted or never existed).  Any other HTTP error
                (5xx) or network failure propagates as-is so the scanner keeps
                retrying it.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_URL_RE.match(path)
        if match is None:
            return {"error": f"Not a GitLab issue URL: {issue_url}"}

        project = self._client.resolve_project(match["path"])
        if project is None:
            return {"error": f"Could not resolve project: {match['path']}"}

        try:
            issue = self._client.get_issue(project.project_id, int(match["iid"]))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:  # noqa: PLR2004
                raise IssueNotFoundError(issue_url) from exc
            raise
        return issue if isinstance(issue, dict) else {"error": f"Issue not found: {issue_url}"}

    def close_issue(self, *, issue_url: str, comment: str = "") -> RawAPIDict:
        """Close a GitLab issue, optionally leaving an audit-trail note first.

        Idempotent: ``PUT state_event=close`` is a no-op on an already-closed
        issue. Returns ``{"error": ...}`` when the URL is not a recognised
        GitLab issue URL or when the project cannot be resolved.
        """
        path = urlparse(issue_url).path
        match = _ISSUE_URL_RE.match(path)
        if match is None:
            return {"error": f"Not a GitLab issue URL: {issue_url}"}

        project = self._client.resolve_project(match["path"])
        if project is None:
            return {"error": f"Could not resolve project: {match['path']}"}

        if comment:
            self.post_issue_comment(issue_url=issue_url, body=comment)
        return (
            self._client.put_json(
                f"projects/{project.project_id}/issues/{int(match['iid'])}",
                {"state_event": "close"},
            )
            or {}
        )

    def repo_for_issue_url(self, issue_url: str) -> str:  # noqa: PLR6301 — pure URL parse, on the host for the Protocol surface.
        """Return the project slug that OWNS *issue_url* (the note's own project).

        The evidence command uploads artifacts to this slug so they land in the
        same project's ``/uploads`` namespace the note is created on — a note
        renders only the uploads claimed by its OWN project (a different repo's
        upload 404s). Returns ``""`` for a non-issue URL. See
        :mod:`teatree.backends.gitlab.issue_notes`.
        """
        return _issue_notes.repo_for_issue_url(issue_url)

    def post_issue_comment(self, *, issue_url: str, body: str) -> RawAPIDict:
        """Post a note on a GitLab issue / work item (delegates to :mod:`issue_notes`)."""
        return _issue_notes.post_issue_comment(self._client, issue_url=issue_url, body=body)

    def list_issue_comments(self, *, issue_url: str) -> list[RawAPIDict]:
        """List the notes on a GitLab issue / work item (delegates to :mod:`issue_notes`)."""
        return _issue_notes.list_issue_comments(self._client, issue_url=issue_url)

    def update_issue_comment(self, *, issue_url: str, comment_id: int, body: str) -> RawAPIDict:
        """Edit a note in place to keep ONE evidence note per ticket (delegates to :mod:`issue_notes`)."""
        return _issue_notes.update_issue_comment(self._client, issue_url=issue_url, comment_id=comment_id, body=body)

    def delete_issue_comment(self, *, issue_url: str, comment_id: int) -> RawAPIDict:
        return _issue_notes.delete_issue_comment(self._client, issue_url=issue_url, comment_id=comment_id)

    def get_mr_approvals(self, *, repo: str, pr_iid: int) -> ApprovalState:
        """Return the approval state for an MR — used by ``GitLabApprovalsScanner`` (#936).

        ``approvals_left`` is computed from the same ``/merge_requests/<iid>/approvals``
        endpoint that :py:meth:`get_review_state` already consults — the upstream
        ``approvals_left`` field is canonical; falling back to ``required - count``
        when the field is absent. ``unresolved_resolvable`` counts open
        ``resolvable`` discussion threads from ``/merge_requests/<iid>/discussions``
        (system notes and non-resolvable comments are excluded — they cannot block a
        merge under the "must resolve" policy).
        """
        project = self._resolve_project(repo)
        if project is None:
            return ApprovalState(approvals_left=0, approved_by=[], unresolved_resolvable=0)

        raw = self._client.get_mr_approvals(project.project_id, pr_iid)
        approved_by_raw = raw.get("approved_by", [])
        approved_by = (
            [name for name in approved_by_raw if isinstance(name, str)] if isinstance(approved_by_raw, list) else []
        )
        approvals_left = _read_int(raw, "approvals_left")
        if approvals_left < 0:
            required = _read_int(raw, "required")
            count = _read_int(raw, "count")
            approvals_left = max(required - count, 0)

        discussions = self._client.get_mr_discussions(project.project_id, pr_iid)
        unresolved_resolvable = _count_unresolved_resolvable_threads(discussions)
        return ApprovalState(
            approvals_left=approvals_left,
            approved_by=approved_by,
            unresolved_resolvable=unresolved_resolvable,
        )

    def _resolve_project(self, repo: str) -> ProjectInfo | None:
        """Resolve a GitLab project from a local path, ``namespace/repo`` slug, or bare name.

        Bare repo names (no slash, no matching path) fall back to the CWD's
        git remote — ``Worktree.repo_path`` stores a bare name, and callers
        that hand it straight to the code host would otherwise 404.
        """
        if Path(repo).exists():
            return self._client.resolve_project_from_remote(repo)
        if "/" in repo:
            return self._client.resolve_project(repo)
        return self._client.resolve_project_from_remote(".")

    def _merge_rpc(self) -> _forge_merge.GlabMergeRpc:  # noqa: PLR6301 — runner needs no instance state; on the host for the Protocol.
        return _forge_merge.GlabMergeRpc(_forge_merge.glab_runner())

    def fetch_live_head_sha(self, *, slug: str, pr_id: int) -> str:
        return self._merge_rpc().fetch_live_head_sha(slug=slug, pr_id=pr_id)

    def fetch_pr_merge_state(self, *, slug: str, pr_id: int) -> PrMergeState:
        return self._merge_rpc().fetch_pr_merge_state(slug=slug, pr_id=pr_id)

    def fetch_pr_is_draft(self, *, slug: str, pr_id: int) -> bool:
        return self._merge_rpc().fetch_pr_is_draft(slug=slug, pr_id=pr_id)

    def fetch_required_checks_rollup(self, *, slug: str, pr_id: int) -> list[RawAPIDict]:
        return self._merge_rpc().fetch_required_checks_rollup(slug=slug, pr_id=pr_id)

    def fetch_pr_changed_paths(self, *, slug: str, pr_id: int) -> list[str]:
        return self._merge_rpc().fetch_pr_changed_paths(slug=slug, pr_id=pr_id)

    def merge_pr_squash_bound(self, *, slug: str, pr_id: int, expected_head_oid: str) -> ForgeMergeResult:
        return self._merge_rpc().merge_pr_squash_bound(slug=slug, pr_id=pr_id, expected_head_oid=expected_head_oid)
