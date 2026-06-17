"""TOML -> ``ConfigSetting`` import service — the #938 dual-read migration (TODO-75).

The reusable seam that seeds the DB override store from the existing
``~/.teatree.toml`` so the cutover to the #1775 DB/TOML hard partition does not
lose a user's pre-partition config. Both callers share it:

*   ``t3 <overlay> config_setting import`` — the manual one-time migration, with
    its original CLOBBER semantics (re-import refreshes every operational key
    from the file, overwriting any stale DB row).
*   ``t3 setup`` — the AUTOMATIC migration. It runs on every update, so it calls
    with ``clobber=False``: it seeds only keys ABSENT from the store and never
    overwrites a value the user has since changed via ``config_setting set``.

Every ``[teatree]`` key that is a registered ``OVERLAY_OVERRIDABLE_SETTINGS``
(= DB-home) field is coerced through that registry's parser and upserted into the
GLOBAL scope; every operational key under an ``[overlays.<name>]`` table is
upserted into THAT overlay's scope — the DB twin of the per-overlay TOML override
(#1775). Bootstrap-file-only keys (``private_repos`` / ``DATABASE_URL`` / …), the
overlay's own ``path`` / ``url`` discovery keys, and unknown keys are skipped:
only operational settings move.

The service returns a structured :class:`ConfigImportResult` rather than writing
to a stream, so the management command renders it and ``t3 setup`` logs a one-line
summary from the same outcome.
"""

from dataclasses import dataclass, field

from teatree.config import OVERLAY_OVERRIDABLE_SETTINGS
from teatree.core.models import ConfigSetting

GLOBAL_SCOPE = ""


def _scope_label(scope: str) -> str:
    """Human label for a scope: ``global`` for the empty scope else ``overlay '<name>'``."""
    return "global" if not scope else f"overlay {scope!r}"


@dataclass
class ConfigImportResult:
    """Outcome of a TOML -> ``ConfigSetting`` import pass.

    ``imported`` counts rows newly written (a key with no prior row in its
    scope). ``overwritten`` counts existing rows replaced (clobber mode only).
    ``preserved`` counts rows left untouched because a value already existed and
    the caller asked not to clobber. ``skipped`` counts file keys that are not
    operational DB-home settings (bootstrap / discovery / unknown / invalid).
    ``rows`` records ``(scope, key)`` for every row written or overwritten so the
    summary can name what moved; ``skipped_reasons`` records the loud-skip lines
    for invalid values.
    """

    imported: int = 0
    overwritten: int = 0
    preserved: int = 0
    skipped: int = 0
    rows: list[tuple[str, str]] = field(default_factory=list)
    skipped_reasons: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        """Rows actually written this pass (new + overwritten)."""
        return self.imported + self.overwritten

    def summary(self) -> str:
        """One-line human summary naming the count and the scopes touched."""
        if not self.rows:
            return f"imported {self.changed} setting(s) into the DB store"
        scopes = sorted({_scope_label(scope) for scope, _ in self.rows})
        return f"imported {self.changed} setting(s) into the DB store [{', '.join(scopes)}]"


def import_toml_into_db(raw: dict, *, clobber: bool = True) -> ConfigImportResult:
    """Seed the ``ConfigSetting`` store from a raw config dict; return the outcome.

    Walks the global ``[teatree]`` table into the global scope and each
    ``[overlays.<name>]`` table into that overlay's scope. With *clobber* (the
    default, the manual ``config_setting import`` semantics) an existing row is
    overwritten from the file value. With ``clobber=False`` (the ``t3 setup``
    auto-migration) a key that already has a row in its scope is left untouched
    and counted as ``preserved`` — so a value the user changed via
    ``config_setting set`` survives every later ``t3 setup``.
    """
    result = ConfigImportResult()
    teatree_table = raw.get("teatree")
    if isinstance(teatree_table, dict):
        _import_table(teatree_table, GLOBAL_SCOPE, clobber=clobber, result=result)
    overlays = raw.get("overlays")
    if isinstance(overlays, dict):
        for overlay_name, overlay_cfg in overlays.items():
            if isinstance(overlay_cfg, dict):
                _import_table(overlay_cfg, overlay_name, clobber=clobber, result=result)
    return result


def _import_table(table: dict, scope: str, *, clobber: bool, result: ConfigImportResult) -> None:
    """Upsert every operational key in *table* into *scope*, mutating *result*.

    A key registered in ``OVERLAY_OVERRIDABLE_SETTINGS`` is coerced through its
    registry parser and (per *clobber*) written or preserved; every other key is
    skipped. An invalid value is recorded loud and skipped — never fatal — so one
    bad key cannot abort the migration of the rest.
    """
    for key, raw_value in table.items():
        parser = OVERLAY_OVERRIDABLE_SETTINGS.get(key)
        if parser is None:
            result.skipped += 1
            continue
        try:
            canonical = parser(raw_value)
        except (ValueError, TypeError, AttributeError) as exc:
            result.skipped += 1
            result.skipped_reasons.append(
                f"skipped {key!r} [{_scope_label(scope)}]: invalid value {raw_value!r}: {exc}"
            )
            continue
        existed = ConfigSetting.objects.get_effective(key, scope=scope) is not None
        if existed and not clobber:
            result.preserved += 1
            continue
        ConfigSetting.objects.set_value(key, canonical, scope=scope)
        result.rows.append((scope, key))
        if existed:
            result.overwritten += 1
        else:
            result.imported += 1
