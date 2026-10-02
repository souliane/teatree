# test-path: cross-cutting
"""The DECLARED default is the base of the effective-settings chain — the file is not a tier.

Per key the chain is

    env -> DB(overlay) -> DB(global) -> overlay code default -> declared default.

The shipped ``config/defaults.toml`` sits nowhere in it: its ``[teatree]`` table is
RENDERED from those same declarations (``config/declared_defaults.py``), so reading it
back would be a second authority that can only ever agree or be a bug. These tests point
the file's path at a fixture whose values DIVERGE from the declarations and pin that
nothing the resolver serves moves — the observation that would have gone red while the
file was still read.

The file itself is still pinned: its key set must partition across the registries that
declare it, and its bytes are the render (``tests/teatree_config/test_declared_defaults.py``).
"""

import dataclasses
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase

from teatree.config import (
    ENV_SETTING_OVERRIDES,
    Autonomy,
    Mode,
    TeaTreeConfig,
    cold_defaults,
    effective_default,
    get_effective_settings,
)
from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.overlay_code_defaults import PROMOTED_OVERLAY_CODE_DEFAULT_KEYS
from teatree.config.registries import COLD_HOOK_SETTINGS, COLD_SETTINGS
from teatree.config.resolution import _BESPOKE_STRUCTURED_FIELDS as _STRUCTURED_KEYS
from teatree.config.resolution import AUTONOMY_COLLAPSED_FIELDS, _coerce_setting_rows
from teatree.config.settings import UserSettings
from teatree.core.models import ConfigSetting

# Two scalars whose declared defaults (75 / 1) the fixture diverges from, plus a
# structured sub-table — the shapes the resolver treats differently.
_DIVERGING_FIXTURE = """\
[teatree]
provision_ram_ceiling_percent = 42
merge_wip = 7

[teatree.speak]
local = "all"
"""


class TestTheShippedFileIsNotATier(TestCase):
    """A ``defaults.toml`` diverging from the declarations changes nothing the resolver serves."""

    @pytest.fixture(autouse=True)
    def _diverging_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        monkeypatch.delenv("T3_MERGE_WIP", raising=False)
        toml = tmp_path / "defaults.toml"
        toml.write_text(_DIVERGING_FIXTURE, encoding="utf-8")
        monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", toml)

    def test_a_diverging_scalar_leaves_the_declared_default_in_force(self) -> None:
        assert ConfigSetting.objects.count() == 0
        assert shipped_defaults_table()["provision_ram_ceiling_percent"] == 42
        assert get_effective_settings().provision_ram_ceiling_percent == UserSettings().provision_ram_ceiling_percent

    def test_a_diverging_sub_table_leaves_the_declared_default_in_force(self) -> None:
        assert shipped_defaults_table()["speak"]["local"] == "all"
        assert get_effective_settings().speak.local == UserSettings().speak.local

    def test_a_db_row_still_wins_over_the_declared_default(self) -> None:
        # The control: the resolver is not inert — a real override still moves the value,
        # so the no-move assertions above measure the file and not a dead resolver.
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 60)
        assert get_effective_settings().provision_ram_ceiling_percent == 60

    def test_effective_default_reads_the_declaration_not_the_file(self) -> None:
        # The seed-skip / import-skip authority agrees with the resolver, so a row equal
        # to the shipped default stays provably redundant.
        assert shipped_defaults_table()["merge_wip"] == 7
        assert effective_default("merge_wip") == UserSettings().merge_wip
        assert effective_default("session_stale_after_hours") == UserSettings().session_stale_after_hours


