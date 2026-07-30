"""``t3 identities {seed,add,list,remove}`` — manage trusted identities (#1773).

The DB-backed :class:`TrustedIdentity` set is the canonical tier for "who is
the user" on a PUBLIC repo (BLUEPRINT §17.4). ``seed`` consolidates the
configured ``user_identity_aliases`` into the DB (the first concrete slice of
the config-to-DB direction); ``add`` / ``remove`` / ``list`` are upkeep. Core
carries no personal handle — the seed set comes from the operator's own config
(BLUEPRINT § 1: core stays generic).
"""

import logging
from typing import Annotated, TypedDict

import typer
from django_typer.management import TyperCommand, command
from rich.console import Console
from rich.table import Table

from teatree.core.models import TrustedIdentity

logger = logging.getLogger(__name__)

_PLATFORMS = frozenset(p.value for p in TrustedIdentity.Platform)


class AddResult(TypedDict):
    """Return shape of ``identities add`` — the row's key plus whether it was new."""

    platform: str
    handle: str
    created: bool


class Command(TyperCommand):
    @command()
    def seed(self) -> dict[str, int]:
        """Consolidate the configured ``user_identity_aliases`` into the DB (idempotent).

        Handles are inserted under the ``github`` platform by default; trust
        matching is platform-tolerant, so the platform is metadata only — use
        ``add gitlab <handle>`` to record a precise forge. Re-running inserts
        nothing new. Until ``seed`` runs, an empty table falls back to
        ``user_identity_aliases`` (the migration-window behaviour), so trust
        never regresses.
        """
        from teatree.config import get_effective_settings  # noqa: PLC0415 — deferred: keeps command import light

        aliases = [alias.strip() for alias in get_effective_settings().user_identity_aliases if alias.strip()]
        created = 0
        for handle in aliases:
            _, was_created = TrustedIdentity.objects.get_or_create(
                platform=TrustedIdentity.Platform.GITHUB,
                handle=handle,
                defaults={"note": "seeded from user_identity_aliases"},
            )
            created += int(was_created)
        self.stdout.write(f"Seeded {len(aliases)} trusted identities from config ({created} new).")
        return {"seeded": len(aliases), "created": created}

    @command()
    def add(
        self,
        platform: Annotated[str, typer.Argument(help="github | gitlab | slack | internal")],
        handle: Annotated[str, typer.Argument(help="The forge handle / login to trust.")],
        *,
        note: Annotated[str, typer.Option(help="Free-form upkeep note.")] = "",
    ) -> AddResult:
        """Add a trusted identity (idempotent on ``(platform, handle)``)."""
        normalized = platform.strip().lower()
        if normalized not in _PLATFORMS:
            self.stderr.write(f"Unknown platform {platform!r}; expected one of {sorted(_PLATFORMS)}.")
            raise SystemExit(1)
        cleaned = handle.strip()
        if not cleaned:
            self.stderr.write("handle must not be empty.")
            raise SystemExit(1)
        row, created = TrustedIdentity.objects.get_or_create(
            platform=normalized,
            handle=cleaned,
            defaults={"note": note},
        )
        verb = "added" if created else "already present"
        self.stdout.write(f"{verb}: {row}")
        return AddResult(platform=normalized, handle=cleaned, created=created)

    @command(name="list")
    def list_(self) -> list[dict[str, str]]:
        """List all trusted identities."""
        rows = list(TrustedIdentity.objects.all())
        table = Table("platform", "handle", "note", "created_at")
        for row in rows:
            table.add_row(row.platform, row.handle, row.note, row.created_at.isoformat())
        Console().print(table)
        return [{"platform": r.platform, "handle": r.handle, "note": r.note} for r in rows]

    @command()
    def remove(
        self,
        platform: Annotated[str, typer.Argument(help="github | gitlab | slack | internal")],
        handle: Annotated[str, typer.Argument(help="The forge handle / login to untrust.")],
    ) -> dict[str, int]:
        """Remove a trusted identity by ``(platform, handle)``."""
        deleted, _ = TrustedIdentity.objects.filter(platform=platform.strip().lower(), handle=handle.strip()).delete()
        self.stdout.write(f"Removed {deleted} trusted identity row(s) for {platform}:{handle}.")
        return {"removed": deleted}
