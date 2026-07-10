"""Assignable-identity resolution for PR creation (#3100).

Both PR-creation chokepoints (``ShipExecutor._build_pr_spec`` and the
orphan-branch ``create_or_defer_pr``) used the host-token login unvalidated.
A pull-only PAT resolves to a login that is NOT assignable on the target
repo, and ``gh pr create --assignee <login>`` then fails the whole create.
The resolver validates assignability first and degrades to an unassigned
PR instead of a failed one.
"""

from teatree.core.backend_protocols import CodeHostBackend
from teatree.utils import git


def resolve_pr_assignee(host: CodeHostBackend, *, repo: str) -> str:
    candidate = host.current_user() or git.config_value(key="user.name")
    if not candidate:
        return ""
    return candidate if host.is_assignable(repo=repo, login=candidate) else ""
