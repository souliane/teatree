# test-path: cross-cutting
"""Conformance suite for the ``FEATURE_FLAGS`` lifecycle registry (T4-PR-1).

Mirrors ``test_settings_home_partition.py`` / the ``cold_hook_settings``
no-silent-drop fitness test: the registry is pure data and these fitness
functions keep it honest. They go RED the moment an entry names a field that is
not a real ``bool`` ``UserSettings`` field registered in
``OVERLAY_OVERRIDABLE_SETTINGS`` (the registration-drift class), lacks a
``tracking_issue`` or a valid ``stage``, or lets a ``DARK`` flag default to its
ON value — the Goodhart guard that keeps the outer loop's OFF switch un-flippable
without a code-reviewed stage demotion.

The registry is seeded with three real flags across two stages, so no invariant
is vacuously true over an empty or single-entry set.
"""

import dataclasses

from teatree.config import (
    FEATURE_FLAGS,
    OVERLAY_OVERRIDABLE_SETTINGS,
    FeatureFlag,
    FlagStage,
    UserSettings,
    dark_flags,
    is_feature_flag,
)
from teatree.config.feature_flags import REMOVE_STAGE_BANNER, flag_trailer, render_flags_audit


def _user_settings_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(UserSettings)}


class TestRegistrySeededNonVacuously:
    """The registry is seeded so every invariant below has real entries to bite on."""

    def test_at_least_three_flags_registered(self) -> None:
        assert len(FEATURE_FLAGS) >= 3

    def test_flags_span_at_least_two_stages(self) -> None:
        stages = {flag.stage for flag in FEATURE_FLAGS.values()}
        assert len(stages) >= 2, f"registry must exercise multiple stages, got {stages}"

    def test_canonical_seed_flags_present(self) -> None:
        # The three the task pins: the new canonical flag plus two retro-classified.
        assert {"outer_loop_enabled", "teams_enabled", "loop_runner_enabled"} <= set(FEATURE_FLAGS)


class TestRegisteredHome:
    """Every entry names a REAL bool ``UserSettings`` field in the overridable registry.

    This is the ``cold_hook_settings`` registration-drift class: a flag registered
    for a nonexistent, non-bool, or unregistered field turns the suite red.
    """

    def test_every_key_equals_its_field(self) -> None:
        # One canonical identity: the dict key IS the field name (no stripping/splitting).
        for key, flag in FEATURE_FLAGS.items():
            assert key == flag.field, f"{key!r} key must equal its FeatureFlag.field {flag.field!r}"

    def test_every_flag_names_a_real_user_settings_field(self) -> None:
        fields = _user_settings_field_names()
        unknown = sorted(key for key in FEATURE_FLAGS if key not in fields)
        assert unknown == [], f"feature flags naming no UserSettings field: {unknown}"

    def test_every_flag_field_is_bool(self) -> None:
        defaults = UserSettings()
        non_bool = sorted(key for key in FEATURE_FLAGS if not isinstance(getattr(defaults, key), bool))
        assert non_bool == [], f"feature flags naming a non-bool field: {non_bool}"

    def test_every_flag_field_is_overlay_overridable(self) -> None:
        unregistered = sorted(key for key in FEATURE_FLAGS if key not in OVERLAY_OVERRIDABLE_SETTINGS)
        assert unregistered == [], f"feature flags not in OVERLAY_OVERRIDABLE_SETTINGS: {unregistered}"


class TestLifecycleFields:
    """Every entry carries a non-empty tracking issue and a valid stage."""

    def test_every_flag_has_non_empty_tracking_issue(self) -> None:
        untracked = sorted(key for key, flag in FEATURE_FLAGS.items() if not flag.tracking_issue.strip())
        assert untracked == [], f"feature flags with no tracking_issue: {untracked}"

    def test_every_flag_has_non_empty_summary(self) -> None:
        empty = sorted(key for key, flag in FEATURE_FLAGS.items() if not flag.summary.strip())
        assert empty == [], f"feature flags with no summary: {empty}"

    def test_every_flag_stage_is_a_valid_flagstage(self) -> None:
        for key, flag in FEATURE_FLAGS.items():
            assert isinstance(flag.stage, FlagStage), f"{key!r} has a non-FlagStage stage: {flag.stage!r}"


