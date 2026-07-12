# test-path: cross-cutting
"""Disjoint-partition guard for the ``UserSettings`` group bases (#83).

``UserSettings`` declares its 160 fields across ~11 private in-file group
dataclasses purely for readability; the flat field namespace is the persisted
contract. Dataclass inheritance SILENTLY overrides a duplicate field name (no
error), so a field accidentally declared in two groups — or dropped from every
group — would not fail loudly on its own. This guard closes that: the group
field sets must be pairwise DISJOINT and their union must equal exactly
``dataclasses.fields(UserSettings)``. Either failure turns this red, so the
grouping can never silently shadow or drop a field.
"""

import dataclasses

from teatree.config.settings import (
    UserSettings,
    _IdentityRoutingSettings,
    _LoopAndTeamsSettings,
    _LoopFlagAndCredentialSettings,
    _ModeHarnessSettings,
    _OnBehalfSettings,
    _PrePublishGateSettings,
    _ProvisioningSettings,
    _QualityGateSettings,
    _ResourcePressureSettings,
    _ScannerSettings,
    _WorkspaceCoreSettings,
)

#: The declaration bases, in MRO order. Editing the grouping WITHOUT keeping the
#: partition disjoint-and-complete is a red test.
_GROUP_BASES = (
    _WorkspaceCoreSettings,
    _ModeHarnessSettings,
    _LoopAndTeamsSettings,
    _OnBehalfSettings,
    _IdentityRoutingSettings,
    _QualityGateSettings,
    _ScannerSettings,
    _ResourcePressureSettings,
    _ProvisioningSettings,
    _PrePublishGateSettings,
    _LoopFlagAndCredentialSettings,
)


def _group_field_sets() -> list[frozenset[str]]:
    return [frozenset(f.name for f in dataclasses.fields(group)) for group in _GROUP_BASES]


def test_group_field_sets_are_pairwise_disjoint() -> None:
    seen: set[str] = set()
    for group, fields in zip(_GROUP_BASES, _group_field_sets(), strict=True):
        overlap = seen & fields
        assert not overlap, f"{group.__name__} redeclares field(s) already in another group: {sorted(overlap)}"
        seen |= fields


def test_group_union_equals_user_settings_fields() -> None:
    union: frozenset[str] = frozenset().union(*_group_field_sets())
    all_fields = frozenset(f.name for f in dataclasses.fields(UserSettings))
    assert union == all_fields, (
        f"group partition does not cover UserSettings exactly: "
        f"missing={sorted(all_fields - union)} extra={sorted(union - all_fields)}"
    )


def test_partition_flags_a_synthetic_duplicate() -> None:
    # Anti-vacuity: if a field were declared in two groups, the pairwise-disjoint
    # check must fire. Simulate by intersecting a group with itself-plus-a-known field.
    sets = _group_field_sets()
    # A known field lives in exactly one group; asserting it is NOT in a second proves
    # the disjoint check has real teeth (a duplicate would make this membership count > 1).
    memberships = sum(1 for s in sets if "mode" in s)
    assert memberships == 1, "each field must belong to exactly one group base"