class TestTheBaseTheResolverIsHandedSurvives(TestCase):
    """A caller that STAGES a base carries explicit opinions no default tier may overwrite.

    ``get_effective_settings``'s base is ``load_config().user``. Production always hands it
    a bare ``UserSettings()``, but the ``load_config`` patch seam ~29 test modules use — and
    any future loader that resolves values itself — stages one. Overwriting it is the CI red
    this class caught: spreading a shipped table over EVERY field made the base inert, so a
    staged opinion silently resolved back to the shipped default and the feature it armed
    short-circuited off.
    """

    @pytest.fixture(autouse=True)
    def _no_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_a_staged_base_value_survives(self) -> None:
        staged = TeaTreeConfig(user=UserSettings(self_update_disabled=True))
        assert shipped_defaults_table()["self_update_disabled"] is False
        with mock.patch("teatree.config.load_config", return_value=staged):
            assert get_effective_settings().self_update_disabled is True

    def test_a_staged_enum_base_value_survives(self) -> None:
        # ``autonomy``, not ``mode``: ``mode`` is autonomy-collapsed, so ``_apply_autonomy``
        # pins it and the staged base could never be what survived.
        staged = TeaTreeConfig(user=UserSettings(autonomy=Autonomy.BABYSIT))
        assert shipped_defaults_table()["autonomy"] == "full"
        with mock.patch("teatree.config.load_config", return_value=staged):
            assert get_effective_settings().autonomy is Autonomy.BABYSIT

    def test_a_db_row_still_beats_a_staged_base(self) -> None:
        # The base is a DEFAULTS-tier opinion, not an override: a real row still wins.
        ConfigSetting.objects.set_value("self_update_disabled", value=False)
        staged = TeaTreeConfig(user=UserSettings(self_update_disabled=True))
        with mock.patch("teatree.config.load_config", return_value=staged):
            assert get_effective_settings().self_update_disabled is False


class TestEveryShippedKeyIsPinned:
    """The whole shipped key set is covered — no key sits in an unguarded gap.

    The resolved-field guard below only reaches keys that are ``UserSettings`` fields, and
    the cold / cold-hook keys have no field at all — they declare their default on a registry
    entry — so a wrong value there is invisible to that guard. These tests make the partition
    TOTAL: every shipped key must land in exactly one pinned bucket, so a key added to
    the file by a hand edit or a snapshot run cannot land in a gap.
    """

    def test_every_shipped_key_lands_in_exactly_one_pinned_bucket(self) -> None:
        shipped = set(shipped_defaults_table())
        resolver_reached = set(_coerce_setting_rows(shipped_defaults_table())) | _STRUCTURED_KEYS
        buckets = (resolver_reached, set(COLD_HOOK_SETTINGS), set(COLD_SETTINGS))
        unpinned = shipped - set().union(*buckets)
        assert not unpinned, f"shipped keys pinned by nothing: {sorted(unpinned)}"
        overlapping = {key for key in shipped if sum(key in bucket for bucket in buckets) > 1}
        assert not overlapping, f"shipped keys claimed by two tiers: {sorted(overlapping)}"

    def test_cold_hook_keys_ship_their_registered_default(self) -> None:
        # Driven from the REGISTRY, not the table, so an absent key is drift too: a cold-hook
        # gate the file stops shipping would otherwise leave this guard silently vacuous.
        table = shipped_defaults_table()
        missing = object()
        drift = {
            key: (table.get(key, missing), setting.default)
            for key, setting in COLD_HOOK_SETTINGS.items()
            if table.get(key, missing) != setting.default
        }
        assert not drift, f"a cold-hook key is absent from the shipped file or ships a foreign value: {drift}"

    def test_cold_keys_ship_their_unset_sentinel(self) -> None:
        # Each cold key's reader treats an absent row / empty string / non-explicit-true as
        # UNSET and applies its own fallback (e.g. `loop_preset.token_outage_preset_name`
        # returns DEFAULT_TOKEN_OUTAGE_PRESET for a blank value). So the shipped value is the
        # UNSET SENTINEL, never the fallback the reader materialises from it — writing the
        # fallback in would make the key read as explicitly set.
        table = shipped_defaults_table()
        materialised = {key: table[key] for key in table if key in COLD_SETTINGS and table[key]}
        assert not materialised, (
            f"a cold key ships a materialised fallback instead of its unset sentinel: {materialised}"
        )


