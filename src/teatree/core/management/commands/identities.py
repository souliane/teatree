"""``t3 identities {seed,bootstrap,add,list,remove}`` — manage trusted identities (#1773).

The DB-backed :class:`TrustedIdentity` set is the canonical tier for "who is
the user" on a PUBLIC repo (BLUEPRINT §17.4). ``seed`` consolidates the
configured ``user_identity_aliases`` into the DB (the first concrete slice of
the config-to-DB direction); ``add`` / ``remove`` / ``list`` are upkeep. Core
carries no personal handle — the seed set comes from the operator's own config
(BLUEPRINT § 1: core stays generic).
"""

import logging
from typing import IO, Annotated, TypedDict, cast

import typer
from django_typer.management import TyperCommand, command
from rich.console import Console
from rich.table import Table

from teatree.core.identity_wiring import derivable_owner_identities
from teatree.core.machine_output import emit
from teatree.core.models import ConfigSetting, TrustedIdentity
from teatree.core.overlay_loader import get_overlay

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
    def bootstrap(
        self,
        *,
        json_output: Annotated[
            bool,
            typer.Option("--json", help="Emit the derived identities as JSON on stdout instead of the human view."),
        ] = False,
    ) -> None:
        """Derive ``user_identity_aliases`` from the forge logins this venue authenticates as.

        The keystone's reviewer allowlist is the UNION of ``user_identity_aliases`` and
        ``independent_reviewer_identities``, so deriving the owner's own handles admits the human
        who records a CLEAR AND un-degrades the other consumers reading the same list — one
        derived fact rather than two hand-typed lists that can disagree.

        Refuses rather than writing when every resolvable login is one this deployment declares in
        ``self_forge_identities``: that is a venue authenticated as its own bot, and writing it
        would admit a coding agent's identity as an independent reviewer.
        """
        from teatree.backends.loader import get_code_hosts  # noqa: PLC0415 — deferred: keeps command import light
        from teatree.config import cold_reader, get_effective_settings  # noqa: PLC0415 — deferred: same

        logins = [host.current_user() for host in get_code_hosts(get_overlay())]
        declared = cold_reader.mapping_setting("self_forge_identities")
        self_identities = [
            entry
            for logins_for_host in declared.values()
            if isinstance(logins_for_host, list)
            for entry in logins_for_host
            if isinstance(entry, str)
        ]
        derived = derivable_owner_identities(forge_logins=logins, self_identities=self_identities)
        if not derived:
            self.stderr.write(
                "Refusing to bootstrap: no forge login resolved that this deployment does not also act as "
                f"(resolved {sorted(filter(None, logins))!r}, declared as our own {sorted(self_identities)!r}). "
                "Set the owner's handles by hand with "
                "`t3 <overlay> config_setting set user_identity_aliases '[\"<handle>\"]'`."
            )
            raise SystemExit(1)

        existing = list(get_effective_settings().user_identity_aliases)
        merged = existing + [handle for handle in derived if handle not in existing]
        ConfigSetting.objects.set_value("user_identity_aliases", merged)

        self.print_result = False
        emit(
            {"derived": list(derived), "user_identity_aliases": merged},
            json_output=json_output,
            out=cast("IO[str]", self.stdout),
            err=cast("IO[str]", self.stderr),
            human=f"user_identity_aliases = {merged!r} (derived {list(derived)!r}).",
        )

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
