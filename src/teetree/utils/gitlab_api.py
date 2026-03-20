import os
import re
import subprocess
from dataclasses import dataclass
from typing import SupportsInt, cast

import httpx

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


class GitLabAPI:
    def __init__(self, *, token: str = "", base_url: str = "https://gitlab.com/api/v4") -> None:
        self.token = token or os.environ.get("GITLAB_TOKEN", "")
        self.base_url = base_url.rstrip("/")
        self._project_cache: dict[str, ProjectInfo] = {}

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
        data = self.graphql(_WORK_ITEM_STATUS_QUERY, {"projectPath": project_path, "iid": str(iid)})
        if not isinstance(data, dict):
            return None
        nodes = (
            data.get("data", {}).get("project", {}).get("workItems", {}).get("nodes", [])  # type: ignore[union-attr]
        )
        if not isinstance(nodes, list) or not nodes:
            return None
        widgets = nodes[0].get("widgets", [])
        if not isinstance(widgets, list):
            return None
        for widget in widgets:
            if isinstance(widget, dict) and widget.get("type") == "STATUS":
                status = widget.get("status")
                if isinstance(status, dict):
                    return str(status.get("name", ""))
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
        result = subprocess.run(
            ["git", "-C", repo_dir, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return None

        remote = result.stdout.strip()
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
        data = self.get_json(f"projects/{project_id}/merge_requests/{mr_iid}/pipelines?per_page=1")
        if isinstance(data, list) and data:
            pipeline = data[0]
            return {
                "status": str(pipeline.get("status", "")),
                "url": str(pipeline.get("web_url", "")),
            }
        return {"status": None, "url": None}

    def get_mr_approvals(self, project_id: int, mr_iid: int) -> dict[str, object]:
        """Return approval count, required count, and approver names for an MR."""
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
            return {
                "count": count,
                "required": int(data.get("approvals_required", 1)),  # type: ignore[arg-type]
                "approved_by": names,
            }
        return {"count": 0, "required": 1, "approved_by": []}

    def get_issue(self, project_id: int, issue_iid: int) -> dict[str, object] | None:
        """Fetch a single issue by project ID and IID."""
        data = self.get_json(f"projects/{project_id}/issues/{issue_iid}")
        if isinstance(data, dict):
            return data
        return None

    def get_mr_discussions(self, project_id: int, mr_iid: int) -> list[dict[str, object]]:
        """Fetch all discussion threads for a merge request."""
        data = self.get_json(f"projects/{project_id}/merge_requests/{mr_iid}/discussions?per_page=100")
        if isinstance(data, list):
            return data
        return []

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
        data = self.get_json("user")
        if isinstance(data, dict):
            return str(data.get("username", ""))
        return ""

    @staticmethod
    def current_branch(repo_dir: str = ".") -> str:
        result = subprocess.run(
            ["git", "-C", repo_dir, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
