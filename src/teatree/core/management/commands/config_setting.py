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

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS, get_effective_settings, load_config
from teatree.core.models import ConfigSetting


class Command(TyperCommand):
    @command()
    def set(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name (must be overridable).")],
        value: Annotated[str, typer.Argument(help="JSON value, e.g. true / false / '\"x\"' / 3.")],
    ) -> None:
        """Upsert the DB override row for *key* to the JSON-parsed *value*.

        Refuses a key not in ``OVERLAY_OVERRIDABLE_SETTINGS``, a *value* that is
        not valid JSON, and a *value* that JSON-parses but is invalid for the
        setting's type, leaving the store untouched on any error.

        The type check runs the **same** registry parser the resolver applies on
        read (#258): an out-of-enum ``mode`` or a quoted ``"false"`` for a
        bool-typed setting is rejected here, at WRITE time, so a value that would
        raise on every later config resolution can never be stored. Validating
        on write is what keeps a bad row from bricking all reads.
        """
        if key not in OVERLAY_OVERRIDABLE_SETTINGS:
            self.stderr.write(f"  refusing: {key!r} is not an overridable setting (#1775 pilot scope)")
            raise SystemExit(2)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            self.stderr.write(f"  invalid JSON value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        parser = OVERLAY_OVERRIDABLE_SETTINGS[key]
        try:
            canonical = parser(parsed)
        except (ValueError, TypeError, AttributeError) as exc:
            self.stderr.write(f"  invalid value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        # Persist the CANONICAL parsed value, not the raw user value, so the DB
        # row and the read-time coercion agree (#258): a numeric string ``"5"``
        # is stored as the int ``5`` and an upper-case enum ``"AUTO"`` as the
        # normalised ``"auto"``. Every registry parser returns a JSON-storable
        # type — scalar, list, or a ``StrEnum`` (which a ``JSONField`` persists as
        # its string value) — so the parsed value round-trips through the store
        # and the read tier re-coerces it to the same value.
        ConfigSetting.objects.set_value(key, canonical)
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

    @command()
    def get(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name to read (must be overridable).")],
    ) -> None:
        """Print the resolved value for *key* and name its source (DB vs file/env).

        The read side of the dual-read store: when a ``ConfigSetting`` row exists
        it is reported as the ``db`` source; otherwise the value falls through to
        the file/env layer and is reported as the ``file/env`` source. Refuses a
        key not in ``OVERLAY_OVERRIDABLE_SETTINGS`` so a typo is loud, not a
        silent ``file/env`` answer for a non-setting.
        """
        if key not in OVERLAY_OVERRIDABLE_SETTINGS:
            self.stderr.write(f"  refusing: {key!r} is not an overridable setting (#1775 pilot scope)")
            raise SystemExit(2)
        stored = ConfigSetting.objects.get_effective(key)
        if stored is not None:
            self.stdout.write(f"  {key} = {stored!r}  [source: db]")
            return
        fallback = getattr(get_effective_settings(), key, None)
        self.stdout.write(f"  {key} = {fallback!r}  [source: file/env]")

    @command(name="import")
    def import_toml(self) -> None:
        """Seed the DB store from the operational ``[teatree]`` toml keys (one-time migration).

        The dual-read migration step (#938): every ``[teatree]`` key that is a
        registered ``OVERLAY_OVERRIDABLE_SETTINGS`` field is coerced through that
        registry's parser and upserted into the store, so existing installs move
        their operational config into the DB. Bootstrap-file-only keys
        (``private_repos`` / ``DATABASE_URL`` / …) and unknown keys are skipped —
        only operational settings move. The upsert makes a re-run idempotent.
        """
        teatree_table = load_config().raw.get("teatree", {})
        if not isinstance(teatree_table, dict):
            self.stdout.write("  (no [teatree] table to import)")
            return
        imported = 0
        for key, raw_value in teatree_table.items():
            parser = OVERLAY_OVERRIDABLE_SETTINGS.get(key)
            if parser is None:
                continue
            try:
                canonical = parser(raw_value)
            except (ValueError, TypeError, AttributeError) as exc:
                self.stderr.write(f"  skipping {key!r}: invalid value {raw_value!r}: {exc}")
                continue
            ConfigSetting.objects.set_value(key, canonical)
            imported += 1
            self.stdout.write(f"  imported {key} = {ConfigSetting.objects.get_effective(key)!r}")
        self.stdout.write(f"  imported {imported} operational setting(s) into the DB store")
