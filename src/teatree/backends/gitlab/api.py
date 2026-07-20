"""GitLab domain queries — MR / issue / pipeline / approval reads on top of the transport.

The raw HTTP concern (auth, pagination, the TTL cache, uploads, GraphQL) lives in
:mod:`teatree.backends.gitlab.http_client`; this module is the DOMAIN layer built on
it. :class:`GitLabHTTPClient`, :class:`ProjectInfo`, :data:`RawMR` and ``_resolve_token``
are re-exported here so every existing ``from teatree.backends.gitlab.api import ...``
keeps working.
"""

import re
from typing import SupportsInt, TypedDict, cast
from urllib.parse import quote_plus, urlencode

import httpx

from teatree.backends.gitlab.http_client import _MAX_PAGES, GitLabHTTPClient, ProjectInfo, RawMR, _resolve_token
from teatree.backends.gitlab.payloads import WORK_ITEM_STATUS_QUERY, status_from_work_item_payload
from teatree.utils import git

__all__ = ["GitLabAPI", "GitLabHTTPClient", "ProjectInfo", "RawMR"]

_ = (_MAX_PAGES, _resolve_token)  # re-exported for the tests/callers that read them off this module

# TTL constants for response caching (seconds)
_TTL_PIPELINE = 60
_TTL_APPROVALS = 60
_TTL_DISCUSSIONS = 120
_TTL_ISSUE = 300
_TTL_WORK_ITEM = 300
_TTL_USERNAME = 3600

# HTTP status code bounds for success classification.
_HTTP_OK_LOW = 200
_HTTP_OK_HIGH = 300


class _ReviewerEntry(TypedDict, total=False):
    """Subset of the GitLab reviewer payload teatree reads (#1295 cap B)."""

    id: int
    username: str


def _as_int(value: object) -> int:
    return int(cast("SupportsInt | str", value))


