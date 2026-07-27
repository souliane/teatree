# test-path: cross-cutting
"""The shipped-``defaults.toml`` DEFAULTS tier in the effective-settings chain.

``config/defaults.toml`` is the shipped code-default layer, so the resolver READS it.
It sits UNDER every override, so per key the chain is

    env -> DB(overlay) -> DB(global) -> overlay code default -> TOML default.

The tier is observable only against a shipped file whose value diverges from the
dataclass default, and the committed file deliberately carries no such divergence
(wiring the tier in is behaviour-neutral by construction). These tests therefore point
the tier at a real fixture ``defaults.toml`` — real stdlib parse, real registry
coercion, real ``ConfigSetting`` rows — and pin both that the TOML value wins over the
dataclass default AND that every override tier still beats it.
"""

import dataclasses
import tempfile
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase

from teatree.config import (
    ENV_SETTING_OVERRIDES,
    OnBehalfPostMode,
    TeaTreeConfig,
    cold_defaults,
    effective_default,
    get_effective_settings,
    mr_reminder_from_table,
    speak_from_subtable,
)
from teatree.config.cold_defaults import DEFAULTS_TOML, shipped_defaults_table
from teatree.config.cold_hook_settings import COLD_HOOK_SETTINGS
from teatree.config.overlay_code_defaults import PROMOTED_OVERLAY_CODE_DEFAULT_KEYS
from teatree.config.registries import COLD_SETTINGS
from teatree.config.resolution import _BESPOKE_STRUCTURED_FIELDS as _STRUCTURED_KEYS
from teatree.config.resolution import _coerce_setting_rows
from teatree.config.settings import UserSettings
from teatree.core.models import ConfigSetting

# Two scalars whose dataclass defaults (85 / 1) the fixture diverges from, plus a
# structured sub-table — the shapes the resolver treats differently. Every other key is
# absent, standing in for the Secret/Personal keys the shipped file omits.
_FIXTURE = """\
[teatree]
provision_ram_ceiling_percent = 42
merge_wip = 7

[teatree.speak]
local = "all"
"""


class TestTomlDefaultTier(TestCase):
    @pytest.fixture(autouse=True)
    def _fixture_defaults(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setenv("T3_OVERLAY_NAME", "t3-teatree")
        monkeypatch.delenv("T3_MERGE_WIP", raising=False)
        toml = tmp_path / "defaults.toml"
        toml.write_text(_FIXTURE, encoding="utf-8")
        monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", toml)
        self.monkeypatch = monkeypatch

    def test_toml_value_wins_over_the_dataclass_default(self) -> None:
        assert ConfigSetting.objects.count() == 0
        assert UserSettings().provision_ram_ceiling_percent == 85
        assert get_effective_settings().provision_ram_ceiling_percent == 42

    def test_db_global_row_beats_the_toml_default(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 60)
        assert get_effective_settings().provision_ram_ceiling_percent == 60

    def test_db_overlay_row_beats_the_toml_default(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 55, scope="t3-teatree")
        assert get_effective_settings().provision_ram_ceiling_percent == 55

    def test_db_overlay_row_beats_a_db_global_row_over_the_toml_default(self) -> None:
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 60)
        ConfigSetting.objects.set_value("provision_ram_ceiling_percent", 55, scope="t3-teatree")
        assert get_effective_settings().provision_ram_ceiling_percent == 55

    def test_env_beats_a_db_row_over_the_toml_default(self) -> None:
        assert get_effective_settings().merge_wip == 7
        ConfigSetting.objects.set_value("merge_wip", 5)
        self.monkeypatch.setenv("T3_MERGE_WIP", "3")
        assert get_effective_settings().merge_wip == 3

    def test_key_absent_from_the_toml_falls_back_to_the_dataclass_default(self) -> None:
        # A Default-category key the file happens to omit, and a Personal key absent from
        # the shipped file by construction — neither raises, both keep the code default.
        settings = get_effective_settings()
        assert settings.session_stale_after_hours == UserSettings().session_stale_after_hours
        assert settings.openai_compatible_model == UserSettings().openai_compatible_model

    def test_structured_sub_table_resolves_from_the_toml_and_a_db_row_still_wins(self) -> None:
        assert get_effective_settings().speak.local == "all"
        ConfigSetting.objects.set_value("speak", {"local": "off"})
        assert get_effective_settings().speak.local == "off"

    def test_effective_default_reports_the_toml_value(self) -> None:
        # The seed-skip / import-skip authority agrees with the resolver, so a row equal
        # to the shipped default stays provably redundant.
        assert effective_default("provision_ram_ceiling_percent") == 42
        assert effective_default("session_stale_after_hours") == UserSettings().session_stale_after_hours


