import os
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import SupportsInt, TypedDict, cast
from urllib.parse import urlencode

import httpx

from teatree.backends.gitlab.payloads import WORK_ITEM_STATUS_QUERY, status_from_work_item_payload
from teatree.utils import git

type RawMR = dict[str, object]

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

# Upper bound on pages walked for an offset-paginated list endpoint. GitLab
# serves at most 100 items per page; this cap stops a runaway loop if the API
# ever returns a malformed ``x-next-page`` that never empties.
_MAX_PAGES = 100


class _ReviewerEntry(TypedDict, total=False):
    """Subset of the GitLab reviewer payload teatree reads (#1295 cap B)."""

    id: int
    username: str


def _as_int(value: object) -> int:
    return int(cast("SupportsInt | str", value))


@dataclass(frozen=True, slots=True)
class ProjectInfo:
    project_id: int
    path_with_namespace: str
    short_name: str
    default_branch: str = "main"


def _resolve_token() -> str:
    """Resolve a GitLab token from env, then ``pass`` store as fallback."""
    token = os.environ.get("GITLAB_TOKEN", "")
    if token:
        return token
    from teatree.utils.secrets import read_pass  # noqa: PLC0415 — deferred to avoid circular import at module load

    return read_pass("gitlab/pat")


class GitLabHTTPClient:
    def __init__(self, *, token: str = "", base_url: str = "https://gitlab.com/api/v4") -> None:
        self.token = token or _resolve_token()
        self.base_url = base_url.rstrip("/")
        self._project_cache: dict[str, ProjectInfo] = {}
        self._response_cache: dict[str, tuple[float, object]] = {}

    def _get_cached(self, cache_key: str, ttl: int) -> object | None:
        entry = self._response_cache.get(cache_key)
        if entry is not None and (time.monotonic() - entry[0]) < ttl:
            return entry[1]
        return None

    def _set_cached(self, cache_key: str, value: object) -> None:
        self._response_cache[cache_key] = (time.monotonic(), value)

    def _headers(self) -> dict[str, str]:
        return {"PRIVATE-TOKEN": self.token}

    def clear_response_cache(self) -> None:
        self._response_cache.clear()

    def get_json(self, endpoint: str) -> dict[str, object] | list[dict[str, object]] | None:
        if not self.token:
            return None
        response = httpx.get(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object] | list[dict[str, object]]", response.json())

    def get_json_paginated(self, endpoint: str) -> list[RawMR]:
        """Fetch every page of an offset-paginated GitLab list endpoint.

        GitLab returns each list page's continuation in the ``x-next-page``
        response header — the next page number, or empty on the last page.
        ``get_json`` reads only the first page, silently truncating any result
        set larger than ``per_page``; this follows ``x-next-page`` until empty,
        accumulating every page's items. Returns an empty list when there is no
        token or a page body is not a JSON array. *endpoint* should already
        carry the query string; the ``page`` parameter is appended per request.
        """
        if not self.token:
            return []
        sep = "&" if "?" in endpoint else "?"
        items: list[RawMR] = []
        page = 1
        for _ in range(_MAX_PAGES):
            response = httpx.get(
                f"{self.base_url}/{endpoint.lstrip('/')}{sep}page={page}",
                headers=self._headers(),
                timeout=10.0,
            )
            response.raise_for_status()
            body = response.json()
            if not isinstance(body, list):
                break
            items.extend(cast("list[RawMR]", body))
            next_page = response.headers.get("x-next-page", "")
            if not next_page:
                break
            page = int(next_page)
        return items

    def post_json(self, endpoint: str, payload: dict[str, object] | None = None) -> dict[str, object] | None:
        if not self.token:
            return None
        response = httpx.post(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=payload or {},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())

    def post_status(self, endpoint: str, payload: Mapping[str, object] | None = None) -> int:
        if not self.token:
            return 0
        response = httpx.post(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=dict(payload) if payload else {},
            timeout=10.0,
        )
        return response.status_code

    def put_json(self, endpoint: str, payload: dict[str, object] | None = None) -> dict[str, object] | None:
        if not self.token:
            return None
        response = httpx.put(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=payload or {},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())

    def put_status(self, endpoint: str, payload: Mapping[str, object] | None = None) -> int:
        if not self.token:
            return 0
        response = httpx.put(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            json=dict(payload) if payload else {},
            timeout=10.0,
        )
        return response.status_code

    def delete(self, endpoint: str) -> int:
        if not self.token:
            return 0
        response = httpx.delete(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers=self._headers(),
            timeout=10.0,
        )
        return response.status_code

    def upload_file(self, project_id: int, filepath: str) -> dict[str, object] | None:
        if not self.token:
            return None
        with Path(filepath).open("rb") as f:
            response = httpx.post(
                f"{self.base_url}/projects/{project_id}/uploads",
                headers=self._headers(),
                files={"file": (Path(filepath).name, f)},
                timeout=30.0,
            )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())

    def fetch_upload(self, project_id: int, secret: str, filename: str) -> tuple[int, bytes]:
        """Fetch an uploaded file's bytes through the token-authenticated API route.

        The web upload-serving routes (``/uploads/<secret>/<file>`` and the
        ``/-/project/<id>/uploads/...`` form a rendered note's ``<img>``/``<video>``
        points at) reject a ``PRIVATE-TOKEN`` — they require a browser session
        cookie (a token request 302s to sign-in or 404s). The API route
        ``GET /projects/:id/uploads/:secret/:filename`` (GitLab 16.6+) is the
        only token-authenticated way to confirm an upload resolves. Returns the
        HTTP status and the response body so the caller can assert ``200`` and
        magic-byte-check the content (GitLab serves every upload as
        ``application/octet-stream``, so the content-type header proves nothing).
        Returns ``(0, b"")`` when there is no token.
        """
        if not self.token:
            return 0, b""
        response = httpx.get(
            f"{self.base_url}/projects/{project_id}/uploads/{secret}/{filename}",
            headers=self._headers(),
            timeout=30.0,
        )
        return response.status_code, response.content

    def graphql(self, query: str, variables: dict[str, object] | None = None) -> dict[str, object] | None:
        if not self.token:
            return None
        graphql_url = self.base_url.replace("/api/v4", "/api/graphql")
        response = httpx.post(
            graphql_url,
            headers=self._headers(),
            json={"query": query, "variables": variables or {}},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())


