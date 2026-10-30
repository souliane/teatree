"""Shared config-display helpers for every surface that renders a setting (#3664, D7).

The settings editor (:mod:`teatree.dash.settings_editor`), the live readouts beside it
(:mod:`teatree.dash.settings_readouts`) and the Django admin's ``ConfigSettingAdmin`` all
turn a setting's value into display text and decide which values must never reach the
response. This is the ONE source of truth for all three, so no surface can drift into
divergent render or masking. It sits in ``core`` rather than ``dash`` because the admin
is a layer BELOW the dashboard and may not import it:

- :func:`render_value` — one value-to-display rule (booleans as on/off, and each empty
    named as its own kind, so an empty list never reads as an unset value).
- :func:`is_secret` — the value-masking taxonomy: the full four-class union a secret
    VALUE is withheld by (``Category.SECRET`` field / ``SECRET_SETTINGS`` denylist /
    credential coordinate / personal identifier). A key that is a personal identifier or
    ``Category.SECRET`` but not on the denylist is masked here too, so no surface can
    regress toward exposure.
- :func:`withholds_value` — :func:`is_secret`, plus the value test a stored extra-headers map needs.
- :func:`masked_display` — the two composed: the mask for a withheld value, else the text.

Masking a credential ENTRY NAME (the ``pass`` coordinate the credentials readout shows)
is a DIFFERENT question — "does this coordinate NAME carry an internal
namespace" — answered by ``SECRET_SETTINGS`` membership alone, not by this taxonomy:
broadening it to :func:`is_secret` would hide every credential name (they are all
credential coordinates) and defeat the band. See ``CredentialEntry.mask_if_private``.
"""

from teatree.config.value_visibility import MASKED, is_secret, withholds_value

#: The shipped-defaults column for a key ``defaults.toml`` carries NO entry for. A distinct
#: sentence rather than a bare ``none``, which reads as a value and is what let "the file has
#: no entry for this key" and "the entry is empty" look identical on the page (#4078). The key
#: still has a code default; what is absent is a shipped one.
NO_SHIPPED_DEFAULT = "(no shipped default)"

#: What an UNSET value reads as. Kept as the em-dash it has always been — this is the one
#: empty that genuinely means "no value here".
UNSET = "—"


def render_value(value: object) -> str:
    """A value as display text — booleans as on/off, and each EMPTY as its own word.

    The four empties are told apart (#4078). One em-dash for ``None`` / ``""`` / ``[]`` /
    ``{}`` alike made an empty list read exactly like an unset value, which is the
    distinction an operator choosing a default actually needs: "nobody set this" and "this is
    set, to nothing" are different facts about a setting, and only the first is an absence.

    ``None`` keeps the em-dash because it IS the absence; every other empty names its own
    shape, in the same vocabulary the TOML surfaces use (a mapping is a ``table``).
    """
    if isinstance(value, bool):
        return "on" if value else "off"
    if value is None:
        return UNSET
    if isinstance(value, str) and not value:
        return "(empty text)"
    if isinstance(value, list) and not value:
        return "(empty list)"
    if isinstance(value, dict) and not value:
        return "(empty table)"
    return str(value)


def masked_display(setting: str, value: object) -> str:
    """*value* as display text, replaced by :data:`MASKED` when it is withheld."""
    return MASKED if withholds_value(setting, value) else render_value(value)


__all__ = ["MASKED", "NO_SHIPPED_DEFAULT", "UNSET", "is_secret", "masked_display", "render_value", "withholds_value"]
