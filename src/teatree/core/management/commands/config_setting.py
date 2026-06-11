"""``t3 <overlay> config-setting`` — set/clear/list the DB config override tier.

The ORM-touching admin path for the ``ConfigSetting`` store (#1775, the first
slice of "move config to the database"). Mirrors the per-worktree env command
shape: a django_typer ``TyperCommand`` whose subcommands write to the
authoritative source (the DB), never a file.

The pilot is scoped to keys registered in ``OVERLAY_OVERRIDABLE_SETTINGS`` — the
same registry the resolver's DB tier consults — so an admin cannot stash a row
the resolver would silently ignore. The ``value`` is parsed as JSON, so a bool
kill-switch (``true``/``false``), a string (``'"ready"'``), an int (``3``), or a
list (``'["a","b"]'``) all round-trip into the store.

Non-zero exits use ``raise SystemExit(N)`` — this runs under Django's
``call_command``; ``typer.Exit`` is the wrong primitive on that path.
"""

import json
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS
from teatree.core.models import ConfigSetting


class Command(TyperCommand):
    @command()
    def set(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name (must be overridable).")],
        value: Annotated[str, typer.Argument(help="JSON value, e.g. true / false / '\"x\"' / 3.")],
    ) -> None:
        """Upsert the DB override row for *key* to the JSON-parsed *value*.

        Refuses a key not in ``OVERLAY_OVERRIDABLE_SETTINGS`` and a *value* that
        is not valid JSON, leaving the store untouched on either error.
        """
        if key not in OVERLAY_OVERRIDABLE_SETTINGS:
            self.stderr.write(f"  refusing: {key!r} is not an overridable setting (#1775 pilot scope)")
            raise SystemExit(2)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            self.stderr.write(f"  invalid JSON value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        ConfigSetting.objects.set_value(key, parsed)
        # Verify-by-re-read: report the stored value the resolver will now see.
        stored = ConfigSetting.objects.get_effective(key)
        self.stdout.write(f"  set {key} = {stored!r}")

    @command()
    def clear(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name whose DB override to remove.")],
    ) -> None:
        """Delete the DB override row for *key*; falls back to the file/env source.

        Exits non-zero when no row exists so a typo'd key is loud, not silent.
        """
        if ConfigSetting.objects.clear(key):
            self.stdout.write(f"  cleared DB override for {key}")
            return
        self.stderr.write(f"  no DB override row for {key}")
        raise SystemExit(1)

    @command(name="list")
    def list_rows(self) -> None:
        """List every DB config override row (read-only)."""
        rows = list(ConfigSetting.objects.all())
        if not rows:
            self.stdout.write("  (no DB config overrides)")
            return
        for row in rows:
            self.stdout.write(f"  {row.key} = {row.value!r}")
