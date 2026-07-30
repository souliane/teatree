r"""The sticky per-overlay Anthropic account pick, keyed by ``(kind, scope)``.

The routing selector (``teatree.credential_config``) is STICKY: once it picks an
account (``pass`` entry) for a given credential *kind* (``oauth`` / ``api_key``) and
overlay *scope*, it keeps using that pick while the account's health-cache row stays
fresh and non-exhausted — re-selecting only on expiry / exhaustion. This one-row-per
``(kind, scope)`` pointer persists that pick so the selection survives across
processes; it is deliberately separate from the health cache
(``AnthropicTokenUsage``, keyed by ``pass_path``) because stickiness is a property of
``(kind, scope)`` — a fallback can make one account the sticky pick of several
overlays at once.
"""

from typing import ClassVar

from django.db import models
from django.utils import timezone


class AnthropicActivePickManager(models.Manager["AnthropicActivePick"]):
    """Read/upsert the sticky pick for a ``(kind, scope)`` pair."""

    def pick_for(self, kind: str, scope: str) -> str | None:
        """The currently-pinned ``pass_path`` for *kind* in *scope*, or ``None``."""
        row = self.filter(kind=kind, scope=scope).first()
        return row.pass_path if row is not None else None

    def set_pick(self, kind: str, scope: str, pass_path: str) -> "AnthropicActivePick":
        """Pin *pass_path* as the sticky pick for *kind* in *scope* (idempotent upsert)."""
        row, _ = self.update_or_create(
            kind=kind,
            scope=scope,
            defaults={"pass_path": pass_path, "updated_at": timezone.now()},
        )
        return row


class AnthropicActivePick(models.Model):
    """One sticky ``(kind, scope) -> pass_path`` routing pick.

    The unique ``(kind, scope)`` pair is the pointer key: the selector reuses this
    pick while its health row is fresh and non-exhausted, and overwrites it when it
    re-selects.
    """

    kind = models.CharField(max_length=32)
    scope = models.CharField(max_length=255, blank=True, default="")
    pass_path = models.CharField(max_length=255)
    updated_at = models.DateTimeField(default=timezone.now)

    objects: ClassVar[AnthropicActivePickManager] = AnthropicActivePickManager()

    class Meta:
        db_table = "teatree_anthropic_active_pick"
        ordering: ClassVar = ["kind", "scope"]
        constraints: ClassVar = [
            models.UniqueConstraint(fields=["kind", "scope"], name="uniq_anthropic_active_pick_kind_scope"),
        ]

    def __str__(self) -> str:
        where = "global" if not self.scope else f"overlay:{self.scope}"
        return f"anthropic-active-pick<{self.kind} {where} -> {self.pass_path}>"
