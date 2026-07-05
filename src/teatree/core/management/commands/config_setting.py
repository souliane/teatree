"""``t3 <overlay> config_setting`` — set/clear/list the DB config override tier.

The ORM-touching admin path for the ``ConfigSetting`` store (#1775, the first
slice of "move config to the database"). Mirrors the per-worktree env command
shape: a django_typer ``TyperCommand`` whose subcommands write to the
authoritative source (the DB), never a file.

The pilot is scoped to keys registered in ``OVERLAY_OVERRIDABLE_SETTINGS`` (the
``UserSettings`` partition the resolver's DB tier consults) plus the
``REGISTRY_SETTINGS`` keys (``overlays`` / ``e2e_repos`` — the non-``UserSettings``
registries ``loader._inject_db_registries`` injects into ``config.raw``), so an admin
cannot stash a row no reader would consult. The ``value`` is parsed as JSON, so a bool
kill-switch (``true``/``false``), a string (``'"ready"'``), an int (``3``), or a
list (``'["a","b"]'``) all round-trip into the store.

Every write/read subcommand takes ``--overlay <name>``: omitted (the default) it
addresses the GLOBAL scope (every overlay, the original #1775 behaviour); with a
name it addresses that overlay's scope alone — the DB twin of the
``[overlays.<name>]`` TOML override. The resolver layers global rows then the
active overlay's rows on top, so an overlay-scoped row beats a global one.

Non-zero exits use ``raise SystemExit(N)`` — this runs under Django's
``call_command``; ``typer.Exit`` is the wrong primitive on that path.
"""

import json
from pathlib import Path
from typing import Annotated

import typer
from django_typer.management import TyperCommand, command

from teatree.config import FEATURE_FLAGS, OVERLAY_OVERRIDABLE_SETTINGS, get_effective_settings, load_config
from teatree.config.feature_flags import flag_trailer, render_flags_audit
from teatree.config.registries import REGISTRY_SETTINGS
from teatree.core.config_migration import export_db_to_toml, import_toml_into_db
from teatree.core.models import ConfigSetting

_OverlayOption = Annotated[
    str,
    typer.Option("--overlay", help="Overlay name to scope the row to; omit for the global scope (every overlay)."),
]


def _scope_label(scope: str) -> str:
    """Human label for a row's scope: ``global`` for the empty scope else ``overlay '<name>'``."""
    return "global" if not scope else f"overlay {scope!r}"


def _flag_suffix(key: str) -> str:
    """A leading-space ``[feature flag, …]`` governance trailer for *key*, or ``""``.

    So an operator flipping a governed, lifecycle-staged toggle sees it is a flag —
    not a durable setting — and where its removal is tracked, without a second lookup.
    """
    trailer = flag_trailer(key)
    return f"  {trailer}" if trailer else ""