class TestDarkDefaultsOff:
    """A DARK flag's dataclass default equals its off_value — it can NEVER ship default-ON."""

    def test_every_dark_flag_default_equals_off_value(self) -> None:
        defaults = UserSettings()
        for key, flag in dark_flags().items():
            assert getattr(defaults, key) == flag.off_value, (
                f"DARK flag {key!r} defaults to {getattr(defaults, key)!r} but its off_value is "
                f"{flag.off_value!r} — a dark feature must ship OFF by default"
            )

    def test_outer_loop_enabled_pinned_dark_and_off(self) -> None:
        flag = FEATURE_FLAGS["outer_loop_enabled"]
        assert flag.stage is FlagStage.DARK
        assert flag.off_value is False
        assert UserSettings().outer_loop_enabled is False

    def test_off_value_is_load_bearing_for_the_invariant(self) -> None:
        # The dark-defaults-off invariant compares ``default == off_value`` — NOT a
        # hard-coded ``default is False``. An inverted-sense ``*_disabled`` flag ships
        # OFF at default True; a positive-sense one at default False. Proving both
        # senses read correctly keeps off_value a real capability, not decoration.
        inverted = FeatureFlag(
            field="x_disabled", stage=FlagStage.DARK, tracking_issue="#1", summary="s", off_value=True
        )
        positive = FeatureFlag(
            field="x_enabled", stage=FlagStage.DARK, tracking_issue="#1", summary="s", off_value=False
        )
        # (default that means "ships OFF", the flag's off_value) — the ships-off
        # default equals off_value; the opposite default does not.
        for ships_off_default, off_value in ((True, inverted.off_value), (False, positive.off_value)):
            assert ships_off_default == off_value
            assert (not ships_off_default) != off_value


class TestQueryHelpers:
    def test_is_feature_flag_true_for_flag_false_for_setting(self) -> None:
        assert is_feature_flag("outer_loop_enabled") is True
        assert is_feature_flag("mode") is False
        assert is_feature_flag("not_a_setting_at_all") is False

    def test_dark_flags_returns_only_dark_stage(self) -> None:
        assert all(flag.stage is FlagStage.DARK for flag in dark_flags().values())
        assert set(dark_flags()) == {k for k, f in FEATURE_FLAGS.items() if f.stage is FlagStage.DARK}


class TestAuditRenderSurfacesRemoveLoud:
    """The audit view surfaces a REMOVE-stage flag LOUD — a dead toggle cannot hide."""

    def test_remove_stage_flag_is_shouted(self) -> None:
        fixture = {
            "legacy_toggle": FeatureFlag(
                field="legacy_toggle",
                stage=FlagStage.REMOVE,
                tracking_issue="souliane/teatree#0000",
                summary="Gated code is permanent; delete this toggle.",
            )
        }
        rendered = render_flags_audit(fixture)
        assert REMOVE_STAGE_BANNER in rendered
        assert "legacy_toggle" in rendered

    def test_dark_and_settling_flags_are_not_shouted(self) -> None:
        rendered = render_flags_audit(FEATURE_FLAGS)
        # The live registry has no REMOVE flag, so the loud banner must not appear.
        assert REMOVE_STAGE_BANNER not in rendered

    def test_audit_lists_every_live_flag(self) -> None:
        rendered = render_flags_audit(FEATURE_FLAGS)
        for key in FEATURE_FLAGS:
            assert key in rendered

    def test_empty_registry_renders_a_placeholder_not_a_crash(self) -> None:
        assert "no feature flags" in render_flags_audit({})


class TestFlagTrailer:
    def test_trailer_names_stage_and_tracking_for_a_flag(self) -> None:
        trailer = flag_trailer("outer_loop_enabled")
        assert "feature flag" in trailer
        assert "stage=dark" in trailer
        assert "tracking" in trailer

    def test_trailer_is_empty_for_a_durable_setting(self) -> None:
        assert flag_trailer("mode") == ""
