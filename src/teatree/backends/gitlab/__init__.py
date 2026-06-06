"""GitLab integration backend (API client, CI, sync, payloads).

Package facade re-exporting the cross-package public surface so callers import
from ``teatree.backends.gitlab`` while each symbol keeps an explicit defining
module (``client`` / ``api`` / ``sync`` …). ``mock.patch`` targets name the
defining submodule, never this facade.
"""

from teatree.backends.gitlab.client import GitLabCodeHost, get_client

__all__ = ["GitLabCodeHost", "get_client"]