class Command(TyperCommand):
    @command()
    def set(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name (must be overridable).")],
        value: Annotated[str, typer.Argument(help="JSON value, e.g. true / false / '\"x\"' / 3.")],
        overlay: _OverlayOption = "",
    ) -> None:
        """Upsert the DB override row for *key* (in *overlay*'s scope or global) to *value*.

        Refuses a key in neither ``OVERLAY_OVERRIDABLE_SETTINGS`` nor
        ``REGISTRY_SETTINGS``, a *value* that is not valid JSON, and a *value* that
        JSON-parses but is invalid for the setting's type, leaving the store
        untouched on any error.

        ``--overlay <name>`` scopes the row to one overlay (the DB twin of a
        per-overlay TOML override); omitted, it writes the global scope.

        The type check runs the **same** registry parser the resolver applies on
        read (#258): an out-of-enum ``mode`` or a quoted ``"false"`` for a
        bool-typed setting is rejected here, at WRITE time, so a value that would
        raise on every later config resolution can never be stored. Validating
        on write is what keeps a bad row from bricking all reads.
        """
        allowed = {**OVERLAY_OVERRIDABLE_SETTINGS, **REGISTRY_SETTINGS}
        if key not in allowed:
            self.stderr.write(f"  refusing: {key!r} is not an overridable setting (#1775 pilot scope)")
            raise SystemExit(2)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            self.stderr.write(f"  invalid JSON value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        parser = allowed[key]
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
        ConfigSetting.objects.set_value(key, canonical, scope=overlay)
        # Verify-by-re-read: report the stored value the resolver will now see.
        stored = ConfigSetting.objects.get_effective(key, scope=overlay)
        self.stdout.write(f"  set {key} = {stored!r}  [{_scope_label(overlay)}]{_flag_suffix(key)}")

    @command()
    def clear(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name whose DB override to remove.")],
        overlay: _OverlayOption = "",
    ) -> None:
        """Delete the DB override row for *key* in *overlay*'s scope (or global).

        After clearing, the setting falls back through the remaining tiers (an
        overlay-scoped clear falls back to the global DB row / file / env). Exits
        non-zero when no row exists in that scope so a typo'd key is loud, not
        silent.
        """
        if ConfigSetting.objects.clear(key, scope=overlay):
            self.stdout.write(f"  cleared DB override for {key}  [{_scope_label(overlay)}]")
            return
        self.stderr.write(f"  no DB override row for {key}  [{_scope_label(overlay)}]")
        raise SystemExit(1)

    @command(name="list")
    def list_rows(self) -> None:
        """List every DB config override row, naming each row's scope (read-only)."""
        rows = list(ConfigSetting.objects.all())
        if not rows:
            self.stdout.write("  (no DB config overrides)")
            return
        for row in rows:
            self.stdout.write(f"  {row.key} = {row.value!r}  [{_scope_label(row.scope)}]")

    @command()
    def flags(self) -> None:
        """The read-only dead-toggle audit report over the ``FEATURE_FLAGS`` registry.

        Lists every governed feature flag with its lifecycle stage, off-value, and
        tracking issue; a ``REMOVE``-stage flag (a toggle whose gated code is now
        permanent) is surfaced LOUD so a dead toggle cannot rot unnoticed. Reads the
        code-level registry only — it writes nothing to the ``ConfigSetting`` store.
        """
        self.stdout.write(render_flags_audit(FEATURE_FLAGS))

    @command()
    def get(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name to read (must be overridable).")],
        overlay: _OverlayOption = "",
    ) -> None:
        """Print the resolved value for *key* and name its source (DB vs file/env).

        The read side of the dual-read store: when a ``ConfigSetting`` row exists
        in the requested scope it is reported as the ``db`` source; otherwise the
        value falls through to the file/env layer and is reported as the
        ``file/env`` source. ``--overlay <name>`` reads that overlay's scope.
        Refuses a key in neither ``OVERLAY_OVERRIDABLE_SETTINGS`` nor
        ``REGISTRY_SETTINGS`` so a typo is loud, not a silent ``file/env`` answer
        for a non-setting.
        """
        if key not in OVERLAY_OVERRIDABLE_SETTINGS and key not in REGISTRY_SETTINGS:
            self.stderr.write(f"  refusing: {key!r} is not an overridable setting (#1775 pilot scope)")
            raise SystemExit(2)
        stored = ConfigSetting.objects.get_effective(key, scope=overlay)
        if stored is not None:
            self.stdout.write(f"  {key} = {stored!r}  [source: db, {_scope_label(overlay)}]{_flag_suffix(key)}")
            return
        fallback = getattr(get_effective_settings(overlay or None), key, None)
        self.stdout.write(f"  {key} = {fallback!r}  [source: file/env]{_flag_suffix(key)}")

    @command(name="import")
    def import_toml(
        self,
        *,
        no_clobber: Annotated[
            bool,
            typer.Option(
                "--no-clobber",
                help="Seed only keys absent from the store; never overwrite an existing DB row.",
            ),
        ] = False,
    ) -> None:
        """Seed the DB store from the operational toml keys (one-time migration).

        The dual-read migration step (#938): every ``[teatree]`` key that is a
        registered ``OVERLAY_OVERRIDABLE_SETTINGS`` field is coerced through that
        registry's parser and upserted into the GLOBAL store, and every operational
        key under an ``[overlays.<name>]`` table is upserted into THAT overlay's
        scope — the DB twin of the per-overlay TOML override (#1775). So an install
        with both a global ``mode`` and a per-overlay ``mode = "auto"`` migrates both
        tiers in one pass. Bootstrap-file-only keys (``private_repos`` /
        ``DATABASE_URL`` / …), the overlay's own ``path`` / ``url`` discovery keys,
        and unknown keys are skipped — only operational settings move. The upsert
        makes a re-run idempotent.

        ``--no-clobber`` seeds only keys ABSENT from the store and leaves an
        existing row untouched — the mode ``t3 setup`` runs on every update so a
        value the user changed via ``config_setting set`` survives. Without it
        (the default), a re-import refreshes every operational key from the file.
        """
        result = import_toml_into_db(load_config().raw, clobber=not no_clobber)
        for reason in result.skipped_reasons:
            self.stderr.write(f"  {reason}")
        for scope, key in result.rows:
            stored = ConfigSetting.objects.get_effective(key, scope=scope)
            self.stdout.write(f"  imported {key} = {stored!r}  [{_scope_label(scope)}]")
        self.stdout.write(f"  {result.summary()}")

    @command()
    def export(
        self,
        *,
        overlay: _OverlayOption = "",
        output: Annotated[
            str,
            typer.Option("--output", help="Write the TOML to this path instead of stdout."),
        ] = "",
        include_private: Annotated[
            bool,
            typer.Option(
                "--include-private",
                help="Also export private/secret rows (terms/brands, token refs) — PERSONAL backup only, never share.",
            ),
        ] = False,
    ) -> None:
        """Dump the ``ConfigSetting`` store to TOML — the inverse of ``import``.

        Global rows render under ``[teatree]`` and each overlay scope under
        ``[overlays.<name>]``, each value as its native TOML scalar — so a dump fed
        back through ``import`` rebuilds the same store (``export -> import ->
        export`` is a fixed point). ``--overlay <name>`` scopes the dump to that one
        overlay; omitted, every scope is dumped. ``--output <path>`` writes a file;
        omitted, the TOML goes to stdout.

        The secret guard withholds private rows by DEFAULT — a known-private key
        (``SECRET_SETTINGS``) or any value carrying a customer/brand term — so a
        SHARED export (auto-configuring a fresh teatree) cannot leak customer data
        even though the private DB store keeps it. Each withheld row is named on
        stderr; ``--include-private`` exports everything for a PERSONAL, never-shared
        backup.
        """
        result = export_db_to_toml(overlay or None, include_private=include_private)
        for row in result.redacted:
            self.stderr.write(f"  withheld {row.key}  [{_scope_label(row.scope)}]  ({row.reason})")
        if result.redacted:
            self.stderr.write(
                f"  {len(result.redacted)} private/tainted row(s) withheld; pass --include-private to include them."
            )
        if output:
            Path(output).expanduser().write_text(result.toml, encoding="utf-8")
            self.stdout.write(f"  exported config store to {output}")
            return
        self.stdout.write(result.toml, ending="")
