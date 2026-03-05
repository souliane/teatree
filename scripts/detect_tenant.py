#!/usr/bin/env -S uv run --script
# /// script
# dependencies = [
#   "typer>=0.12",
# ]
# ///
"""Auto-detect tenant/customer from issue labels, description, or external tracker.

Priority chain:
1. Explicit --tenant argument
2. Issue labels (customer-specific labels)
3. Issue description (customer name mentions)
4. Extension point: wt_detect_variant

Used by: t3-ticket (scope analysis), t3-code (feature flag check).
"""

import re
import sys
from pathlib import Path

import typer

_scripts_dir = str(Path(__file__).resolve().parent)
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from lib.gitlab import get_issue, resolve_project


def _detect_from_labels(labels: list[str], known_tenants: list[str]) -> str | None:
    """Match issue labels against known tenant names (case-insensitive)."""
    label_set = {lab.lower() for lab in labels}
    for tenant in known_tenants:
        if tenant.lower() in label_set:
            return tenant
    return None


def _detect_from_description(description: str, known_tenants: list[str]) -> str | None:
    """Search issue description for tenant name mentions."""
    desc_lower = description.lower()
    for tenant in known_tenants:
        if tenant.lower() in desc_lower:
            return tenant
    return None


def detect(
    issue_url: str = "",
    *,
    explicit: str = "",
    known_tenants: list[str] | None = None,
) -> dict:
    """Detect tenant. Returns {tenant, source, confidence}."""
    if explicit:
        return {"tenant": explicit, "source": "explicit", "confidence": "high"}

    if not known_tenants:
        known_tenants = _default_tenants()

    issue = _fetch_issue(issue_url)
    if isinstance(issue, dict) and "source" in issue:
        return issue

    labels = issue.get("labels", [])
    if tenant := _detect_from_labels(labels, known_tenants):
        return {"tenant": tenant, "source": "label", "confidence": "high"}

    description = issue.get("description", "") or ""
    if tenant := _detect_from_description(description, known_tenants):
        return {"tenant": tenant, "source": "description", "confidence": "medium"}

    return {"tenant": "", "source": "not_found", "confidence": "none"}


def _fetch_issue(issue_url: str) -> dict:
    """Fetch issue from URL. Returns issue dict or error result dict."""
    if not issue_url:
        return {"tenant": "", "source": "none", "confidence": "none"}

    match = re.search(r"gitlab\.com/([^/]+/[^/]+)/-/(?:issues|work_items)/(\d+)", issue_url)
    if not match:
        return {"tenant": "", "source": "parse_error", "confidence": "none"}

    proj = resolve_project(match.group(1))
    if not proj:
        return {"tenant": "", "source": "project_error", "confidence": "none"}

    issue = get_issue(proj.project_id, int(match.group(2)))
    if not issue:
        return {"tenant": "", "source": "issue_error", "confidence": "none"}

    return issue


def _default_tenants() -> list[str]:
    """Load known tenants from T3_KNOWN_TENANTS env or return empty list."""
    import os

    env = os.environ.get("T3_KNOWN_TENANTS", "")
    if env:
        return [t.strip() for t in env.split(",") if t.strip()]
    return []


def main(
    issue_url: str = typer.Argument("", help="GitLab issue URL"),
    tenant: str = typer.Option("", "--tenant", "-t", help="Explicit tenant override"),
    tenants: str = typer.Option("", "--tenants", help="Comma-separated known tenant names"),
) -> None:
    """Detect tenant from issue context."""
    known = [t.strip() for t in tenants.split(",") if t.strip()] if tenants else None
    result = detect(issue_url, explicit=tenant, known_tenants=known)

    if result["tenant"]:
        print(f"{result['tenant']} (source: {result['source']}, confidence: {result['confidence']})")
    else:
        print(f"No tenant detected (source: {result['source']})", file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    typer.run(main)