def test_the_stdlib_tier_and_the_model_authority_serve_the_same_shipped_file() -> None:
    # One path constant, one file — the resolver's DEFAULTS tier (stdlib ``cold_defaults``)
    # and ``effective_default`` (the pydantic model) can never disagree about what the
    # shipped default IS.
    assert DEFAULTS_TOML.name == "defaults.toml"
    table = shipped_defaults_table(DEFAULTS_TOML)
    assert effective_default("provision_ram_ceiling_percent") == table["provision_ram_ceiling_percent"]


class TestTierNeverOverwritesTheResolverBase(TestCase):
    """A DEFAULTS tier fills what the base left unset — it never clobbers the base's own value.

    ``get_effective_settings``'s base is ``load_config().user``. Production always hands it
    a bare ``UserSettings()``, but a caller that STAGES a base — the ``load_config`` patch
    seam ~29 test modules use, or any future loader that resolves values itself — carries
    explicit opinions. A tier that sits BELOW every override must not overwrite them.

    This is the class the CI red caught: spreading the shipped table over EVERY field made
    the base inert, so a staged ``review_nag_enabled=True`` silently resolved to the shipped
    ``false`` and the review-nag scanner short-circuited disabled.
    """

    @pytest.fixture(autouse=True)
    def _no_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_staged_base_value_survives_the_toml_default(self) -> None:
        staged = TeaTreeConfig(user=UserSettings(review_nag_enabled=True))
        assert shipped_defaults_table()["review_nag_enabled"] is False
        with mock.patch("teatree.config.load_config", return_value=staged):
            assert get_effective_settings().review_nag_enabled is True

    def test_staged_enum_base_value_survives_the_toml_default(self) -> None:
        staged = TeaTreeConfig(user=UserSettings(on_behalf_post_mode=OnBehalfPostMode.IMMEDIATE))
        assert shipped_defaults_table()["on_behalf_post_mode"] == "draft_or_ask"
        with mock.patch("teatree.config.load_config", return_value=staged):
            assert get_effective_settings().on_behalf_post_mode is OnBehalfPostMode.IMMEDIATE

    def test_a_db_row_still_beats_a_staged_base(self) -> None:
        # The base is a DEFAULTS-tier opinion, not an override: a real row still wins.
        ConfigSetting.objects.set_value("review_nag_enabled", value=False)
        staged = TeaTreeConfig(user=UserSettings(review_nag_enabled=True))
        with mock.patch("teatree.config.load_config", return_value=staged):
            assert get_effective_settings().review_nag_enabled is False

    def test_a_bare_base_still_takes_the_toml_value(self) -> None:
        # Control: the tier is not disabled by this rule — production's bare base still
        # resolves from the shipped file, which is what makes the tier observable at all.
        toml = tmp_defaults_file(self, "[teatree]\nprovision_ram_ceiling_percent = 42\n")
        assert toml.exists()
        assert get_effective_settings().provision_ram_ceiling_percent == 42


def tmp_defaults_file(case: TestCase, text: str) -> Path:
    toml = Path(tempfile.mkdtemp()) / "defaults.toml"
    toml.write_text(text, encoding="utf-8")
    patcher = mock.patch.object(cold_defaults, "DEFAULTS_TOML", toml)
    patcher.start()
    case.addCleanup(patcher.stop)
    return toml


