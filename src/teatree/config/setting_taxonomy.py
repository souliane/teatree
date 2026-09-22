"""The two axes every config key is classified on, and the marker that carries them.

Split out of :mod:`teatree.config.schema` (module-health LOC cap): the taxonomy is a
closed vocabulary the schema ANNOTATES with, not part of the settings model itself, and
:func:`~teatree.config.schema.setting_meta` is the one reader that pulls a marker back
off a field. Keeping it here means the model can grow a key without the vocabulary
growing with it.
"""

from dataclasses import dataclass
from enum import StrEnum, auto


class Category(StrEnum):
    """A key's shareability class — the axis ``defaults.toml`` and the export gate key on.

    ``DEFAULT`` keys carry a shareable shipped default and ARE present in
    ``defaults.toml`` (required in the model — a DEFAULT key missing from the file
    fails construction loudly). ``PERSONAL`` (operator identifiers / machine paths /
    model-routing tables) and ``SECRET`` (customer/brand terms, credential
    coordinates) keys hold no shareable default: they carry the empty code default
    and are NEVER written to ``defaults.toml``.
    """

    DEFAULT = auto()
    PERSONAL = auto()
    SECRET = auto()


class Registry(StrEnum):
    """Which of the four existing config-key registries a key belongs to."""

    OVERLAY = auto()
    COLD = auto()
    COLD_HOOK = auto()
    REGISTRY = auto()


@dataclass(frozen=True)
class SettingMeta:
    """The ``Annotated`` taxonomy marker carried on every field.

    Instances are shared per ``(category, registry)`` combo (the ``_<CAT>_<REG>``
    constants in :mod:`teatree.config.schema`) so each field declaration stays one line
    under the 120-col cap. Per-field prose would have to un-share them, so it belongs on
    a surface keyed by field name rather than on this marker.
    """

    category: Category
    registry: Registry


__all__ = ["Category", "Registry", "SettingMeta"]
