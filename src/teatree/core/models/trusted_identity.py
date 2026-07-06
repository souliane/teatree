"""DB-backed trusted-identity set — the user's known forge handles (#1773).

On a PUBLIC repo, anyone who is not the user is a potential malicious actor,
so the merge keystone and the reviewing scanners must classify a PR author as
trusted-vs-untrusted. The user owns MULTIPLE identities across forges (two
GitHub logins plus a GitLab username), and ALL of them count as "the user".

This is the durable, queryable single source of truth for that set — the same
"canonical tier is the DB" pattern :class:`MergeClear` / :class:`DbApproval`
follow (BLUEPRINT §17.4.2). One row per ``(platform, handle)``.

Resolution during the migration window. DB rows are the canonical tier first.
An EMPTY table (pre-seed) falls back to ``user_identity_aliases`` so an install
whose data migration has not seeded yet does not regress. A pre-migration
database error (``no such table`` / ``relation does not exist``) also falls back
to config — the sibling of :class:`SlackBroadcastsScanner`'s pre-migration
tolerance.

The config-fallback path lives in :mod:`teatree.core.review.author_trust`, which is
the shared classifier the four reviewing scanners and the merge keystone all
consume so they cannot drift.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class TrustedIdentityManager(models.Manager["TrustedIdentity"]):
    """Trust queries — case-insensitive, platform-tolerant (#1773)."""

    def is_trusted(self, handle: str, platform: str = "") -> bool:
        """True iff *handle* is a trusted identity (case-insensitive, platform-tolerant).

        A handle trusted on ANY platform is trusted: the user's handles are
        unique enough across forges that a cross-platform match is still the
        user, and a caller that cannot resolve the platform must still get a
        verdict. ``platform`` narrows the match to that forge when supplied AND
        a matching row exists there; otherwise it falls back to any platform's
        matching handle (tolerant, never stricter than the bare-handle check).
        """
        cleaned = handle.strip()
        if not cleaned:
            return False
        rows = self.filter(handle__iexact=cleaned)
        if platform.strip() and rows.filter(platform__iexact=platform.strip()).exists():
            return True
        return rows.exists()

    def trusted_handles(self) -> set[str]:
        """The union of every trusted handle (lower-cased) for scanner wiring."""
        return {h.strip().lower() for h in self.values_list("handle", flat=True) if h.strip()}


class TrustedIdentity(models.Model):
    """One trusted forge handle owned by the user (#1773).

    Rows are uniquely keyed on ``(platform, handle)`` so the same handle can
    legitimately exist on more than one forge. ``note`` is free-form upkeep
    metadata (e.g. "primary GitHub login"). ``created_at`` records when the
    handle was added so a later audit can see when trust was granted.
    """

    class Platform(models.TextChoices):
        GITHUB = "github", "GitHub"
        GITLAB = "gitlab", "GitLab"
        SLACK = "slack", "Slack"
        INTERNAL = "internal", "Internal"

    platform = models.CharField(max_length=16, choices=Platform.choices)
    handle = models.CharField(max_length=128)
    note = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[TrustedIdentityManager] = TrustedIdentityManager()

    class Meta:
        db_table = "teatree_trusted_identity"
        ordering: ClassVar = ["platform", "handle"]
        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=["platform", "handle"],
                name="uniq_trustedidentity_platform_handle",
            ),
        ]

    def __str__(self) -> str:
        return f"trusted-identity<{self.platform}:{self.handle}>"
