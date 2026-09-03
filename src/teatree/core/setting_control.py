"""One setting's CONTROL — its kind, its admissible options, and how its values render.

The derivation behind every surface that offers a setting for reading or editing: the
dashboard's editable grid (:mod:`teatree.dash.settings_editor`) and the settings snapshot
(:mod:`teatree.core.settings_snapshot`) both compose a :class:`SettingControl` rather than
re-deriving what kind of control a key needs. Two consumers, ONE derivation — a second copy
of the kind rule is how a snapshot comes to call ``enum`` what the grid renders as free text.

It sits in ``core`` beside :mod:`teatree.core.config_display` for the same reason that module
does: the Django admin depends on this vocabulary and may not import ``dash``.

The masking policy is NOT re-decided here — :func:`~teatree.core.config_display.is_secret`
remains the single taxonomy, and every value this module renders goes through
:func:`~teatree.core.config_display.masked_display`.
"""

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import time
from types import UnionType
from typing import TYPE_CHECKING, Final, Union, get_args, get_origin

from teatree.config.schema import TeatreeSettingsSchema, setting_choices
from teatree.config.setting_help import setting_help
from teatree.config.setting_registries import SAFETY_POSTURE_KEYS
from teatree.core.config_display import MASKED, NO_SHIPPED_DEFAULT, is_secret, masked_display, render_value

if TYPE_CHECKING:
    from teatree.core.models.config_setting import ConfigValue

#: Python type -> the control vocabulary word for it. The four collection types collapse to
#: ``list`` because a control offers the same editing gesture for all of them.
TYPE_KINDS: Final[dict[object, str]] = {
    bool: "bool",
    int: "int",
    float: "float",
    str: "str",
    time: "time",
    list: "list",
    tuple: "list",
    set: "list",
    frozenset: "list",
    dict: "dict",
}

UNKNOWN_KIND: Final = "unknown"


def wire(value: object) -> str:
    """*value* as the JSON literal an edit POSTs — the one encoding both ends agree on."""
    return json.dumps(value, default=str)


def annotation_text(annotation: object) -> str:
    """*annotation* as stable display text — the form a snapshot can compare across boxes."""
    if annotation is None:
        return ""
    if isinstance(annotation, type) and not get_args(annotation):
        return annotation.__name__
    return str(annotation).replace("typing.", "")


def setting_kind(annotation: object, choices: Sequence[object]) -> str:
    """Which control *annotation* needs — ``bool`` / ``enum`` / a scalar or container word.

    ``bool`` is decided before *choices* because a boolean's two values ARE its choices, and
    calling that ``enum`` would render a toggle as a two-option select on every surface.
    """
    inner = _unwrap_optional(annotation)
    if inner is bool:
        return "bool"
    if choices:
        return "enum"
    origin = get_origin(inner) or inner
    if (kind := TYPE_KINDS.get(origin)) is not None:
        return kind
    if isinstance(origin, type) and issubclass(origin, str):
        return "str"
    return UNKNOWN_KIND


def value_kind(value: object) -> str:
    """The kind word for an OBSERVED value — for a field carrying no declared annotation."""
    return TYPE_KINDS.get(type(value), UNKNOWN_KIND)


def _unwrap_optional(annotation: object) -> object:
    if get_origin(annotation) not in {Union, UnionType}:
        return annotation
    present = [arg for arg in get_args(annotation) if arg is not type(None)]
    return present[0] if len(present) == 1 else annotation


@dataclass(frozen=True, slots=True)
class SettingChoice:
    """One option of a constrained control — the JSON an edit posts, and its screen label.

    The label runs through the SAME ``render_value`` every other value on a page does, so a
    boolean reads ``on`` / ``off`` in the select exactly as it does in the default column.
    """

    value: str
    label: str


@dataclass(frozen=True, slots=True)
class SettingControl:
    """Everything a surface needs to render one setting, derived from the schema alone.

    *shipped* is the shipped-defaults table in STORED form, so "same as default" compares
    like with like rather than a stored scalar against a coerced value.
    """

    key: str
    shipped: "Mapping[str, ConfigValue]" = field(default_factory=dict)

    @property
    def name(self) -> str:
        return self.key

    @property
    def help_text(self) -> str:
        """The same sentence ``defaults.toml`` carries as this key's comment — never re-typed."""
        return setting_help(self.key)

    @property
    def is_secret(self) -> bool:
        return is_secret(self.key)

    @property
    def is_safety_posture(self) -> bool:
        return self.key in SAFETY_POSTURE_KEYS

    @property
    def has_shipped_default(self) -> bool:
        return self.key in self.shipped

    @property
    def shipped_default(self) -> str:
        """The shipped default as display text — ``***`` for a secret, a sentence when there is none.

        A key the shipped file carries no entry for reads as :data:`NO_SHIPPED_DEFAULT` rather
        than an empty string a template then spells as the bare word ``none`` (#4078): ``none``
        reads as a VALUE, and it was the same word whether the file had no entry or the entry
        was empty — two different facts, one rendering.
        """
        if self.is_secret:
            return MASKED
        return render_value(self.shipped[self.key]) if self.has_shipped_default else NO_SHIPPED_DEFAULT

    @property
    def annotation(self) -> object:
        """The schema's declared type for this key, or ``None`` when it declares no field."""
        field = TeatreeSettingsSchema.model_fields.get(self.key)
        return None if field is None else field.annotation

    @property
    def annotation_text(self) -> str:
        return annotation_text(self.annotation)

    @property
    def raw_choices(self) -> tuple[object, ...]:
        """The schema's own admissible set, in its own vocabulary — ``()`` when open-valued."""
        return setting_choices(self.key)

    @property
    def kind(self) -> str:
        return setting_kind(self.annotation, self.raw_choices)

    @property
    def choices(self) -> tuple[SettingChoice, ...]:
        """The admissible values as select options — derived, so a select cannot offer a refusal."""
        return tuple(SettingChoice(wire(value), render_value(value)) for value in self.raw_choices)

    def display_value(self, value: object) -> str:
        """*value* as the text a surface shows — ``***`` for a secret, WITHOUT reading it."""
        return masked_display(self.key, value)

    def wire_value(self, value: object) -> str:
        """*value* as the JSON literal a control holds — masked by the SAME test as the display text.

        A control holding the real value would put a secret in the page the moment a template
        read it, so the mask covers both renderings of one stored value.
        """
        return MASKED if self.is_secret else wire(value)


__all__ = [
    "MASKED",
    "NO_SHIPPED_DEFAULT",
    "TYPE_KINDS",
    "UNKNOWN_KIND",
    "SettingChoice",
    "SettingControl",
    "annotation_text",
    "setting_kind",
    "value_kind",
    "wire",
]
