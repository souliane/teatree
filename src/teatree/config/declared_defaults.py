"""Every shipped default read from the DECLARATION that states it, never from the file.

``defaults.toml`` is an EXPORT of this module: :func:`render_defaults_file` re-renders
its ``[teatree]`` table from the declarations and
``scripts/hooks/generate_defaults_toml.py`` writes the result, so the file cannot ship a
value no declaration states. Exactly three declarations own the ``Category.DEFAULT``
keys — the ``UserSettings`` dataclass, :data:`~teatree.config.registries.COLD_SETTINGS`
and :data:`~teatree.config.registries.COLD_HOOK_SETTINGS` — and :func:`declared_defaults`
is TOTAL over them: a Default-category key none of the three declares raises rather than
being dropped from the file it is meant to render.

Sourcing the values from :func:`~teatree.config.schema.shipped_defaults` instead would
make the generator circular — that singleton is CONSTRUCTED from ``defaults.toml``, so a
generator built on it re-renders whatever the file already says and its byte-for-byte
pass proves nothing at all. Measured with ``artifact_idle_days = 99.0`` planted in the file:
a ``shipped_defaults``-sourced render came back byte-identical, while this one restored
the declared ``2.0``.

The base TEXT is still the current file, because the ``[loops]`` / ``[modes]`` /
``[schedules]`` seed tables and the header are hand-maintained content no declaration
derives; only the ``[teatree]`` table is replaced.
"""

import dataclasses
from collections.abc import Mapping

from teatree.config.cold_defaults import DEFAULTS_TOML
from teatree.config.defaults_snapshot import SettingValue, default_category_keys, render_toml
from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS
from teatree.config.setting_layers import stored_form
from teatree.config.settings import UserSettings


def _renderable(value: object) -> SettingValue:
    """*value* in the form the file stores it, refusing a shape the file has never carried.

    The conversion is :func:`~teatree.config.setting_layers.stored_form`, shared with the
    resolver's default authority so the rendered file and the resolved value cannot
    disagree. What this adds is the REFUSAL: a field typed as something new (a ``Path``, a
    new dataclass) fails loudly rather than rendering as its ``repr``.
    """
    stored = stored_form(value)
    if isinstance(stored, bool | int | float | str | list | Mapping):
        return stored
    msg = f"no stored form for a {type(value).__name__} default — teach stored_form how the file carries it"
    raise TypeError(msg)


def declared_defaults() -> dict[str, SettingValue]:
    """Each ``Category.DEFAULT`` key's shipped default, from the declaration that owns it."""
    code = UserSettings()
    fields = {f.name for f in dataclasses.fields(UserSettings)}
    cold = {**COLD_SETTINGS, **COLD_HOOK_SETTINGS}
    defaults: dict[str, SettingValue] = {}
    for key in sorted(default_category_keys()):
        if key in fields:
            defaults[key] = _renderable(getattr(code, key))
        elif key in cold:
            defaults[key] = cold[key].default
        else:
            msg = f"{key!r} is Default-category but no declaration states its value"
            raise KeyError(msg)
    return defaults


def render_defaults_file(base_text: str | None = None) -> str:
    """The whole shipped file with its ``[teatree]`` table rendered from the declarations.

    *base_text* defaults to the committed file, whose hand-maintained seed tables and
    header survive the re-render byte for byte.
    """
    text = DEFAULTS_TOML.read_text(encoding="utf-8") if base_text is None else base_text
    return render_toml(declared_defaults(), base_text=text)


__all__ = ["declared_defaults", "render_defaults_file"]