class GitLabAPI(GitLabHTTPClient):
    def get_work_item_status(self, project_path: str, iid: int) -> str | None:
        """Fetch the Status widget value for a GitLab work item via GraphQL."""
        cache_key = f"work_item_status:{project_path}:{iid}"
        cached: str | None = self._get_cached(cache_key, _TTL_WORK_ITEM)
        if cached is not None:
            return cached
        data = self.graphql(WORK_ITEM_STATUS_QUERY, {"projectPath": project_path, "iid": str(iid)})
        result = status_from_work_item_payload(data)
        self._set_cached(cache_key, result)
        return result

    def resolve_project(self, repo_path: str) -> ProjectInfo | None:
        """Resolve a project slug to its :class:`ProjectInfo`, or ``None`` when unknown.

        ``get_json`` now applies ``raise_for_status``, so an unknown / private
        slug surfaces as an ``httpx.HTTPStatusError`` (HTTP 404), not a silent
        empty read. A 404 is the documented "no such project" degrade → return
        ``None`` so every ``if project is None`` guard downstream stays live;
        any OTHER status (401/403/5xx) is a genuine outage and re-raises so a
        credential/transport failure is never mistaken for "unknown project".

        The ``None`` verdict is deliberately NOT cached (mirrors F4.5): a 404
        seen during a transient outage must not pin the slug as unresolvable for
        the process lifetime — only a successfully-resolved project is cached.
        """
        if repo_path in self._project_cache:
            return self._project_cache[repo_path]

        try:
            data = self.get_json(f"projects/{repo_path.replace('/', '%2F')}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == httpx.codes.NOT_FOUND:
                return None
            raise
        if not isinstance(data, dict):
            return None

        info = ProjectInfo(
            project_id=_as_int(data["id"]),
            path_with_namespace=str(data["path_with_namespace"]),
            short_name=str(data["path"]),
            default_branch=str(data.get("default_branch") or "main"),
        )
        self._project_cache[repo_path] = info
        return info

    def resolve_project_from_remote(self, repo_dir: str = ".") -> ProjectInfo | None:
        remote = git.remote_url(repo=repo_dir)
        if not remote:
            return None
        hostname = re.sub(r"https?://|/api/v\d+/?$", "", self.base_url)
        escaped = re.escape(hostname)
        match = re.search(escaped + r"[:/](.+?)(?:\.git)?$", remote)
        if not match:
            return None
        return self.resolve_project(match.group(1))

    def list_all_open_mrs(
        self,
        author: str,
        *,
        include_draft: bool = True,
        per_page: int = 100,
        updated_after: str | None = None,
    ) -> list[dict[str, object]]:
        """Fetch all open MRs authored by *author* across all accessible projects."""
        query: dict[str, str | int] = {
            "state": "opened",
            "author_username": author,
            "scope": "all",
            "per_page": per_page,
        }
        if updated_after:
            query["updated_after"] = updated_after
        params = urlencode(query)
        data = self.get_json_paginated(f"merge_requests?{params}")
        if include_draft:
            return data
        return [mr for mr in data if not mr.get("draft")]

    def list_open_issues_for_assignee(
        self,
        assignee: str,
        *,
        per_page: int = 100,
        updated_after: str | None = None,
    ) -> list[RawMR]:
        """Fetch all open issues (and work items) assigned to *assignee* across accessible projects."""
        query: dict[str, str | int] = {
            "state": "opened",
            "assignee_username": assignee,
            "scope": "all",
            "per_page": per_page,
        }
        if updated_after:
            query["updated_after"] = updated_after
        params = urlencode(query)
        return self.get_json_paginated(f"issues?{params}")

    def list_open_issues_for_author(
        self,
        author: str,
        *,
        per_page: int = 100,
        updated_after: str | None = None,
        project_slugs: tuple[str, ...] = (),
    ) -> list[RawMR]:
        """Fetch all open issues (and work items) AUTHORED by *author*.

        The author-scoped sibling of :meth:`list_open_issues_for_assignee`, backing the
        issue-implementer's trusted-author intake (#3235): ``author_username``, never
        ``assignee_username`` — a trusted human's issue is actionable the moment they
        file it, with no triage, assignment, or label.

        *project_slugs* scopes the query to each named project's issues endpoint (the
        repos the factory owns); empty keeps the global ``scope=all`` search — matching
        the pre-scope behaviour, which returns issues from every accessible project and
        so admits a cross-repo intake the factory must not implement.
        """
        base: dict[str, str | int] = {"state": "opened", "author_username": author, "per_page": per_page}
        if updated_after:
            base["updated_after"] = updated_after
        if not project_slugs:
            return self.get_json_paginated(f"issues?{urlencode({**base, 'scope': 'all'})}")
        issues: list[RawMR] = []
        for slug in project_slugs:
            issues.extend(self.get_json_paginated(f"projects/{slug.replace('/', '%2F')}/issues?{urlencode(base)}"))
        return issues

    def list_open_mrs_as_reviewer(
        self,
        reviewer: str,
        *,
        per_page: int = 100,
        updated_after: str | None = None,
    ) -> list[RawMR]:
        """Fetch all open MRs where *reviewer* is assigned as reviewer (not author)."""
        query: dict[str, str | int] = {
            "state": "opened",
            "reviewer_username": reviewer,
            "scope": "all",
            "per_page": per_page,
            "not[author_username]": reviewer,
        }
        if updated_after:
            query["updated_after"] = updated_after
        params = urlencode(query)
        return self.get_json_paginated(f"merge_requests?{params}")

    def list_recently_merged_mrs(
        self,
        author: str,
        *,
        updated_after: str | None = None,
        per_page: int = 100,
    ) -> list[RawMR]:
        """Fetch recently merged MRs authored by *author*.

        When *updated_after* is provided, only returns MRs updated after that
        timestamp (ISO 8601).  Otherwise returns the most recent *per_page*.
        """
        return self._list_terminal_mrs("merged", author, updated_after=updated_after, per_page=per_page)

    def list_recently_closed_mrs(
        self,
        author: str,
        *,
        updated_after: str | None = None,
        per_page: int = 100,
    ) -> list[RawMR]:
        """Fetch recently closed-without-merge MRs authored by *author*.

        Mirrors :meth:`list_recently_merged_mrs` for the ``state=closed`` case.
        """
        return self._list_terminal_mrs("closed", author, updated_after=updated_after, per_page=per_page)

    def _list_terminal_mrs(
        self,
        state: str,
        author: str,
        *,
        updated_after: str | None,
        per_page: int,
    ) -> list[RawMR]:
        """List terminal (merged/closed) MRs, newest first.

        With *updated_after* the query is server-side bounded to that cutoff, so
        every matching page is walked. WITHOUT a cutoff only the single most
        recent page (``per_page`` rows) is fetched — walking all pages to
        ``_MAX_PAGES`` there meant up to ``100 * per_page`` rows (10k items) read
        every tick for a callsite that only wants the recent terminal MRs.
        """
        query: dict[str, str | int] = {
            "state": state,
            "author_username": author,
            "scope": "all",
            "per_page": per_page,
            "order_by": "updated_at",
            "sort": "desc",
        }
        if updated_after:
            query["updated_after"] = updated_after
            return self.get_json_paginated(f"merge_requests?{urlencode(query)}")
        data = self.get_json(f"merge_requests?{urlencode(query)}")
        return data if isinstance(data, list) else []

    def get_mr_pipeline(self, project_id: int, mr_iid: int) -> dict[str, str | None]:
        """Return the latest pipeline status and URL for an MR."""
        cache_key = f"pipeline:{project_id}:{mr_iid}"
        cached: dict[str, str | None] | None = self._get_cached(cache_key, _TTL_PIPELINE)
        if cached is not None:
            return cached
        data = self.get_json(f"projects/{project_id}/merge_requests/{mr_iid}/pipelines?per_page=1")
        if isinstance(data, list) and data:
            pipeline = data[0]
            result = {
                "status": str(pipeline.get("status", "")),
                "url": str(pipeline.get("web_url", "")),
            }
        else:
            result = {"status": None, "url": None}
        self._set_cached(cache_key, result)
        return result

    def get_mr_approvals(self, project_id: int, mr_iid: int) -> dict[str, object]:
        """Return approval count, required count, and approver names for an MR."""
        cache_key = f"approvals:{project_id}:{mr_iid}"
        cached: RawMR | None = self._get_cached(cache_key, _TTL_APPROVALS)
        if cached is not None:
            return cached
        data = self.get_json(f"projects/{project_id}/merge_requests/{mr_iid}/approvals")
        if isinstance(data, dict):
            approved_by = data.get("approved_by", [])
            count = len(approved_by) if isinstance(approved_by, list) else 0
            names: list[str] = []
            for entry in approved_by if isinstance(approved_by, list) else []:  # pragma: no branch
                if not isinstance(entry, dict):
                    continue
                entry_dict: dict[str, object] = entry  # type: ignore[assignment]
                user = entry_dict.get("user")
                if isinstance(user, dict):  # pragma: no branch
                    user_dict: dict[str, object] = user  # type: ignore[assignment]
                    username = str(user_dict.get("username", ""))
                    if username:  # pragma: no branch
                        names.append(username)
            left = data.get("approvals_left")
            result_val = {
                "count": count,
                "required": int(data.get("approvals_required", 1)),  # type: ignore[arg-type]
                "approved_by": names,
                "approvals_left": left if isinstance(left, int) and not isinstance(left, bool) else -1,
            }
            self._set_cached(cache_key, result_val)
            return result_val
        fallback = {"count": 0, "required": 1, "approved_by": [], "approvals_left": -1}
        self._set_cached(cache_key, fallback)
        return fallback

    def get_issue(self, project_id: int, issue_iid: int) -> dict[str, object] | None:
        """Fetch a single issue by project ID and IID."""
        cache_key = f"issue:{project_id}:{issue_iid}"
        cached: RawMR | None = self._get_cached(cache_key, _TTL_ISSUE)
        if cached is not None:
            return cached
        data = self.get_json(f"projects/{project_id}/issues/{issue_iid}")
        result = data if isinstance(data, dict) else None
        self._set_cached(cache_key, result)
        return result

    def get_mr_discussions(self, project_id: int, mr_iid: int) -> list[dict[str, object]]:
        """Fetch all discussion threads for a merge request."""
        cache_key = f"discussions:{project_id}:{mr_iid}"
        cached: list[RawMR] | None = self._get_cached(cache_key, _TTL_DISCUSSIONS)
        if cached is not None:
            return cached
        result = self.get_json_paginated(f"projects/{project_id}/merge_requests/{mr_iid}/discussions?per_page=100")
        self._set_cached(cache_key, result)
        return result

    def get_draft_notes_count(self, project_id: int, mr_iid: int) -> int:
        """Return the number of unpublished draft notes on a merge request.

        Paginates to completion: a single ``per_page=100`` page capped the count
        at 100, so an MR with more than 100 draft notes reported a floor, not the
        real total. ``get_json_paginated`` walks every page so the count is exact.
        """
        cache_key = f"draft_notes:{project_id}:{mr_iid}"
        cached: int | None = self._get_cached(cache_key, _TTL_DISCUSSIONS)
        if cached is not None:
            return cached
        data = self.get_json_paginated(f"projects/{project_id}/merge_requests/{mr_iid}/draft_notes?per_page=100")
        count = len(data)
        self._set_cached(cache_key, count)
        return count

    def cancel_pipelines(
        self,
        project_id: int,
        ref: str,
        *,
        statuses: tuple[str, ...] = ("running", "pending"),
    ) -> list[int]:
        """Cancel every running/pending pipeline on *ref*, returning their ids.

        Walks EVERY page of the pipeline list per status (``get_json_paginated``):
        the old ``per_page=10`` page-1-only read left the 11th-onward pipeline
        running silently when a busy ref had more than ten in-flight pipelines.
        """
        cancelled: list[int] = []
        for status in statuses:
            params = urlencode({"ref": ref, "status": status, "per_page": 100})
            for pipeline in self.get_json_paginated(f"projects/{project_id}/pipelines?{params}"):
                pipeline_id = _as_int(pipeline["id"])
                self.post_json(f"projects/{project_id}/pipelines/{pipeline_id}/cancel")
                cancelled.append(pipeline_id)
        return cancelled

    def resolve_user_id_by_username(self, username: str) -> int:
        """Resolve a GitLab username to its numeric user id (#1295 capability B).

        Uses ``GET /users?username=<username>`` which returns at most one
        result (usernames are unique). Returns 0 when the user cannot be
        resolved so callers can detect and report the failure.
        """
        if not username:
            return 0
        data = self.get_json(f"users?username={quote_plus(username)}")
        if not isinstance(data, list) or not data:
            return 0
        first = data[0]
        if not isinstance(first, dict):
            return 0
        return _as_int(first.get("id", 0))

    def assign_reviewer(self, project_id: int, mr_iid: int, user_id: int) -> bool:
        """Append *user_id* to the MR's reviewer list (#1295 capability B).

        Reads the current MR state first to preserve existing reviewer ids
        (never clobbers); idempotent when *user_id* is already a reviewer.
        Returns ``True`` when the PUT succeeded (or the user was already a
        reviewer), ``False`` on a failed lookup / non-2xx response.
        """
        if project_id <= 0 or mr_iid <= 0 or user_id <= 0:
            return False
        current = self.get_json(f"projects/{project_id}/merge_requests/{mr_iid}")
        if not isinstance(current, dict):
            return False
        existing_ids: list[int] = []
        reviewers = current.get("reviewers", [])
        if isinstance(reviewers, list):
            for entry in reviewers:
                if not isinstance(entry, dict):
                    continue
                entry_typed: _ReviewerEntry = entry  # type: ignore[assignment]
                existing_ids.append(_as_int(entry_typed.get("id", 0)))
        if user_id in existing_ids:
            return True
        new_ids = [*existing_ids, user_id]
        status = self.put_status(
            f"projects/{project_id}/merge_requests/{mr_iid}",
            {"reviewer_ids": new_ids},
        )
        return _HTTP_OK_LOW <= status < _HTTP_OK_HIGH

    def current_username(self) -> str:
        cache_key = "username"
        cached: str | None = self._get_cached(cache_key, _TTL_USERNAME)
        if cached is not None:
            return cached
        data = self.get_json("user")
        result = str(data.get("username", "")) if isinstance(data, dict) else ""
        self._set_cached(cache_key, result)
        return result

    @staticmethod
    def current_branch(repo_dir: str = ".") -> str:
        return git.current_branch(repo=repo_dir)
