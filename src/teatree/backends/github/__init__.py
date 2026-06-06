"""GitHub integration backend (API client, projects, sync, claims, payloads).

Package facade re-exporting the cross-package public surface so callers import
from ``teatree.backends.github`` while each symbol keeps an explicit defining
module (``client`` / ``projects`` / ``sync`` …). ``mock.patch`` targets name the
defining submodule, never this facade.
"""

from teatree.backends.github.client import GitHubCodeHost, issue_repo_short
from teatree.backends.github.projects import ProjectItem, fetch_project_items

__all__ = ["GitHubCodeHost", "ProjectItem", "fetch_project_items", "issue_repo_short"]
