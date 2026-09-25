"""The GitHub App manifest — deterministic, minimal-permission, no secrets (#4795).

Every requested permission maps to a NAMED teatree capability
(:data:`CAPABILITY_FOR_PERMISSION`) — least-privilege by construction: adding a
permission here without adding its capability entry fails
``tests/teatree_backends/github/test_app_manifest.py``. The manifest itself
carries no secret — GitHub mints ``id`` / ``pem`` / ``webhook_secret`` /
``client_secret`` only on the conversion callback
(:mod:`teatree.backends.github.app_registration`), never here.
"""

from typing import TypedDict

#: permission name -> access level GitHub's manifest schema expects.
DEFAULT_PERMISSIONS: dict[str, str] = {
    "metadata": "read",
    "contents": "read",
    "pull_requests": "write",
    "issues": "write",
    "checks": "read",
}

#: permission name -> the teatree capability it exists to enable. Every key in
#: :data:`DEFAULT_PERMISSIONS` MUST have an entry here (pinned by test).
CAPABILITY_FOR_PERMISSION: dict[str, str] = {
    "metadata": "baseline repository discovery — required by every App installation",
    "contents": "read pushed commits for the poll-driven push-event fallback",
    "pull_requests": "read/normalize pull-request and review events into the shared receiver",
    "issues": "read/normalize issue and issue-comment events into the shared receiver",
    "checks": "read CI/workflow check-run and check-suite status into the shared receiver",
}

#: The event families the App subscribes to — exactly the ones
#: :mod:`teatree.core.github_app.event_identity` and
#: :mod:`teatree.core.github_app.webhook_normalize` know how to process, plus the
#: two installation-lifecycle events every App must handle regardless of scope.
DEFAULT_EVENTS: tuple[str, ...] = (
    "pull_request",
    "pull_request_review",
    "issues",
    "issue_comment",
    "check_run",
    "check_suite",
    "push",
    "installation",
    "installation_repositories",
)


class Manifest(TypedDict):
    name: str
    url: str
    hook_attributes: dict[str, str]
    redirect_url: str
    public: bool
    default_permissions: dict[str, str]
    default_events: list[str]


def build_manifest(*, name: str, url: str, webhook_url: str, redirect_url: str = "") -> Manifest:
    """A deterministic App-creation manifest for *name* — no secrets, reviewable as JSON.

    Same inputs always produce the same output (byte-identical dict), so an
    operator can diff a re-generated manifest against what they registered.
    """
    return Manifest(
        name=name,
        url=url,
        hook_attributes={"url": webhook_url},
        redirect_url=redirect_url or url,
        public=False,
        default_permissions=dict(DEFAULT_PERMISSIONS),
        default_events=list(DEFAULT_EVENTS),
    )


__all__ = ["CAPABILITY_FOR_PERMISSION", "DEFAULT_EVENTS", "DEFAULT_PERMISSIONS", "Manifest", "build_manifest"]
