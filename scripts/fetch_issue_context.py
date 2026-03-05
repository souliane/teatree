#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Fetch issue context: issue data + comments + embedded images.

Used by: t3-ticket (intake), t3-review (fetch ticket before review),
    t3-followup (pre-fetch context).

Output: JSON with issue metadata, description, comments, and downloaded image paths.
"""

import json
import re
import sys
import tempfile
from pathlib import Path

import typer

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.gitlab import download_file, get_issue, get_issue_comments, resolve_project

_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\((/uploads/[^\s)]+)\)")
_EXTERNAL_LINK_RE = re.compile(r"https?://(?:www\.)?(?:notion\.so|linear\.app|atlassian\.net|jira\.\w+)[^\s)>]+")


def _download_images(
    description: str,
    project_web_url: str,
    dest_dir: str,
) -> list[dict]:
    """Download embedded images from issue description. Returns list of {alt, url, path}."""
    downloaded = []
    for match in _IMAGE_RE.finditer(description):
        alt, upload_path = match.group(1), match.group(2)
        full_url = f"{project_web_url}{upload_path}"
        filename = Path(upload_path).name
        dest = str(Path(dest_dir) / filename)
        if download_file(full_url, dest):
            downloaded.append({"alt": alt, "url": full_url, "local_path": dest})
    return downloaded


def _extract_external_links(text: str) -> list[str]:
    """Extract external tracker links (Notion, Linear, Jira) from text."""
    return list(dict.fromkeys(_EXTERNAL_LINK_RE.findall(text)))


def fetch_context(
    issue_url: str,
    *,
    download_images: bool = True,
    image_dir: str = "",
) -> dict:
    """Fetch full issue context. Returns structured dict."""
    # Parse project path and issue IID from URL
    match = re.search(r"gitlab\.com/([^/]+/[^/]+)/-/(?:issues|work_items)/(\d+)", issue_url)
    if not match:
        return {"error": f"Could not parse issue URL: {issue_url}"}

    project_path, issue_iid = match.group(1), int(match.group(2))
    proj = resolve_project(project_path)
    if not proj:
        return {"error": f"Could not resolve project: {project_path}"}

    issue = get_issue(proj.project_id, issue_iid)
    if not issue:
        return {"error": f"Could not fetch issue #{issue_iid} from {project_path}"}

    description = issue.get("description", "") or ""
    comments = get_issue_comments(proj.project_id, issue_iid)

    # Extract external links from description and comments
    all_text = description + "\n" + "\n".join(c.get("body", "") for c in comments)
    external_links = _extract_external_links(all_text)

    # Download embedded images
    images: list[dict] = []
    if download_images and _IMAGE_RE.search(description):
        dest = image_dir or tempfile.mkdtemp(prefix="t3-issue-")
        project_web_url = issue.get("web_url", "").rsplit("/-/", 1)[0]
        images = _download_images(description, project_web_url, dest)

    # Extract labels
    labels = issue.get("labels", [])
    process_label = next((label for label in labels if label.startswith(("Process::", "Process:: "))), None)

    return {
        "url": issue.get("web_url", ""),
        "iid": issue_iid,
        "project_path": project_path,
        "project_id": proj.project_id,
        "title": issue.get("title", ""),
        "description": description,
        "labels": labels,
        "process_label": process_label,
        "assignees": [a.get("username", "") for a in issue.get("assignees", [])],
        "comments": [
            {
                "author": c.get("author", {}).get("username", ""),
                "body": c.get("body", ""),
                "created_at": c.get("created_at", ""),
            }
            for c in comments
        ],
        "external_links": external_links,
        "images": images,
    }


def main(
    issue_url: str = typer.Argument(..., help="GitLab issue URL"),
    no_images: bool = typer.Option(False, "--no-images", help="Skip image downloads"),
    image_dir: str = typer.Option("", "--image-dir", help="Directory for downloaded images"),
    output: str = typer.Option("", "--output", "-o", help="Write JSON to file instead of stdout"),
) -> None:
    """Fetch full issue context as JSON."""
    result = fetch_context(issue_url, download_images=not no_images, image_dir=image_dir)

    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        raise SystemExit(1)

    output_json = json.dumps(result, indent=2, ensure_ascii=False)
    if output:
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        Path(output).write_text(output_json + "\n", encoding="utf-8")
        print(f"Written to {output}")
    else:
        print(output_json)

    n_comments = len(result.get("comments", []))
    n_images = len(result.get("images", []))
    n_links = len(result.get("external_links", []))
    print(
        f"#{result['iid']}: {result['title'][:60]} — "
        f"{n_comments} comments, {n_images} images, {n_links} external links",
        file=sys.stderr,
    )


if __name__ == "__main__":
    typer.run(main)