class GitLabAPI(GitLabHTTPClient):
    def get_work_item_status(self, project_path: str, iid: int) -> str | None:
        """Fetch the Status widget value for a GitLab work item via GraphQL."""
        cache_key = f"work_item_status:{project_path}:{iid}"
        cached = self._get_cached(cache_key, _TTL_WORK_ITEM)
        if cached is not None:
            return cached  # type: ignore[return-value]
        data = self.graphql(WORK_ITEM_STATUS_QUERY, {"projectPath": project_path, "iid": str(iid)})
        result = status_from_work_item_payload(data)
        self._set_cached(cache_key, result)
        return result

    def resolve_project(self, repo_path: str) -> ProjectInfo | None:
        if repo_path in self._project_cache:
            return self._project_cache[repo_path]

        data = self.get_json(f"projects/{repo_path.replace('/', '%2F')}")
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
        params = urlencode(query)
        return self.get_json_paginated(f"merge_requests?{params}")

    def get_mr_pipeline(self, project_id: int, mr_iid: int) -> dict[str, str | None]:
        """Return the latest pipeline status and URL for an MR."""
        cache_key = f"pipeline:{project_id}:{mr_iid}"
        cached = self._get_cached(cache_key, _TTL_PIPELINE)
        if cached is not None:
            return cached  # type: ignore[return-value]
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
        cached = self._get_cached(cache_key, _TTL_APPROVALS)
        if cached is not None:
            return cached  # type: ignore[return-value]
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
        cached = self._get_cached(cache_key, _TTL_ISSUE)
        if cached is not None:
            return cached  # type: ignore[return-value]
        data = self.get_json(f"projects/{project_id}/issues/{issue_iid}")
        result = data if isinstance(data, dict) else None
        self._set_cached(cache_key, result)
        return result

    def get_mr_discussions(self, project_id: int, mr_iid: int) -> list[dict[str, object]]:
        """Fetch all discussion threads for a merge request."""
        cache_key = f"discussions:{project_id}:{mr_iid}"
        cached = self._get_cached(cache_key, _TTL_DISCUSSIONS)
        if cached is not None:
            return cached  # type: ignore[return-value]
        result = self.get_json_paginated(f"projects/{project_id}/merge_requests/{mr_iid}/discussions?per_page=100")
        self._set_cached(cache_key, result)
        return result

    def get_draft_notes_count(self, project_id: int, mr_iid: int) -> int:
        """Return the number of unpublished draft notes on a merge request."""
        cache_key = f"draft_notes:{project_id}:{mr_iid}"
        cached = self._get_cached(cache_key, _TTL_DISCUSSIONS)
        if cached is not None:
            return cached  # type: ignore[return-value]
        data = self.get_json(f"projects/{project_id}/merge_requests/{mr_iid}/draft_notes?per_page=100")
        count = len(data) if isinstance(data, list) else 0
        self._set_cached(cache_key, count)
        return count

    def cancel_pipelines(
        self,
        project_id: int,
        ref: str,
        *,
        statuses: tuple[str, ...] = ("running", "pending"),
    ) -> list[int]:
        cancelled: list[int] = []
        for status in statuses:
            params = urlencode({"ref": ref, "status": status, "per_page": 10})
            data = self.get_json(f"projects/{project_id}/pipelines?{params}")
            if not isinstance(data, list):
                continue
            for pipeline in data:
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
        data = self.get_json(f"users?username={username}")
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
        cached = self._get_cached(cache_key, _TTL_USERNAME)
        if cached is not None:
            return cached  # type: ignore[return-value]
        data = self.get_json("user")
        result = str(data.get("username", "")) if isinstance(data, dict) else ""
        self._set_cached(cache_key, result)
        return result

    @staticmethod
    def current_branch(repo_dir: str = ".") -> str:
        return git.current_branch(repo=repo_dir)