def _shipped_effective(key: str, code: UserSettings) -> object:
    """The value the resolver derives for *key* from the committed file alone.

    The two structured fields arrive as sub-tables, so they resolve through the same
    parsers the resolver rebuilds them with rather than through ``effective_default``
    (which reports them in stored dict form on purpose).
    """
    table = shipped_defaults_table()[key]
    if key == "mr_reminder":
        return mr_reminder_from_table(table)
    if key == "speak":
        return speak_from_subtable(table, base=code.speak)
    return effective_default(key)


class TestResolvedNeutrality(TestCase):
    """The FULLY-RESOLVED settings object is field-for-field identical, in VALUE and TYPE.

    The narrower value-equality guard below compares the shipped table's parsed value
    against the dataclass default. That is necessary but NOT sufficient: an assembled
    value can compare equal while its TYPE differs (``{}`` vs ``()``, ``[]`` vs ``()``,
    ``str`` vs ``StrEnum``), and downstream code that iterates, branches on emptiness, or
    does an identity/type-sensitive comparison then behaves differently on an ``==`` that
    passed. So this guard walks EVERY field of the object the real resolver returns.

    The promoted overlay-code-default keys are excluded: a DIFFERENT, pre-existing tier
    (#36) legitimately moves those, and this PR does not touch it.
    """

    @pytest.fixture(autouse=True)
    def _no_overrides(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for env_var in ENV_SETTING_OVERRIDES:
            monkeypatch.delenv(env_var, raising=False)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_every_resolved_field_matches_the_dataclass_default_in_value_and_type(self) -> None:
        assert ConfigSetting.objects.count() == 0
        code = UserSettings()
        resolved = get_effective_settings()
        drift = {
            field.name: (getattr(resolved, field.name), getattr(code, field.name))
            for field in dataclasses.fields(UserSettings)
            if field.name not in PROMOTED_OVERLAY_CODE_DEFAULT_KEYS
            and (
                getattr(resolved, field.name) != getattr(code, field.name)
                or type(getattr(resolved, field.name)) is not type(getattr(code, field.name))
            )
        }
        assert not drift, f"the resolver moves a field's value or TYPE off its dataclass default: {drift}"


class TestEveryShippedKeyIsPinned:
    """The whole shipped key set is covered — no key sits in an unguarded gap.

    The value-neutrality guard below only reaches keys that are ``UserSettings`` fields,
    which is 173 of the 200 the file carries. The other 27 are read by a different tier,
    so a wrong value there is invisible to that guard. These tests make the partition
    TOTAL: every shipped key must land in exactly one pinned bucket, so a key added to
    the file in a future generator run cannot land in a gap.
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
        # UNSET and applies its own fallback (e.g. `loop_preset._low_power_preset_name`
        # returns DEFAULT_LOW_POWER_PRESET for a blank value). So the shipped value is the
        # UNSET SENTINEL, never the fallback the reader materialises from it — writing the
        # fallback in would make the key read as explicitly set.
        table = shipped_defaults_table()
        materialised = {key: table[key] for key in table if key in COLD_SETTINGS and table[key]}
        assert not materialised, (
            f"a cold key ships a materialised fallback instead of its unset sentinel: {materialised}"
        )


def test_committed_defaults_toml_moves_no_effective_default() -> None:
    """Every shipped value equals its in-code default, so reading the file changes nothing.

    Wiring the tier in must not silently move an effective default. A future generator
    run that adopts a live box value turns this red — the divergence then has to be a
    reviewed decision rather than a side effect of regenerating the file.
    """
    code = UserSettings()
    diverged = {
        key: (_shipped_effective(key, code), getattr(code, key))
        for key in cold_defaults.shipped_defaults_table()
        if hasattr(code, key) and _shipped_effective(key, code) != getattr(code, key)
    }
    assert not diverged, f"defaults.toml moves an effective default: {diverged}"
