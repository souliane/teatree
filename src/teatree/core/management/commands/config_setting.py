"""``t3 <overlay> config_setting`` — set/clear/list the DB config override tier.

The ORM-touching admin path for the ``ConfigSetting`` store (#1775, the first
slice of "move config to the database"). Mirrors the per-worktree env command
shape: a django_typer ``TyperCommand`` whose subcommands write to the
authoritative source (the DB), never a file.

The pilot is scoped to keys registered in ``OVERLAY_OVERRIDABLE_SETTINGS`` (the
``UserSettings`` partition the resolver's DB tier consults) plus the
``REGISTRY_SETTINGS`` keys (``overlays`` / ``e2e_repos`` / ``peer_instances`` — the
non-``UserSettings`` registries ``loader._inject_db_registries`` injects into
``config.raw``), so an admin
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
import sys
import tomllib
from pathlib import Path
from typing import Annotated, NoReturn

import typer
from django.core.exceptions import ValidationError
from django_typer.management import TyperCommand, command

from teatree.config import (
    ALL_KNOWN_CONFIG_SETTINGS,
    COLD_HOOK_SETTINGS,
    FEATURE_FLAGS,
    effective_default,
    get_effective_settings,
)
from teatree.config.feature_flags import flag_trailer, render_flags_audit
from teatree.config.retired_settings import retirement_notice
from teatree.config.setting_groups import group_outline
from teatree.config.stored_row_health import stored_row_note
from teatree.config.write_validation import ConfigWriteError, validate_config_write
from teatree.core.config_interchange.migration import export_db_to_toml, import_toml_to_db
from teatree.core.factory.feature_inertness import feature_inertness, render_inertness_report
from teatree.core.models import ConfigSetting
from teatree.core.models.config_setting import ENTRYPOINT_SEEDER, scope_label

# Every key ``config_setting`` knows — the SINGLE known-key set shared by
# get/list/set/clear AND the MCP ``config_setting_get`` read tool
# (``teatree.config.known_settings``): every key ``list`` can display is one
# ``get`` resolves and ``set``/``clear`` accept, and an admin still cannot stash
# a row no reader would consult.
_ALLOWED_SETTINGS = ALL_KNOWN_CONFIG_SETTINGS

_OverlayOption = Annotated[
    str,
    typer.Option("--overlay", help="Overlay name to scope the row to; omit for the global scope (every overlay)."),
]


def _flag_suffix(key: str) -> str:
    """A leading-space ``[feature flag, …]`` governance trailer for *key*, or ``""``.

    So an operator flipping a governed, lifecycle-staged toggle sees it is a flag —
    not a durable setting — and where its removal is tracked, without a second lookup.
    """
    trailer = flag_trailer(key)
    return f"  {trailer}" if trailer else ""


def _stored_row_suffix(key: str) -> str:
    """A leading-space ``[retired …]`` / ``[internal state …]`` trailer, or ``""``.

    So a stored row no live setting declaration owns can never be read as a live
    control — the harm in souliane/teatree#3862.
    """
    note = stored_row_note(key)
    return f"  {note}" if note else ""


class Command(TyperCommand):
    def _refuse_unknown_key(self, key: str) -> NoReturn:
        """Refuse *key*, rendering its retirement record when one is on file (#4094).

        The registry is the single source, so this surface, the resolver's loud
        warning and the ``list`` marker cannot come to describe a retirement
        differently. A retired key stays unwritable — the record is the answer, not
        an admission that would resolve nowhere.
        """
        self.stderr.write(f"  refusing: {retirement_notice(key) or f'{key!r} is not a known config setting'}")
        raise SystemExit(2)

    @command()
    def set(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name (must be overridable).")],
        value: Annotated[str, typer.Argument(help="JSON value, e.g. true / false / '\"x\"' / 3.")],
        overlay: _OverlayOption = "",
    ) -> None:
        """Upsert the DB override row for *key* (in *overlay*'s scope or global) to *value*.

        Refuses a key outside the unified known-key set
        (``OVERLAY_OVERRIDABLE_SETTINGS`` / ``REGISTRY_SETTINGS`` / ``COLD_SETTINGS``
        / ``COLD_HOOK_SETTINGS``), a *value* that is not valid JSON, and a *value*
        that JSON-parses but is invalid for the setting's type, leaving the store
        untouched on any error.

        ``--overlay <name>`` scopes the row to one overlay (the per-overlay
        override); omitted, it writes the global scope.

        The type check runs the **same** registry parser the resolver applies on
        read (#258): an out-of-enum ``mode`` or a quoted ``"false"`` for a
        bool-typed setting is rejected here, at WRITE time, so a value that would
        raise on every later config resolution can never be stored. Validating
        on write is what keeps a bad row from bricking all reads.
        """
        if key not in _ALLOWED_SETTINGS:
            self._refuse_unknown_key(key)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            self.stderr.write(f"  invalid JSON value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        try:
            canonical = validate_config_write(key, parsed)
        except ConfigWriteError as exc:
            self.stderr.write(f"  invalid value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        # Persist the CANONICAL parsed value, not the raw user value, so the DB
        # row and the read-time coercion agree (#258): a numeric string ``"5"``
        # is stored as the int ``5`` and an upper-case enum ``"AUTO"`` as the
        # normalised ``"auto"``. Every registry parser returns a JSON-storable
        # type — scalar, list, or a ``StrEnum`` (which a ``JSONField`` persists as
        # its string value) — so the parsed value round-trips through the store
        # and the read tier re-coerces it to the same value.
        try:
            ConfigSetting.objects.set_value(key, canonical, scope=overlay)
        except ValidationError as exc:
            # A coupled-key inconsistency (#3688) — e.g. an agent_harness_provider
            # no resulting agent_harness would accept at dispatch. Refuse loudly at
            # write time, leaving the store untouched, instead of a fleet-wide
            # repair-halt flood on every later dispatch.
            self.stderr.write(f"  refusing inconsistent config for {key!r}: {exc.messages[0]}")
            raise SystemExit(2) from exc
        # Verify-by-re-read: report the stored value the resolver will now see.
        stored = ConfigSetting.objects.get_effective(key, scope=overlay)
        self.stdout.write(f"  set {key} = {stored!r}  [{scope_label(overlay)}]{_flag_suffix(key)}")

    @command()
    def seed(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name (must be overridable).")],
        value: Annotated[str, typer.Argument(help="JSON value, e.g. true / false / '\"x\"' / 3.")],
        overlay: _OverlayOption = "",
        seeded_by: Annotated[
            str,
            typer.Option("--seeded-by", help="Provenance marker recorded on the row (default: entrypoint)."),
        ] = ENTRYPOINT_SEEDER,
    ) -> None:
        """Provenance-aware DEPLOY seed of *key* → *value* (#3435).

        Unlike ``set`` (an operator write that always upserts), ``seed`` is the
        idempotent redeploy path: it NEVER writes a value equal to the code
        default (which would only freeze a future default change), PRESERVES any
        operator override, and re-seeds a row it still owns when the shipped
        default changed. It records provenance (``seeded_by`` + the seeded value)
        so a later ``t3 doctor --repair`` autofix can tell a deploy-seeded row
        from an operator's deliberate pin. Same key/JSON validation as ``set``.
        """
        if key not in _ALLOWED_SETTINGS:
            self._refuse_unknown_key(key)
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            self.stderr.write(f"  invalid JSON value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        try:
            canonical = validate_config_write(key, parsed)
        except ConfigWriteError as exc:
            self.stderr.write(f"  invalid value for {key!r}: {exc}")
            raise SystemExit(2) from exc
        outcome = ConfigSetting.objects.seed(
            key,
            canonical,
            code_default=effective_default(key),
            seeded_by=seeded_by,
            scope=overlay,
        )
        stored = ConfigSetting.objects.get_effective(key, scope=overlay)
        self.stdout.write(
            f"  seed {key}: {outcome.value}  (effective={stored!r})  [{scope_label(overlay)}]{_flag_suffix(key)}"
        )

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
            self.stdout.write(f"  cleared DB override for {key}  [{scope_label(overlay)}]")
            return
        self.stderr.write(f"  no DB override row for {key}  [{scope_label(overlay)}]")
        raise SystemExit(1)

    @command(name="list")
    def list_rows(self) -> None:
        """List every DB config override row under its group, naming each row's scope.

        Rows are grouped by the SAME nested hierarchy the dashboard and the TOML export
        render, indented one level per depth, so the three surfaces read alike. A row no
        declaration owns still prints, under the leftovers heading — carrying a trailer
        naming it retired, internal state, or unknown, so it cannot be mistaken for a
        live control.
        """
        rows = list(ConfigSetting.objects.all())
        if not rows:
            self.stdout.write("  (no DB config overrides)")
            return
        for section in group_outline(rows, key_of=lambda row: row.key):
            for heading in section.headings:
                self.stdout.write(f"{'  ' * heading.depth}{heading.label}")
            for row in section.rows:
                indent = "  " * (section.depth + 1)
                self.stdout.write(
                    f"{indent}{row.key} = {row.value!r}  [{scope_label(row.scope)}]{_stored_row_suffix(row.key)}"
                )

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
    def inert(self) -> None:
        """Which gated features shipped and then never ran (#4189).

        One line per gate that is off in every scope and whose declared observable is
        empty — the feature twin of ``t3 loops audit``'s shipped-seed report. A gate
        nobody ever decided to leave off is surfaced LOUD; one the owner deliberately
        staged is listed quietly, so the report stays worth reading.
        """
        self.stdout.write(render_inertness_report(feature_inertness()))

    @command()
    def get(
        self,
        key: Annotated[str, typer.Argument(help="UserSettings field name to read (must be overridable).")],
        overlay: _OverlayOption = "",
    ) -> None:
        """Print the resolved value for *key* and name its source (DB vs env/default).

        When a ``ConfigSetting`` row exists in the requested scope it is reported as
        the ``db`` source; otherwise the value falls through to the code layer: a
        cold-hook gate key (``COLD_HOOK_SETTINGS``) reports its in-code
        ``ColdHookSetting`` default, every other key its ``UserSettings``
        env/default value. ``--overlay <name>`` reads that overlay's scope. Refuses
        an unknown key — a typo is loud, not a silent answer for a non-setting — but
        accepts every key ``list`` can display (the unified known-key set).
        """
        if key not in _ALLOWED_SETTINGS:
            self._refuse_unknown_key(key)
        stored = ConfigSetting.objects.get_effective(key, scope=overlay)
        if stored is not None:
            self.stdout.write(f"  {key} = {stored!r}  [source: db, {scope_label(overlay)}]{_flag_suffix(key)}")
            return
        cold_hook = COLD_HOOK_SETTINGS.get(key)
        if cold_hook is not None:
            self.stdout.write(f"  {key} = {cold_hook.default!r}  [source: code default]{_flag_suffix(key)}")
            return
        fallback = getattr(get_effective_settings(overlay or None), key, None)
        self.stdout.write(f"  {key} = {fallback!r}  [source: env/default]{_flag_suffix(key)}")

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
        default_keys_only: Annotated[
            bool,
            typer.Option(
                "--default-keys-only",
                help="Restrict the dump to the Category.DEFAULT keys defaults.toml ships "
                "(drops registries, secrets, identifiers and overlay scopes).",
            ),
        ] = False,
        include_defaults: Annotated[
            bool,
            typer.Option(
                "--include-defaults",
                help="Also emit keys with no DB row, at their resolved effective value. "
                "With --default-keys-only this is the defaults.toml shape.",
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

        Each line carries a trailing comment saying what the key ACCEPTS — its stored
        type, plus the alternatives where the schema constrains them to a set — then what
        it means. Reviewing defaults away from the dashboard is exactly where "may this
        take any string?" gets asked, and the answer is the schema's own
        (:func:`~teatree.config.schema.setting_choices`, the derivation the dashboard's
        selects are built from), so the two surfaces cannot come to disagree.

        The secret guard withholds private rows by DEFAULT — a known-private key
        (``SECRET_SETTINGS``) or any value carrying a customer/brand term — so a
        SHARED export (auto-configuring a fresh teatree) cannot leak customer data
        even though the private DB store keeps it. Each withheld row is named on
        stderr; ``--include-private`` exports everything for a PERSONAL, never-shared
        backup. That file carries the rows an ordinary ``import`` refuses, so it stamps
        itself a backup and is restored with ``import --restore-private`` (#4156).

        A stored row that is not a SETTING — internal runtime state sharing the store, a
        key outliving its declaration — is named on stderr and left out whatever the flags
        say: ``import`` has no home for such a key and refuses the whole file on one.

        Two INDEPENDENT filters widen the dump, both off by default. ``--default-keys-only``
        restricts it to the ``Category.DEFAULT`` keys ``defaults.toml`` ships;
        ``--include-defaults`` also emits the eligible keys that have no DB row, at their
        resolved effective value. Passing BOTH produces the defaults shape — a complete,
        drop-in replacement for ``config/defaults.toml``, header and seed tables included.
        """
        result = export_db_to_toml(
            overlay or None,
            include_private=include_private,
            default_keys_only=default_keys_only,
            include_defaults=include_defaults,
        )
        for row in result.redacted:
            self.stderr.write(f"  withheld {row.key}  [{scope_label(row.scope)}]  ({row.reason})")
        if result.redacted:
            self.stderr.write(
                f"  {len(result.redacted)} private/tainted row(s) withheld; pass --include-private to include them."
            )
        for row in result.omitted:
            self.stderr.write(f"  omitted {row.key}  [{scope_label(row.scope)}]  ({row.reason})")
        if result.omitted:
            self.stderr.write(
                f"  {len(result.omitted)} stored row(s) omitted: not configuration, so import refuses them."
            )
        if result.private_backup:
            self.stderr.write("  this is a PERSONAL BACKUP carrying private rows an ordinary import refuses.")
            self.stderr.write("  never share it; restore it with `config_setting import --restore-private`.")
        if output:
            Path(output).expanduser().write_text(result.toml, encoding="utf-8")
            self.stdout.write(f"  exported config store to {output}")
            return
        self.stdout.write(result.toml, ending="")

    @command(name="import")
    def import_config(
        self,
        *,
        input_path: Annotated[
            str,
            typer.Option("--input", help="Read the TOML dump from this path; omit to read stdin."),
        ] = "",
        dry_run: Annotated[
            bool,
            typer.Option(
                "--dry-run", help="Classify every row (folded / written / skipped / rejected); write nothing."
            ),
        ] = False,
        restore_private: Annotated[
            bool,
            typer.Option(
                "--restore-private",
                help="Restore the private rows of a --include-private personal backup (that file only).",
            ),
        ] = False,
    ) -> None:
        """Load a ``config_setting export`` TOML dump into the store — the inverse of ``export``.

        Retired aliases fold onto their live key; unknown keys and secret/personal-identifier
        rows are REJECTED and the WHOLE import is refused (nothing written) so one bad key never
        leaves a partial store; every value is validated through the same registry parser the
        resolver applies on read. A value equal to the shipped default writes NO row (so a dump of
        ``defaults.toml`` imports to zero rows), and a value the store already holds is reported
        as unchanged rather than written. What the export could NOT carry cannot become a change
        either: a row omitted as non-configuration is simply absent, and a registry field the
        secret guard withheld is merged back from the store rather than deleted. So re-importing
        this box's own export writes nothing and deletes nothing — an import never removes a
        value; ``config_setting clear`` does. ``--dry-run`` classifies without writing.

        Safety-posture keys import here without a confirm phrase: typing this command IS the
        operator's authorization, exactly as ``config_setting set`` is. The dashboard's import
        textarea is the surface that demands one, because a paste is not a per-key intent.

        ``--restore-private`` accepts the private rows of an ``export --include-private``
        backup, the one file that carries them — so the flag whose purpose is a COMPLETE
        backup produces one that restores (#4156). It grants nothing on any other file: an
        ordinary dump's secret rows are refused under it exactly as without it.
        """
        text = Path(input_path).expanduser().read_text(encoding="utf-8") if input_path else sys.stdin.read()
        try:
            result = import_toml_to_db(
                text, dry_run=dry_run, allow_safety_posture=True, restore_private=restore_private
            )
        except tomllib.TOMLDecodeError as exc:
            self.stderr.write(f"  invalid TOML: {exc}")
            raise SystemExit(2) from exc
        if restore_private and not result.private_backup:
            self.stderr.write("  --restore-private ignored: this file is not an --include-private personal backup.")
        for old, new in result.folded:
            self.stdout.write(f"  folded retired alias {old} -> {new}")
        for row in result.rejected:
            self.stderr.write(f"  rejected {row.key}  [{scope_label(row.scope)}]  ({row.reason})")
        if result.rejected:
            if result.private_backup and not restore_private:
                self.stderr.write(
                    "  this file is an --include-private personal backup; "
                    "pass --restore-private to restore its private rows."
                )
            self.stderr.write(f"  {len(result.rejected)} row(s) rejected; nothing was imported.")
            raise SystemExit(2)
        verb = "would import" if dry_run else "imported"
        for row in result.written:
            self.stdout.write(f"  {verb} {row.key} = {row.toml_value}  [{scope_label(row.scope)}]")
        self.stdout.write(
            f"  {verb} {len(result.written)} row(s); {len(result.unchanged)} already at that value; "
            f"{len(result.skipped_default)} equal to the shipped default (no row)."
        )
