"""GitLab API helpers via glab CLI and direct REST API.

Provides batch-friendly operations: list MRs, get pipelines, approvals, issue labels.
All functions return plain dicts/lists — no side effects.

Used by: t3-followup (collect_followup_data), t3-ship (create_mr, cancel_pipelines),
    t3-ticket (fetch_issue_context), t3-review (fetch MR context).
"""

import json
import os
import re
import subprocess
from dataclasses import dataclass

_token_cache: str = ""


def _token() -> str:
    global _token_cache  # noqa: PLW0603
    if _token_cache:
        return _token_cache

    result = subprocess.run(
        ["pass", "show", "gitlab/pat"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        _token_cache = result.stdout.strip()
        return _token_cache

    result = subprocess.run(
        ["glab", "config", "get", "token", "--host", "gitlab.com"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        _token_cache = result.stdout.strip()
        return _token_cache

    _token_cache = os.environ.get("GITLAB_TOKEN", "")
    return _token_cache


def _api_get(endpoint: str, token: str = "") -> dict | list | None:
    """GET a GitLab API endpoint, return parsed JSON or None on error."""
    tok = token or _token()
    if not tok:
        return None

    result = subprocess.run(
        [
            "curl",
            "-sf",
            "--header",
            f"PRIVATE-TOKEN: {tok}",
            f"https://gitlab.com/api/v4/{endpoint}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def _api_post(endpoint: str, data: dict | None = None, token: str = "") -> dict | None:
    """POST to a GitLab API endpoint, return parsed JSON or None on error."""
    tok = token or _token()
    if not tok:
        return None

    cmd = [
        "curl",
        "-sf",
        "-X",
        "POST",
        "--header",
        f"PRIVATE-TOKEN: {tok}",
        "--header",
        "Content-Type: application/json",
    ]
    if data:
        cmd.extend(["--data", json.dumps(data)])
    cmd.append(f"https://gitlab.com/api/v4/{endpoint}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


def _api_put(endpoint: str, data: dict | None = None, token: str = "") -> dict | None:
    """PUT to a GitLab API endpoint."""
    tok = token or _token()
    if not tok:
        return None

    cmd = [
        "curl",
        "-sf",
        "-X",
        "PUT",
        "--header",
        f"PRIVATE-TOKEN: {tok}",
        "--header",
        "Content-Type: application/json",
    ]
    if data:
        cmd.extend(["--data", json.dumps(data)])
    cmd.append(f"https://gitlab.com/api/v4/{endpoint}")

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0 or not result.stdout.strip():
        return None
    return json.loads(result.stdout)


# ---------------------------------------------------------------------------
# User / Project resolution
# ---------------------------------------------------------------------------


def current_user() -> str:
    """Return the authenticated GitLab username."""
    result = subprocess.run(
        ["glab", "auth", "status"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stderr.splitlines() + result.stdout.splitlines():
        if "Logged in" in line and "as " in line:
            parts = line.split("as ", 1)
            if len(parts) > 1:
                return parts[1].split()[0].strip()
    return ""


@dataclass
class ProjectInfo:
    project_id: int
    path_with_namespace: str
    short_name: str


_project_cache: dict[str, ProjectInfo] = {}


def resolve_project(repo_path: str, token: str = "") -> ProjectInfo | None:
    """Resolve a repo path (e.g. 'my-org/my-project') to its project ID."""
    if repo_path in _project_cache:
        return _project_cache[repo_path]

    encoded = repo_path.replace("/", "%2F")
    data = _api_get(f"projects/{encoded}", token)
    if not data or not isinstance(data, dict):
        return None

    info = ProjectInfo(
        project_id=data["id"],
        path_with_namespace=data["path_with_namespace"],
        short_name=data["path"],
    )
    _project_cache[repo_path] = info
    return info


def resolve_project_from_remote(repo_dir: str = ".", token: str = "") -> ProjectInfo | None:
    """Resolve project from a local git repo's remote URL."""
    result = subprocess.run(
        ["git", "-C", repo_dir, "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return None
    url = result.stdout.strip()
    # git@gitlab.com:org/repo.git or https://gitlab.com/org/repo.git
    match = re.search(r"gitlab\.com[:/](.+?)(?:\.git)?$", url)
    if not match:
        return None
    return resolve_project(match.group(1), token)


# ---------------------------------------------------------------------------
# MR operations
# ---------------------------------------------------------------------------


def list_open_mrs(
    repo_path: str,
    author: str,
    *,
    token: str = "",
    include_draft: bool = True,
    per_page: int = 50,
) -> list[dict]:
    """List open MRs authored by user in a repo. Returns raw API dicts."""
    proj = resolve_project(repo_path, token)
    if not proj:
        return []

    endpoint = f"projects/{proj.project_id}/merge_requests?state=opened&author_username={author}&per_page={per_page}"
    data = _api_get(endpoint, token)
    if not data or not isinstance(data, list):
        return []

    if not include_draft:
        return [mr for mr in data if not mr.get("draft")]
    return data


def get_mr(project_id: int, mr_iid: int, token: str = "") -> dict | None:
    """Get full MR data."""
    data = _api_get(f"projects/{project_id}/merge_requests/{mr_iid}", token)
    if data and isinstance(data, dict):
        return data
    return None


def get_mr_approvals(project_id: int, mr_iid: int, token: str = "") -> dict:
    """Get approval info for an MR. Returns {count, required, approved_by}."""
    data = _api_get(f"projects/{project_id}/merge_requests/{mr_iid}/approvals", token)
    if not data or not isinstance(data, dict):
        return {"count": 0, "required": 0, "approved_by": []}
    return {
        "count": len(data.get("approved_by", [])),
        "required": data.get("approvals_required", 0),
        "approved_by": [a.get("user", {}).get("username", "") for a in data.get("approved_by", [])],
    }


def get_mr_notes(  # noqa: PLR0913
    project_id: int,
    mr_iid: int,
    *,
    token: str = "",
    exclude_system: bool = True,
    exclude_author: str = "",
    per_page: int = 20,
) -> list[dict]:
    """Get MR discussion notes. Optionally exclude system notes and self-comments."""
    data = _api_get(
        f"projects/{project_id}/merge_requests/{mr_iid}/notes?per_page={per_page}&sort=desc",
        token,
    )
    if not data or not isinstance(data, list):
        return []

    results = []
    for note in data:
        if exclude_system and note.get("system"):
            continue
        if exclude_author and note.get("author", {}).get("username") == exclude_author:
            continue
        results.append(note)
    return results


def get_mr_pipeline(project_id: int, mr_iid: int, token: str = "") -> dict:
    """Get the head pipeline for an MR. Returns {status, url}."""
    data = _api_get(f"projects/{project_id}/merge_requests/{mr_iid}", token)
    if not data or not isinstance(data, dict):
        return {"status": None, "url": None}
    pipeline = data.get("head_pipeline") or {}
    return {"status": pipeline.get("status"), "url": pipeline.get("web_url")}


def get_mr_state(project_id: int, mr_iid: int, token: str = "") -> dict | None:
    """Get MR state (open/merged/closed) and merge commit SHA."""
    data = _api_get(f"projects/{project_id}/merge_requests/{mr_iid}", token)
    if not data or not isinstance(data, dict):
        return None
    return {
        "state": data.get("state"),
        "merge_commit_sha": data.get("merge_commit_sha"),
    }


def create_mr(  # noqa: PLR0913
    project_id: int,
    source_branch: str,
    target_branch: str,
    title: str,
    description: str = "",
    *,
    assignee_username: str = "",
    labels: list[str] | None = None,
    squash: bool = True,
    token: str = "",
) -> dict | None:
    """Create a merge request. Returns the created MR dict or None."""
    payload: dict = {
        "source_branch": source_branch,
        "target_branch": target_branch,
        "title": title,
        "squash": squash,
    }
    if description:
        payload["description"] = description
    if labels:
        payload["labels"] = ",".join(labels)
    if assignee_username:
        # Resolve username to user ID
        users = _api_get(f"users?username={assignee_username}", token)
        if users and isinstance(users, list) and users:
            payload["assignee_id"] = users[0]["id"]

    return _api_post(f"projects/{project_id}/merge_requests", payload, token)


# ---------------------------------------------------------------------------
# Pipeline operations
# ---------------------------------------------------------------------------


def cancel_pipelines(
    project_id: int,
    ref: str,
    *,
    token: str = "",
    statuses: tuple[str, ...] = ("running", "pending"),
) -> list[int]:
    """Cancel running/pending pipelines for a ref. Returns list of cancelled IDs."""
    cancelled: list[int] = []
    for status in statuses:
        data = _api_get(
            f"projects/{project_id}/pipelines?ref={ref}&status={status}&per_page=10",
            token,
        )
        if not data or not isinstance(data, list):
            continue
        for pipeline in data:
            pid = pipeline.get("id")
            if pid:
                _api_post(f"projects/{project_id}/pipelines/{pid}/cancel", token=token)
                cancelled.append(pid)
    return cancelled


# ---------------------------------------------------------------------------
# Issue operations
# ---------------------------------------------------------------------------


def get_mr_closing_issues(project_id: int, mr_iid: int, token: str = "") -> list[dict]:
    """Get issues that will be closed when the MR is merged."""
    data = _api_get(f"projects/{project_id}/merge_requests/{mr_iid}/closes_issues", token)
    if not data or not isinstance(data, list):
        return []
    return data


def get_issue(project_id: int | str, issue_iid: int, token: str = "") -> dict | None:
    """Get full issue data."""
    data = _api_get(f"projects/{project_id}/issues/{issue_iid}", token)
    if not data or not isinstance(data, dict):
        return None
    return data


def get_issue_labels(project_id: int, issue_iid: int, token: str = "") -> list[str]:
    """Get labels for an issue."""
    issue = get_issue(project_id, issue_iid, token)
    return issue.get("labels", []) if issue else []


def get_issue_comments(project_id: int, issue_iid: int, token: str = "", per_page: int = 50) -> list[dict]:
    """Get issue comments/notes."""
    data = _api_get(
        f"projects/{project_id}/issues/{issue_iid}/notes?per_page={per_page}&sort=asc",
        token,
    )
    if not data or not isinstance(data, list):
        return []
    return [n for n in data if not n.get("system")]


def update_issue_labels(
    project_id: int,
    issue_iid: int,
    *,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    token: str = "",
) -> dict | None:
    """Add/remove labels on an issue."""
    payload: dict = {}
    if add_labels:
        payload["add_labels"] = ",".join(add_labels)
    if remove_labels:
        payload["remove_labels"] = ",".join(remove_labels)
    if not payload:
        return None
    return _api_put(f"projects/{project_id}/issues/{issue_iid}", payload, token)


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------


def download_file(url: str, dest: str, token: str = "") -> bool:
    """Download a file from GitLab (handles private URLs). Returns True on success."""
    tok = token or _token()
    cmd = ["curl", "-sfL", "-o", dest]
    if tok:
        cmd.extend(["--header", f"PRIVATE-TOKEN: {tok}"])
    cmd.append(url)
    result = subprocess.run(cmd, check=False)
    return result.returncode == 0


def discover_mrs(
    repos: list[str],
    username: str,
    *,
    token: str = "",
    include_draft: bool = True,
    verbose: bool = False,
) -> list[dict]:
    """Discover all open MRs across repos, annotated with project metadata.

    Each returned dict has extra keys: _repo_path, _repo_short, _project_id.
    Shared by: collect_followup_data.py, review_request.py.
    """
    import sys

    all_mrs: list[dict] = []
    for repo_path in repos:
        proj = resolve_project(repo_path, token)
        if not proj:
            if verbose:
                print(f"  SKIP {repo_path} (could not resolve project)", file=sys.stderr)
            continue
        mrs = list_open_mrs(repo_path, username, token=token, include_draft=include_draft)
        for mr in mrs:
            mr["_repo_path"] = repo_path
            mr["_repo_short"] = proj.short_name
            mr["_project_id"] = proj.project_id
        all_mrs.extend(mrs)
        if verbose:
            print(f"  {repo_path}: {len(mrs)} open MRs")
    return all_mrs


def current_branch(repo_dir: str = ".") -> str:
    """Get current git branch name."""
    result = subprocess.run(
        ["git", "-C", repo_dir, "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() if result.returncode == 0 else ""
