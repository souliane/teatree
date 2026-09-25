"""GitHub App installation state, poll watermarks, and pre-ingestion rejections (#4795).

Three models support the GitHub App webhook transport with polling fallback:

* :class:`GitHubAppInstallation` — one row per installed App instance. Tracks
    which repositories the poller and the installation-sync handler may act
    on, split into :attr:`~GitHubAppInstallation.active_repositories` (operator-
    confirmed) and :attr:`~GitHubAppInstallation.pending_repositories`
    (GitHub-reported additions awaiting confirmation) — a repository GitHub adds
    to the installation never silently broadens what the poller reads.
* :class:`GitHubPollCursor` — the poller's per-``(installation, repo,
    event_family)`` watermark, so a poll tick asks GitHub for only what is new
    since the last one.
* :class:`WebhookRejection` — a request the webhook view refused BEFORE it
    became an :class:`~teatree.core.models.incoming_event.IncomingEvent` (bad
    signature, oversized body, no secret configured, a stale replay, a
    duplicate). Deliberately NOT a second event ledger: an accepted delivery
    still goes through ``IncomingEvent`` alone; this table exists only for the
    requests that never got that far, which the metrics AC needs a count of.
"""

from datetime import datetime, timedelta
from typing import ClassVar

from django.db import models
from django.utils import timezone

from teatree.core.models.incoming_event import IncomingEvent


class GitHubAppInstallation(models.Model):
    """One installed GitHub App instance, and which of its repos are trusted."""

    class RepositorySelection(models.TextChoices):
        ALL = "all", "All repositories"
        SELECTED = "selected", "Selected repositories"

    installation_id = models.BigIntegerField(unique=True)
    app_id = models.BigIntegerField()
    account_login = models.CharField(max_length=255)
    account_type = models.CharField(max_length=32, blank=True, default="")
    repository_selection = models.CharField(
        max_length=16,
        choices=RepositorySelection.choices,
        default=RepositorySelection.SELECTED,
    )
    # Operator-confirmed repos the poller and installation-sync handler may act
    # on, vs. GitHub-reported additions awaiting confirmation — the anti-
    # broadening split (see module docstring).
    active_repositories = models.JSONField(default=list, blank=True)
    pending_repositories = models.JSONField(default=list, blank=True)
    permissions = models.JSONField(default=dict, blank=True)
    events = models.JSONField(default=list, blank=True)
    overlay = models.CharField(max_length=255, blank=True, default="")
    suspended_at = models.DateTimeField(null=True, blank=True)
    last_verified_delivery_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teatree_github_app_installation"
        ordering: ClassVar = ["account_login"]

    def __str__(self) -> str:
        return f"github-app-installation<{self.installation_id}:{self.account_login}>"

    @property
    def is_suspended(self) -> bool:
        return self.suspended_at is not None

    def record_selection(self, *, added: list[str], removed: list[str]) -> None:
        """Apply a GitHub ``installation_repositories`` delta without broadening access.

        *added* repos land in :attr:`pending_repositories` — never
        :attr:`active_repositories` directly, however the App's own
        ``repository_selection`` reads. *removed* repos are dropped from BOTH
        sets: an operator's earlier confirmation does not survive GitHub
        revoking the repo from the installation.
        """
        active = {r for r in self.active_repositories if isinstance(r, str)}
        pending = {r for r in self.pending_repositories if isinstance(r, str)}
        pending |= {r for r in added if r not in active}
        removed_set = set(removed)
        active -= removed_set
        pending -= removed_set
        self.active_repositories = sorted(active)
        self.pending_repositories = sorted(pending)
        self.save(update_fields=["active_repositories", "pending_repositories", "updated_at"])

    def confirm_repositories(self, repos: list[str] | None = None) -> list[str]:
        """Move *repos* (default: every pending repo) from pending to active; return what moved.

        An operator action (the ``confirm-repos`` CLI) — the one path that ever
        grows :attr:`active_repositories`. A name absent from
        :attr:`pending_repositories` is silently ignored rather than raising, so
        a stale/retried confirmation is a no-op, not an error.
        """
        pending = {r for r in self.pending_repositories if isinstance(r, str)}
        # ``set(pending)`` copies: ``wanted`` must be a distinct object from ``pending``
        # below, or ``pending -= wanted`` (in-place ``difference_update``) empties both.
        wanted = set(pending) if repos is None else pending & set(repos)
        if not wanted:
            return []
        active = {r for r in self.active_repositories if isinstance(r, str)}
        active |= wanted
        pending -= wanted
        self.active_repositories = sorted(active)
        self.pending_repositories = sorted(pending)
        self.save(update_fields=["active_repositories", "pending_repositories", "updated_at"])
        return sorted(wanted)

    def mark_verified_delivery(self, *, at: datetime | None = None) -> None:
        """Record a successfully-processed webhook delivery for this installation."""
        self.last_verified_delivery_at = at or timezone.now()
        self.save(update_fields=["last_verified_delivery_at", "updated_at"])

    def has_recent_verified_delivery(self, *, within: timedelta, at: datetime | None = None) -> bool:
        """True iff a verified delivery landed within *within* of *at* (default now).

        The fail-closed gate ``set-preset webhook`` consults before writing the
        preset: selecting ``webhook`` with no recently-verified delivery would
        cut over to a transport nobody has proven reachable.
        """
        if self.last_verified_delivery_at is None:
            return False
        moment = at or timezone.now()
        return moment - self.last_verified_delivery_at <= within


