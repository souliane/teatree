#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Collect followup data from GitLab into followup.json.

Replaces ~15 manual API calls with a single script invocation.
Handles: MR discovery, pipeline status, approvals, issue labels, cache cleanup.

The agent only needs to:
1. Run this script (1 Bash call)
2. Read the output JSON (1 Read call)
3. Do reasoning-heavy parts: transition logic, Slack searches, user-facing summary
"""

import json
import os
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import typer

# Ensure scripts/ is on path for lib imports
_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.gitlab import (
    current_user,
    discover_mrs,
    get_issue,
    get_mr_approvals,
    get_mr_notes,
    get_mr_pipeline,
    resolve_project,
)

_DEFAULT_DATA_DIR = Path.home() / ".local/share/teatree"

_DEFAULT_REPOS: list[str] = []

_REVIEW_CHANNELS: dict[str, str] = {}


def _data_dir() -> Path:
    return Path(os.environ.get("T3_DATA_DIR") or str(_DEFAULT_DATA_DIR))


def _repos() -> list[str]:
    env_repos = os.environ.get("T3_FOLLOWUP_REPOS", "")
    if env_repos:
        return [r.strip() for r in env_repos.split(",") if r.strip()]
    return _DEFAULT_REPOS


def _review_channels() -> dict[str, str]:
    env_channels = os.environ.get("T3_REVIEW_CHANNELS", "")
    if env_channels:
        return dict(pair.split("=", 1) for pair in env_channels.split(",") if "=" in pair)
    return _REVIEW_CHANNELS


def _extract_ticket_from_branch(branch: str) -> str | None:
    match = re.search(r"(\d+)", branch)
    return match.group(1) if match else None


def _extract_ticket_from_mr(mr: dict) -> str | None:
    desc = mr.get("description", "") or ""
    first_line = desc.split("\n")[0] if desc else ""
    url_match = re.search(r"/-/(?:issues|work_items)/(\d+)", first_line)
    if url_match:
        return url_match.group(1)
    return _extract_ticket_from_branch(mr.get("source_branch", ""))


def _extract_ticket_url_from_mr(mr: dict) -> str | None:
    for text in [mr.get("title", ""), (mr.get("description", "") or "").split("\n")[0]]:
        match = re.search(r"(https://gitlab\.com/[^\s)]+/-/(?:issues|work_items)/\d+)", text)
        if match:
            return match.group(1)
    return None


def _extract_feature_flag(title: str) -> str | None:
    match = re.search(r"\[([^\]]+)\]", title)
    if match:
        flag = match.group(1)
        return flag if flag != "none" else None
    return None


def _process_label(labels: list[str]) -> str | None:
    for label in labels:
        if label.startswith(("Process::", "Process:: ")):
            return label
    return None


def _api_get_mr(project_id: int, mr_iid: int, token: str = "") -> dict | None:
    from lib.gitlab import _api_get

    data = _api_get(f"projects/{project_id}/merge_requests/{mr_iid}", token)
    if data and isinstance(data, dict):
        return data
    return None


def _discover_mrs(repos: list[str], username: str, token: str, *, verbose: bool) -> list[dict]:
    """Phase 1: discover all open MRs across repos."""
    return discover_mrs(repos, username, token=token, verbose=verbose)


def _enrich_mr(
    mr: dict,
    username: str,
    token: str,
    existing_mrs: dict,
) -> tuple[str, dict, bool]:
    """Enrich a single MR with pipeline, approvals, comments. Returns (key, entry, is_draft)."""
    repo_short = mr["_repo_short"]
    iid = mr["iid"]
    project_id = mr["_project_id"]
    mr_key = f"{repo_short}!{iid}"
    is_draft = mr.get("draft", False)

    if not is_draft:
        pipeline_info = get_mr_pipeline(project_id, iid, token)
        approvals = get_mr_approvals(project_id, iid, token)
        colleague_comments = get_mr_notes(
            project_id,
            iid,
            token=token,
            exclude_author=username,
            per_page=5,
        )
        entry = _build_active_mr_entry(mr, pipeline_info, approvals, colleague_comments, existing_mrs)
    else:
        entry = {
            "url": mr.get("web_url", ""),
            "repo": repo_short,
            "title": re.sub(r"^Draft:\s*", "", mr.get("title", "")),
            "pipeline_status": None,
            "pipeline_url": None,
        }

    return mr_key, entry, is_draft


def _build_active_mr_entry(
    mr: dict,
    pipeline_info: dict,
    approvals: dict,
    colleague_comments: list,
    existing_mrs: dict,
) -> dict:
    repo_short = mr["_repo_short"]
    mr_key = f"{repo_short}!{mr['iid']}"
    prev = existing_mrs.get(mr_key, {})

    return {
        "url": mr.get("web_url", ""),
        "repo": repo_short,
        "project_id": mr["_project_id"],
        "title": mr.get("title", ""),
        "branch": mr.get("source_branch", ""),
        "ticket": _extract_ticket_from_mr(mr),
        "pipeline_status": pipeline_info["status"],
        "pipeline_url": pipeline_info["url"],
        "review_requested": prev.get("review_requested", False),
        "review_channel": _review_channels().get(repo_short, ""),
        "review_permalink": prev.get("review_permalink"),
        "review_comments": prev.get("review_comments"),
        "e2e_test_plan_url": prev.get("e2e_test_plan_url"),
        "approvals": {"count": approvals["count"], "required": approvals["required"]},
        "has_colleague_comments": len(colleague_comments) > 0,
        "skipped": prev.get("skipped", False),
        "skip_reason": prev.get("skip_reason"),
    }


def _build_ticket_entry(
    ticket_iid: str,
    ticket_url: str | None,
    feature_flag: str | None,
    existing_tickets: dict,
) -> dict:
    prev = existing_tickets.get(ticket_iid, {})
    preserved = {
        k: v
        for k, v in prev.items()
        if k not in {"title", "url", "tracker_status", "gitlab_status", "notion_status", "feature_flag", "mrs"}
    }
    return {
        "title": prev.get("title", ""),
        "url": ticket_url or prev.get("url"),
        "tracker_status": None,
        "feature_flag": feature_flag or prev.get("feature_flag"),
        "mrs": [],
        **preserved,
    }


def _fetch_issue_labels(tickets: dict, token: str, *, verbose: bool) -> None:
    """Phase 3: fetch issue labels for each ticket."""
    for ticket_iid, ticket_data in tickets.items():
        ticket_url = ticket_data.get("url")
        if not ticket_url:
            continue

        url_match = re.search(r"gitlab\.com/([^/]+/[^/]+)/-/", ticket_url)
        if not url_match:
            continue

        proj = resolve_project(url_match.group(1), token)
        if not proj:
            continue

        issue = get_issue(proj.project_id, int(ticket_iid), token)
        if issue:
            ticket_data["tracker_status"] = _process_label(issue.get("labels", []))
            if not ticket_data["title"]:
                ticket_data["title"] = issue.get("title", "")
            if not ticket_data["url"]:  # pragma: no cover — defensive; url is truthy if we got here
                ticket_data["url"] = issue.get("web_url")
        if verbose:
            print(f"  #{ticket_iid}: {ticket_data.get('tracker_status', 'no label')}")


def _detect_merged(existing_mrs: dict, active_keys: set[str], token: str, *, verbose: bool) -> list[str]:
    """Phase 4: detect merged MRs from previous cache."""
    merged: list[str] = []
    for mr_key, prev_mr in existing_mrs.items():
        if mr_key in active_keys:
            continue
        iid_match = re.search(r"!(\d+)$", mr_key)
        project_id = prev_mr.get("project_id")
        if not iid_match or not project_id:
            continue

        mr_api = _api_get_mr(project_id, int(iid_match.group(1)), token)
        if mr_api and mr_api.get("state") == "merged":
            merged.append(mr_key)
            if verbose:
                print(f"  MERGED: {mr_key}")
    return merged


def _clean_review_tracking(
    existing_tracking: dict,
    active_mrs: dict,
    existing_mrs: dict,
    token: str,
    *,
    verbose: bool,
) -> dict:
    """Phase 5: remove merged MRs from review_comments_tracking."""
    result = {}
    for mr_key, tracking in existing_tracking.items():
        iid_match = re.search(r"!(\d+)$", mr_key)
        if not iid_match:
            result[mr_key] = tracking
            continue

        pid = (active_mrs.get(mr_key) or existing_mrs.get(mr_key, {})).get("project_id")
        if pid:
            mr_api = _api_get_mr(pid, int(iid_match.group(1)), token)
            if mr_api and mr_api.get("state") == "merged":
                if verbose:
                    print(f"  MERGED (review tracking): {mr_key}")
                continue

        result[mr_key] = tracking
    return result


def collect(*, verbose: bool = False) -> dict:  # noqa: C901, PLR0912, PLR0914, PLR0915
    """Collect all followup data and return the full JSON structure."""
    data_dir = _data_dir()
    data_dir.mkdir(parents=True, exist_ok=True)

    followup_path = data_dir / "followup.json"
    existing: dict = {}
    if followup_path.is_file():
        existing = json.loads(followup_path.read_text(encoding="utf-8"))

    existing_tickets = existing.get("tickets", {})
    existing_mrs = existing.get("mrs", {})
    existing_review_tracking = existing.get("review_comments_tracking", {})

    username = current_user()
    if not username:
        print("ERROR: Could not detect GitLab username", file=sys.stderr)
        raise SystemExit(1)

    if verbose:
        print(f"User: {username}")

    token = ""
    all_mrs = _discover_mrs(_repos(), username, token, verbose=verbose)

    # Phase 2: enrich each MR
    tickets: dict[str, dict] = {}
    mrs_data: dict[str, dict] = {}
    draft_mrs: dict[str, dict] = {}

    for mr in all_mrs:
        mr_key, entry, is_draft = _enrich_mr(mr, username, token, existing_mrs)

        if is_draft:
            draft_mrs[mr_key] = entry
        else:
            mrs_data[mr_key] = entry

        # Build ticket entries for non-draft MRs
        ticket_iid = _extract_ticket_from_mr(mr)
        if ticket_iid and not is_draft:
            if ticket_iid not in tickets:
                tickets[ticket_iid] = _build_ticket_entry(
                    ticket_iid,
                    _extract_ticket_url_from_mr(mr),
                    _extract_feature_flag(mr.get("title", "")),
                    existing_tickets,
                )
            if mr_key not in tickets[ticket_iid]["mrs"]:
                tickets[ticket_iid]["mrs"].append(mr_key)
            flag = _extract_feature_flag(mr.get("title", ""))
            if flag and not tickets[ticket_iid].get("feature_flag"):
                tickets[ticket_iid]["feature_flag"] = flag

        if verbose:
            status_str = f"pipeline={entry.get('pipeline_status')}" if not is_draft else "draft"
            print(f"  {mr_key}: {status_str}")

    # Phase 3: issue labels
    _fetch_issue_labels(tickets, token, verbose=verbose)

    # Phase 4: detect merged
    active_keys = set(mrs_data) | set(draft_mrs)
    merged_mrs = _detect_merged(existing_mrs, active_keys, token, verbose=verbose)

    # Phase 5: clean review tracking
    updated_review_tracking = _clean_review_tracking(
        existing_review_tracking,
        mrs_data,
        existing_mrs,
        token,
        verbose=verbose,
    )

    # Phase 6: build actions log
    actions_log: list[str] = []
    for mr_key in merged_mrs:
        ticket_id = existing_mrs.get(mr_key, {}).get("ticket")
        if ticket_id and ticket_id in tickets:
            mr_list = tickets[ticket_id].get("mrs", [])
            if mr_key in mr_list:  # pragma: no cover — defensive; merged keys excluded from active
                mr_list.remove(mr_key)
            if not mr_list:  # pragma: no cover
                actions_log.append(f"Ticket #{ticket_id}: all MRs merged")
        actions_log.append(f"Merged: {mr_key}")

    return {
        "generated_at": datetime.now(tz=UTC).isoformat(),
        "tickets": tickets,
        "mrs": mrs_data,
        "review_comments_tracking": updated_review_tracking,
        "draft_mrs": draft_mrs,
        "actions_log": actions_log,
    }


def main(
    output: Path | None = typer.Option(None, "--output", "-o", help="Output path"),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Print progress"),
    dry_run: bool = typer.Option(False, "--dry-run", "-n", help="Print JSON to stdout"),
) -> None:
    """Collect followup data from GitLab into followup.json."""
    data = collect(verbose=verbose)

    output_json = json.dumps(data, indent=2, ensure_ascii=False)

    if dry_run:
        print(output_json)
        return

    out_path = output or (_data_dir() / "followup.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(output_json + "\n", encoding="utf-8")

    n_tickets = len(data["tickets"])
    n_mrs = len(data["mrs"])
    n_drafts = len(data["draft_mrs"])
    n_merged = sum(1 for a in data["actions_log"] if a.startswith("Merged:"))
    print(f"Collected: {n_tickets} tickets, {n_mrs} active MRs, {n_drafts} drafts, {n_merged} newly merged")

    if data["actions_log"]:
        print("Actions:")
        for action in data["actions_log"]:
            print(f"  - {action}")

    print(f"Written to {out_path}")


if __name__ == "__main__":
    typer.run(main)
