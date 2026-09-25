"""Handle ``installation`` / ``installation_repositories`` webhook events (#4795).

Upserts the local :class:`~teatree.core.models.github_app.GitHubAppInstallation`
row so the poller and CLI have a durable view of what the App is installed on —
without ever silently broadening what the poller or CLI may act on: a
GitHub-reported repository addition always lands in
``pending_repositories``, never ``active_repositories`` (see
:meth:`~teatree.core.models.github_app.GitHubAppInstallation.record_selection`).
"""

from django.utils import timezone

from teatree.core.models import GitHubAppInstallation
from teatree.types import RawAPIDict


def handle(event_type: str, payload: RawAPIDict) -> GitHubAppInstallation | None:
    """Apply an ``installation``/``installation_repositories`` event; return the row touched."""
    if event_type == "installation":
        return _handle_installation(payload)
    if event_type == "installation_repositories":
        return _handle_installation_repositories(payload)
    return None


def _handle_installation(payload: RawAPIDict) -> GitHubAppInstallation | None:
    installation = _dict(payload, "installation")
    installation_id = _int(installation, "id")
    if not installation_id:
        return None
    action = _str(payload, "action")
    account = _dict(installation, "account")
    row, _created = GitHubAppInstallation.objects.get_or_create(
        installation_id=installation_id,
        defaults={
            "app_id": _int(installation, "app_id"),
            "account_login": _str(account, "login"),
            "account_type": _str(account, "type"),
            "repository_selection": _str(installation, "repository_selection") or "selected",
            "permissions": _dict(installation, "permissions"),
            "events": _list(installation, "events"),
        },
    )
    repo_names = [_str(r, "full_name") for r in _list(payload, "repositories") if isinstance(r, dict)]
    repo_names = [name for name in repo_names if name]
    if action == "deleted":
        row.suspended_at = row.suspended_at or timezone.now()
        row.save(update_fields=["suspended_at", "updated_at"])
    elif action == "suspend":
        row.suspended_at = timezone.now()
        row.save(update_fields=["suspended_at", "updated_at"])
    elif action in {"unsuspend", "new_permissions_accepted"}:
        row.suspended_at = None
        row.save(update_fields=["suspended_at", "updated_at"])
    elif action == "created" and repo_names:
        row.record_selection(added=repo_names, removed=[])
    return row


def _handle_installation_repositories(payload: RawAPIDict) -> GitHubAppInstallation | None:
    installation = _dict(payload, "installation")
    installation_id = _int(installation, "id")
    if not installation_id:
        return None
    row = GitHubAppInstallation.objects.filter(installation_id=installation_id).first()
    if row is None:
        return None
    added = [_str(r, "full_name") for r in _list(payload, "repositories_added") if isinstance(r, dict)]
    removed = [_str(r, "full_name") for r in _list(payload, "repositories_removed") if isinstance(r, dict)]
    row.record_selection(added=[n for n in added if n], removed=[n for n in removed if n])
    return row


def _dict(data: RawAPIDict, key: str) -> RawAPIDict:
    value = data.get(key)
    return value if isinstance(value, dict) else {}


def _list(data: RawAPIDict, key: str) -> list[object]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _str(data: RawAPIDict, key: str) -> str:
    value = data.get(key)
    return value if isinstance(value, str) else ""


def _int(data: RawAPIDict, key: str) -> int:
    value = data.get(key)
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


__all__ = ["handle"]