class GitHubPollCursor(models.Model):
    """The poller's per-``(installation, repo, event_family)`` watermark."""

    installation = models.ForeignKey(GitHubAppInstallation, on_delete=models.CASCADE, related_name="poll_cursors")
    repo_full_name = models.CharField(max_length=255)
    event_family = models.CharField(max_length=64)
    cursor_value = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "teatree_github_poll_cursor"
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["installation", "repo_full_name", "event_family"],
                name="uniq_githubpollcursor_installation_repo_family",
            ),
        ]

    def __str__(self) -> str:
        return f"github-poll-cursor<{self.repo_full_name}:{self.event_family}={self.cursor_value!r}>"

    @classmethod
    def cursor_for(cls, *, installation: GitHubAppInstallation, repo_full_name: str, event_family: str) -> str:
        """The stored watermark, or ``""`` when this ``(repo, family)`` has never been polled."""
        row = cls.objects.filter(
            installation=installation, repo_full_name=repo_full_name, event_family=event_family
        ).first()
        return row.cursor_value if row is not None else ""

    @classmethod
    def advance(
        cls, *, installation: GitHubAppInstallation, repo_full_name: str, event_family: str, cursor_value: str
    ) -> "GitHubPollCursor":
        """Upsert the watermark for ``(installation, repo, event_family)`` to *cursor_value*."""
        row, created = cls.objects.get_or_create(
            installation=installation,
            repo_full_name=repo_full_name,
            event_family=event_family,
            defaults={"cursor_value": cursor_value},
        )
        if not created and row.cursor_value != cursor_value:
            row.cursor_value = cursor_value
            row.save(update_fields=["cursor_value", "updated_at"])
        return row


class WebhookRejection(models.Model):
    """A GitHub webhook request refused before it became an ``IncomingEvent``."""

    class Reason(models.TextChoices):
        SIGNATURE_INVALID = "signature_invalid", "Signature invalid"
        OVERSIZED = "oversized", "Payload too large"
        NO_SECRET = "no_secret", "No secret configured"
        RATE_LIMITED = "rate_limited", "Rate limited"
        STALE_REPLAY = "stale_replay", "Stale replay"
        DUPLICATE_SUPPRESSED = "duplicate_suppressed", "Duplicate suppressed"

    source = models.CharField(max_length=16, choices=IncomingEvent.Source.choices)
    reason = models.CharField(max_length=32, choices=Reason.choices)
    delivery_id = models.CharField(max_length=255, blank=True, default="")
    detail = models.TextField(blank=True, default="")
    occurred_at = models.DateTimeField(default=timezone.now)

    class Meta:
        db_table = "teatree_webhook_rejection"
        ordering: ClassVar = ["-occurred_at"]
        indexes: ClassVar = [models.Index(fields=["source", "occurred_at"])]

    def __str__(self) -> str:
        return f"webhook-rejection<{self.source}:{self.reason} {self.delivery_id!r}>"

    @classmethod
    def record(cls, *, source: str, reason: str, delivery_id: str = "", detail: str = "") -> "WebhookRejection":
        return cls.objects.create(source=source, reason=reason, delivery_id=delivery_id, detail=detail)


__all__ = ["GitHubAppInstallation", "GitHubPollCursor", "WebhookRejection"]
