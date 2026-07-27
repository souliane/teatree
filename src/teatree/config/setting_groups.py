"""Which declaration group each config key belongs to — the settings page's grouping.

Membership is DERIVED, never re-listed: a key's group is the ``UserSettings`` declaration
base that declares it (``tests/config/test_settings_group_partition.py`` pins those bases
pairwise-disjoint and exhaustive) or, for a key no base declares, the registry that
registers it. This module carries only each group's operator-facing label and its order —
so a newly-added key is grouped by the declaration it already has, with nothing to forget.

:data:`UNGROUPED_LABEL` is the never-vanish guarantee, not a resting place: a key no
declaration owns still gets a bucket and renders under a visible banner, rather than
being dropped from the page the way the retired band classifier dropped 130 of 184.

Deliberately pydantic-free — it composes the same cold-safe registries
``teatree.config``'s package init already loads, so importing it costs no schema import.
"""

import dataclasses
from collections.abc import Mapping

from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.registries import COLD_SETTINGS, REGISTRY_SETTINGS
from teatree.config.settings import (
    _IdentityRoutingSettings,
    _LoopFlagAndCredentialSettings,
    _LoopSettings,
    _ModeHarnessSettings,
    _OnBehalfSettings,
    _PrePublishGateSettings,
    _ProvisioningSettings,
    _QualityGateSettings,
    _ResourcePressureSettings,
    _ScannerSettings,
    _WorkspaceCoreSettings,
)

#: The bucket a key no declaration owns lands in — rendered last, under a visible banner.
UNGROUPED_LABEL = "Ungrouped"

#: A group's membership source: a declaration base (its dataclass fields) or a registry
#: (its keys). Either way the source owns the key list — this module never re-lists one.
type _GroupSource = type | Mapping[str, object]

#: Label + membership source, in render order. The source supplies the keys; nothing here
#: names one. A declaration base wins over a registry when both would claim a key.
_GROUP_SOURCES: tuple[tuple[str, _GroupSource], ...] = (
    ("Workspace & engagement", _WorkspaceCoreSettings),
    ("Mode, harness & agent runtime", _ModeHarnessSettings),
    ("Loop cadence & throughput", _LoopSettings),
    ("Posting on your behalf", _OnBehalfSettings),
    ("Identity & routing", _IdentityRoutingSettings),
    ("Quality gates", _QualityGateSettings),
    ("Scanners", _ScannerSettings),
    ("Resource pressure", _ResourcePressureSettings),
    ("Provisioning", _ProvisioningSettings),
    ("Pre-publish gates", _PrePublishGateSettings),
    ("Loop kill switches & credentials", _LoopFlagAndCredentialSettings),
    ("Term scanning, agent tables & cold reads", COLD_SETTINGS),
    ("Pre-Django hook gates", COLD_HOOK_SETTINGS),
    ("Definition registries", REGISTRY_SETTINGS),
)


def _keys_of(source: _GroupSource) -> frozenset[str]:
    if isinstance(source, Mapping):
        return frozenset(source)
    return frozenset(field.name for field in dataclasses.fields(source))


_LABEL_BY_KEY: dict[str, str] = {key: label for label, source in reversed(_GROUP_SOURCES) for key in _keys_of(source)}


def setting_group(key: str) -> str:
    """The group label *key* is rendered under — :data:`UNGROUPED_LABEL` when none owns it."""
    return _LABEL_BY_KEY.get(key, UNGROUPED_LABEL)


def group_labels() -> tuple[str, ...]:
    """Every group label in render order, with the leftovers bucket last."""
    return (*(label for label, _ in _GROUP_SOURCES), UNGROUPED_LABEL)


__all__ = ["UNGROUPED_LABEL", "group_labels", "setting_group"]
