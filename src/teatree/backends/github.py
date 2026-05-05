"""GitHub backend — code host and project board sync via ``gh`` CLI."""

import json
import os
from dataclasses import dataclass
from typing import TypedDict, cast
from urllib.parse import quote_plus

from teatree.backends.protocols import PullRequestSpec
from teatree.core.sync import RawAPIDict
from teatree.utils import git
from teatree.utils.run import CompletedProcess, run_checked


class _GitHubUser(TypedDict, total=False):
    """Subset of the GitHub ``/user`` response that teatree reads."""

    login: str


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
        return {"url": result.stdout.strip()}

    def current_user(self) -> str:
        """Return the authenticated GitHub login (e.g. ``souliane``)."""
        data = _gh_api_get("user", token=self._token)
        if not isinstance(data, dict):
            return ""
        user = cast("_GitHubUser", data)
        return user.get("login", "")

    def list_open_prs(self, repo: str, author: str) -> list[dict[str, object]]:
        data = _gh_api_get(f"repos/{repo}/pulls?state=open&per_page=100", token=self._token)
        if not isinstance(data, list):
            return []
        return cast(
            "list[dict[str, object]]",
            [pr for pr in data if isinstance(pr, dict) and pr.get("user", {}).get("login") == author],  # type: ignore[union-attr]
        )

    def list_my_open_prs(self, author: str) -> list[RawAPIDict]:
        query = quote_plus(f"is:pr is:open author:{author}")
        data = _gh_api_get(f"search/issues?q={query}&per_page=100", token=self._token)
        if not isinstance(data, dict):
            return []
        items = cast("RawAPIDict", data).get("items")
        if not isinstance(items, list):
            return []
        return cast("list[RawAPIDict]", items)

    def post_mr_note(self, *, repo: str, mr_iid: int, body: str) -> dict[str, object]:
        data = _gh_api_post(
            f"repos/{repo}/issues/{mr_iid}/comments",
            {"body": body},
            token=self._token,
        )
        return cast("dict[str, object]", data) if isinstance(data, dict) else {}

    def update_mr_note(self, *, repo: str, mr_iid: int, note_id: int, body: str) -> dict[str, object]:
        _ = mr_iid  # GitHub comment IDs are globally unique
        data = _gh_api_patch(
            f"repos/{repo}/issues/comments/{note_id}",
            {"body": body},
            token=self._token,
        )
        return cast("dict[str, object]", data) if isinstance(data, dict) else {}

    def list_mr_notes(self, *, repo: str, mr_iid: int) -> list[dict[str, object]]:
        data = _gh_api_get(f"repos/{repo}/issues/{mr_iid}/comments", token=self._token)
        return cast("list[dict[str, object]]", data) if isinstance(data, list) else []

    def upload_file(self, *, repo: str, filepath: str) -> dict[str, object]:
        msg = f"File upload to {repo} not supported (token={'set' if self._token else 'unset'}, file={filepath})"
        raise NotImplementedError(msg)