class TestResolvedFieldsStayOnTheirDataclassDefault(TestCase):
    """The FULLY-RESOLVED settings object may not move a VALUE off its dataclass default.

    A shipped value moves an effective default only by an edit to the declaration that
    states it, and the shipped file is rendered from those declarations — so the resolver
    reading the file back can only ever return what the dataclass already says. This guard
    is the resolved-object half: it walks EVERY field of the object the real resolver
    returns and allows no value difference at all.

    The TYPE half admits no exception. An assembled value can compare equal while its type
    differs (``{}`` vs ``()``, ``[]`` vs ``()``, ``str`` vs ``StrEnum``), and downstream
    code that iterates, branches on emptiness, or does an identity/type-sensitive
    comparison then behaves differently on an ``==`` that passed. An approved divergence is
    coerced through the key's own registry parser, so it never changes a field's type.

    The promoted overlay-code-default keys are excluded: a DIFFERENT, pre-existing tier
    (#36) legitimately moves those, and this PR does not touch it. So are the
    ``AUTONOMY_COLLAPSED_FIELDS``, which the resolver DERIVES from the ``autonomy`` tier
    rather than reads — their reviewed decision is the tier, and it is pinned separately by
    ``test_autonomy.py``. That exemption is not a hole:
    :meth:`test_every_collapsed_field_moves_only_to_what_the_tier_prescribes` pins each of
    them to the exact value the collapse writes.
    """

    @pytest.fixture(autouse=True)
    def _no_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env_var in ENV_SETTING_OVERRIDES:
            monkeypatch.delenv(env_var, raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def _guarded_fields(self) -> list[str]:
        exempt = PROMOTED_OVERLAY_CODE_DEFAULT_KEYS | AUTONOMY_COLLAPSED_FIELDS
        return [f.name for f in dataclasses.fields(UserSettings) if f.name not in exempt]

    def test_every_resolved_field_keeps_its_dataclass_type(self) -> None:
        assert ConfigSetting.objects.count() == 0
        code, resolved = UserSettings(), get_effective_settings()
        drift = {
            name: (type(getattr(resolved, name)), type(getattr(code, name)))
            for name in self._guarded_fields()
            if type(getattr(resolved, name)) is not type(getattr(code, name))
        }
        assert not drift, f"the resolver moves a field's TYPE off its dataclass default: {drift}"

    def test_no_resolved_field_moves_off_its_dataclass_value(self) -> None:
        # Strictly stronger than the approval-ledger form this replaces: the shipped file is
        # RENDERED from these dataclass defaults, so a divergence is now unreachable rather
        # than merely unrecorded, and there is no approved set to exempt.
        assert ConfigSetting.objects.count() == 0
        code, resolved = UserSettings(), get_effective_settings()
        moved = {
            name: (getattr(resolved, name), getattr(code, name))
            for name in self._guarded_fields()
            if getattr(resolved, name) != getattr(code, name)
        }
        assert not moved, f"the resolver moves a field off its dataclass default: {moved}"

    def test_every_collapsed_field_moves_only_to_what_the_tier_prescribes(self) -> None:
        # The exemption above is bounded here: at the shipped ``autonomy = full`` the
        # collapse writes exactly these values, so a collapsed field cannot drift to
        # something the tier never prescribed.
        resolved = get_effective_settings()
        assert resolved.autonomy is Autonomy.FULL
        assert resolved.mode is Mode.AUTO
        assert resolved.require_human_approval_to_answer is False
        assert resolved.review_request_post_disabled is False
        # ``notify_on_behalf`` is the NOTIFY tier's derivation; ``full`` leaves it alone.
        assert resolved.notify_on_behalf is UserSettings().notify_on_behalf
        # #3630: the merge gate is collapsed by no tier, so it stays a guarded field
        # above and keeps its shipped value here.
        assert resolved.require_human_approval_to_merge is True
