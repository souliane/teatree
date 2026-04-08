import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import SupportsInt, cast

import httpx

from teatree.utils import git

# TTL constants for response caching (seconds)
_TTL_PIPELINE = 60
_TTL_APPROVALS = 60
_TTL_DISCUSSIONS = 120
_TTL_ISSUE = 300
_TTL_WORK_ITEM = 300
_TTL_USERNAME = 3600

_WORK_ITEM_STATUS_QUERY = """\
query($projectPath: ID!, $iid: String!) {
    project(fullPath: $projectPath) {
        workItems(iids: [$iid]) {
            nodes {
                widgets {
                    type
                    ... on WorkItemWidgetStatus {
                        status { name }
                    }
                }
            }
        }
    }
}
"""


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


class GitLabAPI:
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

    def clear_response_cache(self) -> None:
        """Clear all cached API responses. Use before explicit sync operations."""
        self._response_cache.clear()

    def get_json(self, endpoint: str) -> dict[str, object] | list[dict[str, object]] | None:
        if not self.token:
            return None
        response = httpx.get(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers={"PRIVATE-TOKEN": self.token},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object] | list[dict[str, object]]", response.json())

    def post_json(self, endpoint: str, payload: dict[str, object] | None = None) -> dict[str, object] | None:
        if not self.token:
            return None
        response = httpx.post(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers={"PRIVATE-TOKEN": self.token},
            json=payload or {},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())

    def put_json(self, endpoint: str, payload: dict[str, object] | None = None) -> dict[str, object] | None:
        if not self.token:
            return None
        response = httpx.put(
            f"{self.base_url}/{endpoint.lstrip('/')}",
            headers={"PRIVATE-TOKEN": self.token},
            json=payload or {},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())

    def upload_file(self, project_id: int, filepath: str) -> dict[str, object] | None:
        if not self.token:
            return None
        with Path(filepath).open("rb") as f:
            response = httpx.post(
                f"{self.base_url}/projects/{project_id}/uploads",
                headers={"PRIVATE-TOKEN": self.token},
                files={"file": (Path(filepath).name, f)},
                timeout=30.0,
            )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())

    def graphql(self, query: str, variables: dict[str, object] | None = None) -> dict[str, object] | None:
        if not self.token:
            return None
        graphql_url = self.base_url.replace("/api/v4", "/api/graphql")
        response = httpx.post(
            graphql_url,
            headers={"PRIVATE-TOKEN": self.token},
            json={"query": query, "variables": variables or {}},
            timeout=10.0,
        )
        response.raise_for_status()
        return cast("dict[str, object]", response.json())

    def get_work_item_status(self, project_path: str, iid: int) -> str | None:
        """Fetch the Status widget value for a GitLab work item via GraphQL."""
        cache_key = f"work_item_status:{project_path}:{iid}"
        cached = self._get_cached(cache_key, _TTL_WORK_ITEM)
        if cached is not None:
            return cached  # type: ignore[return-value]
        data = self.graphql(_WORK_ITEM_STATUS_QUERY, {"projectPath": project_path, "iid": str(iid)})
        if not isinstance(data, dict):
            self._set_cached(cache_key, None)
            return None
        nodes = (
            data.get("data", {}).get("project", {}).get("workItems", {}).get("nodes", [])  # type: ignore[union-attr]
        )
        if not isinstance(nodes, list) or not nodes:
            self._set_cached(cache_key, None)
            return None
        widgets = nodes[0].get("widgets", [])
        if not isinstance(widgets, list):
            self._set_cached(cache_key, None)
            return None
        for widget in widgets:
            if isinstance(widget, dict) and widget.get("type") == "STATUS":
                status = widget.get("status")
                if isinstance(status, dict):
                    result = str(status.get("name", ""))
                    self._set_cached(cache_key, result)
                    return result
        self._set_cached(cache_key, None)
        return None

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
        from urllib.parse import urlencode  # noqa: PLC0415

        query: dict[str, str | int] = {
            "state": "opened",
            "author_username": author,
            "scope": "all",
            "per_page": per_page,
        }
        if updated_after:
            query["updated_after"] = updated_after
        params = urlencode(query)
        data = self.get_json(f"merge_requests?{params}")
        if not isinstance(data, list):
            return []
        if include_draft:
            return data
        return [mr for mr in data if not mr.get("draft")]

    def list_recently_merged_mrs(
        self,
        author: str,
        *,
        updated_after: str | None = None,
        per_page: int = 100,
    ) -> list[dict[str, object]]:
        """Fetch recently merged MRs authored by *author*.

        When *updated_after* is provided, only returns MRs updated after that
        timestamp (ISO 8601).  Otherwise returns the most recent *per_page*.
        """
        from urllib.parse import urlencode  # noqa: PLC0415

        query: dict[str, str | int] = {
            "state": "merged",
            "author_username": author,
            "scope": "all",
            "per_page": per_page,
            "order_by": "updated_at",
            "sort": "desc",
        }
        if updated_after:
            query["updated_after"] = updated_after
        params = urlencode(query)
        data = self.get_json(f"merge_requests?{params}")
        if not isinstance(data, list):
            return []
        return data

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
            result_val = {
                "count": count,
                "required": int(data.get("approvals_required", 1)),  # type: ignore[arg-type]
                "approved_by": names,
            }
            self._set_cached(cache_key, result_val)
            return result_val
        fallback = {"count": 0, "required": 1, "approved_by": []}
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
        data = self.get_json(f"projects/{project_id}/merge_requests/{mr_iid}/discussions?per_page=100")
        result = data if isinstance(data, list) else []
        self._set_cached(cache_key, result)
        return result

    def cancel_pipelines(
        self,
        project_id: int,
        ref: str,
        *,
        statuses: tuple[str, ...] = ("running", "pending"),
    ) -> list[int]:
        cancelled: list[int] = []
        for status in statuses:
            data = self.get_json(f"projects/{project_id}/pipelines?ref={ref}&status={status}&per_page=10")
            if not isinstance(data, list):
                continue
            for pipeline in data:
                pipeline_id = _as_int(pipeline["id"])
                self.post_json(f"projects/{project_id}/pipelines/{pipeline_id}/cancel")
                cancelled.append(pipeline_id)
        return cancelled

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
