"""Assignable-identity resolution for PR creation (#3100).

Both PR-creation chokepoints (``ShipExecutor._build_pr_spec`` and the
orphan-branch ``create_or_defer_pr``) resolved the assignee as the raw
host-token login, unvalidated. A pull-only PAT resolves to a login that is
NOT assignable on the target repo, and ``gh pr create --assignee <login>``
then fails the whole create.

The resolver consults the trusted-identities registry (#1773) first — the
sanctioned forge handles — then falls back to the host-token login, and
returns the first candidate that is actually assignable on the repo. When
none is assignable it degrades to an unassigned PR (``""``) instead of a
failed create.
"""

from django.apps import apps
from django.db import OperationalError, ProgrammingError

from teatree.core.backend_protocols import CodeHostBackend


def resolve_pr_assignee(host: CodeHostBackend, *, repo: str) -> str:
    for candidate in _assignee_candidates(host):
        if host.is_assignable(repo=repo, login=candidate):
            return candidate
    return ""


def _assignee_candidates(host: CodeHostBackend) -> list[str]:
    seen: set[str] = set()
    candidates: list[str] = []
    for handle in (*_registry_handles(), host.current_user()):
        cleaned = handle.strip()
        key = cleaned.lower()
        if cleaned and key not in seen:
            seen.add(key)
            candidates.append(cleaned)
    return candidates


def _registry_handles() -> list[str]:
    try:
        trusted_identity = apps.get_model("core", "TrustedIdentity")
        return trusted_identity.objects.ordered_handles()
    except (OperationalError, ProgrammingError, RuntimeError):
        # Pre-migration / DB-unreachable window (sibling of author_trust's
        # tolerance): the registry is a preference layer, never load-bearing for
        # opening the PR, so fall through to the host-token login.
        return []
