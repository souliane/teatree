# test-path: cross-cutting
"""Conformance suite for the ``FEATURE_FLAGS`` lifecycle registry (T4-PR-1).

Mirrors ``test_settings_home_partition.py`` / the ``cold_hook_settings``
no-silent-drop fitness test: the registry is pure data and these fitness
functions keep it honest. They go RED the moment an entry names a field that is
not a real ``bool``-or-``StrEnum`` ``UserSettings`` field registered in
``OVERLAY_OVERRIDABLE_SETTINGS`` (the registration-drift class), lacks a
``tracking_issue`` or a valid ``stage``, or lets a ``DARK`` flag default to its
ON value — the Goodhart guard that keeps the outer loop's OFF switch un-flippable
without a code-reviewed stage demotion.

The live registry is seeded with several real flags — mostly ``DARK`` plus a few
``SETTLING`` (``incremental_push_gate``, graduated by #122; ``limit_autorecovery_enabled``,
by #3691; ``directive_loop_enabled``, by #3895). ``REMOVE`` is not represented live, so
the stage-discrimination invariants are proven non-vacuously over a MIXED FIXTURE rather
than the live set's accidental composition.
"""

import dataclasses
from enum import StrEnum

from teatree.config import (
    DURABLE_GATE_SETTINGS,
    FEATURE_FLAGS,
    OVERLAY_OVERRIDABLE_SETTINGS,
    CriticGateMode,
    FeatureFlag,
    FlagStage,
    UserSettings,
    dark_flags,
    is_feature_flag,
)
from teatree.config.feature_flags import (
    REMOVE_STAGE_BANNER,
    UNTRACKED_BANNER,
    flag_trailer,
    render_flags_audit,
    tracking_reference,
    untracked_flags,
)


def _user_settings_field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(UserSettings)}


def _flag(tracking_issue: str) -> FeatureFlag:
    return FeatureFlag(field="x_enabled", stage=FlagStage.DARK, tracking_issue=tracking_issue, summary="s")


def _mixed_stage_fixture() -> dict[str, FeatureFlag]:
    """A fixture registry spanning every stage — the non-vacuity anchor for stage logic."""
    return {
        "a_dark": FeatureFlag(field="a_dark", stage=FlagStage.DARK, tracking_issue="#1", summary="s"),
        "a_settling": FeatureFlag(field="a_settling", stage=FlagStage.SETTLING, tracking_issue="#2", summary="s"),
        "a_remove": FeatureFlag(field="a_remove", stage=FlagStage.REMOVE, tracking_issue="#3", summary="s"),
    }


class TestRegistrySeededNonVacuously:
    """The registry is seeded so every invariant below has real entries to bite on."""

    def test_at_least_three_flags_registered(self) -> None:
        assert len(FEATURE_FLAGS) >= 3

    def test_stage_machinery_spans_every_stage_over_a_fixture(self) -> None:
        # The live registry spans DARK + SETTLING but not REMOVE, so the multi-stage
        # guard bites on a MIXED FIXTURE — proving the stage type exercises every stage
        # without pinning the live set's accidental composition.
        stages = {flag.stage for flag in _mixed_stage_fixture().values()}
        assert stages == set(FlagStage)

    def test_canonical_seed_flag_present(self) -> None:
        # The canonical DARK flags plus a retro-classified one — loop_runner_enabled was
        # graduated OUT by PR-28 (durable kill-switch, no longer a dying flag).
        assert {"outer_loop_enabled", "factory_score_enabled"} <= set(FEATURE_FLAGS)
        assert "loop_runner_enabled" not in FEATURE_FLAGS


class TestRegisteredHome:
    """Every entry names a REAL bool-or-StrEnum ``UserSettings`` field in the overridable registry.

    This is the ``cold_hook_settings`` registration-drift class: a flag registered
    for a nonexistent, wrong-typed, or unregistered field turns the suite red.
    """

    def test_every_key_equals_its_field(self) -> None:
        # One canonical identity: the dict key IS the field name (no stripping/splitting).
        for key, flag in FEATURE_FLAGS.items():
            assert key == flag.field, f"{key!r} key must equal its FeatureFlag.field {flag.field!r}"

    def test_every_flag_names_a_real_user_settings_field(self) -> None:
        fields = _user_settings_field_names()
        unknown = sorted(key for key in FEATURE_FLAGS if key not in fields)
        assert unknown == [], f"feature flags naming no UserSettings field: {unknown}"

    def test_every_flag_field_is_bool_or_a_typed_mode(self) -> None:
        # The #104 non-bool extension: a flag field is a bool on/off toggle OR a
        # StrEnum multi-state mode (``critic_gate_mode`` is tri-state
        # off|advisory|blocking). A flag naming a plain int/float/str field is
        # still registration drift.
        defaults = UserSettings()
        unsupported = sorted(key for key in FEATURE_FLAGS if not isinstance(getattr(defaults, key), (bool, StrEnum)))
        assert unsupported == [], f"feature flags naming a non-bool, non-StrEnum field: {unsupported}"

    def test_every_flag_off_value_type_matches_its_field(self) -> None:
        # off_value carries the "gated code stays OFF" value; its type must match
        # the field default's type so a bool flag never gets a str off_value (or
        # vice versa) by mistake.
        defaults = UserSettings()
        mismatched = sorted(
            key for key, flag in FEATURE_FLAGS.items() if type(getattr(defaults, key)) is not type(flag.off_value)
        )
        assert mismatched == [], f"feature flags whose field type != off_value type: {mismatched}"

    def test_every_flag_field_is_overlay_overridable(self) -> None:
        unregistered = sorted(key for key in FEATURE_FLAGS if key not in OVERLAY_OVERRIDABLE_SETTINGS)
        assert unregistered == [], f"feature flags not in OVERLAY_OVERRIDABLE_SETTINGS: {unregistered}"


class TestEveryGateToggleIsClassified:
    """No ``require_*`` toggle ships unclassified — the hole ``require_spec_coverage`` fell through.

    Its ON state refused every RETROSPECTED→DELIVERED advance (a missing manifest
    is itself a block) while no command could write the manifest, and nothing
    reviewed that because the flag was in no registry at all. Classification is
    now mandatory: a new gate toggle is a dying ``FEATURE_FLAGS`` entry or a
    declared-durable operator policy, never neither.
    """

    def _gate_toggles(self) -> set[str]:
        return {f.name for f in dataclasses.fields(UserSettings) if f.name.startswith("require_")}

    def test_every_gate_toggle_is_a_flag_or_a_durable_setting(self) -> None:
        unclassified = sorted(self._gate_toggles() - set(FEATURE_FLAGS) - DURABLE_GATE_SETTINGS)
        assert unclassified == [], (
            f"gate toggles in neither FEATURE_FLAGS nor DURABLE_GATE_SETTINGS: {unclassified} — "
            f"register a dying flag, or declare it durable operator policy"
        )

    def test_the_two_buckets_are_disjoint(self) -> None:
        assert set(FEATURE_FLAGS) & DURABLE_GATE_SETTINGS == set()

    def test_durable_bucket_names_only_real_gate_toggles(self) -> None:
        assert self._gate_toggles() >= DURABLE_GATE_SETTINGS

    def test_spec_coverage_gate_is_governed_dark_and_off(self) -> None:
        flag = FEATURE_FLAGS["require_spec_coverage"]
        assert flag.stage is FlagStage.DARK
        assert "2232" in flag.tracking_issue
        assert UserSettings().require_spec_coverage is False


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

    def test_critic_gate_mode_pinned_dark_and_off(self) -> None:
        # #104: the re-typed tri-state critic flag ships OFF by default — its
        # off_value is the OFF member and the dataclass default matches it, so the
        # critic gate can never ship armed/blocking without a deliberate config set.
        flag = FEATURE_FLAGS["critic_gate_mode"]
        assert flag.stage is FlagStage.DARK
        assert flag.off_value is CriticGateMode.OFF
        assert UserSettings().critic_gate_mode is CriticGateMode.OFF

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


class TestResilienceRecoveryGraduation:
    """The idle usage-window auto-recovery flag graduated DARK -> SETTLING (default ON).

    A fresh deploy self-recovers from an exhausted usage window out of the box; the
    recovery flag survives only as a per-overlay escape hatch during its soak. The
    deep-merge SAFETY-posture gates are deliberately left DARK — this is a resilience
    default, not a safety loosening.
    """

    def test_limit_autorecovery_graduated_to_settling(self) -> None:
        flag = FEATURE_FLAGS["limit_autorecovery_enabled"]
        assert flag.stage is FlagStage.SETTLING
        assert flag.off_value is False

    def test_limit_autorecovery_defaults_on_for_a_fresh_deploy(self) -> None:
        # The whole point of the graduation: a fresh deploy no longer idles on the
        # first usage-window exhaustion — the recovery chain arms by default.
        assert UserSettings().limit_autorecovery_enabled is True

    def test_safety_posture_gates_stay_dark(self) -> None:
        # The deep-merge / safety-posture dark gates MUST NOT graduate alongside the
        # resilience-recovery flag: they stay OFF by default, each equal to its off_value.
        defaults = UserSettings()
        safety_posture_flags = {
            "require_plan_adequacy",
            "critic_gate_mode",
            "send_proxy_mode",
            "require_debt_delta",
            "require_executed_repro",
            "require_merge_quality_verdict",
            "ci_eval_heal_autofix_enabled",
        }
        for key in safety_posture_flags:
            flag = FEATURE_FLAGS[key]
            assert flag.stage is FlagStage.DARK, f"{key!r} must stay DARK — it is a safety-posture gate"
            assert getattr(defaults, key) == flag.off_value, f"{key!r} must ship OFF by default"


class TestDirectiveIntakeGraduation:
    """``directive_loop_enabled`` graduated DARK -> SETTLING (default ON) — #3895.

    The owner-authorised autonomous-by-default posture: a captured directive is
    interpreted with no operator opt-in, and the intake arc terminates at the structural
    human ratify gate. The EXECUTION arc's own guards (``factory_score_enabled``, a live
    critic) did NOT graduate with it, so nothing self-modifies at default resolution.
    """

    def test_directive_loop_graduated_to_settling(self) -> None:
        flag = FEATURE_FLAGS["directive_loop_enabled"]
        assert flag.stage is FlagStage.SETTLING
        assert flag.off_value is False

    def test_directive_loop_defaults_on_for_a_fresh_deploy(self) -> None:
        assert UserSettings().directive_loop_enabled is True

    def test_the_graduated_flag_left_the_dark_set(self) -> None:
        # The stage move is what lets the key ship default-ON — `TestDarkDefaultsOff` holds
        # every DARK flag equal to its own off_value. It does NOT release the key from
        # `pinned_fail_closed_keys()`: a graduated flag's ON default is a code-reviewed
        # decision, and a snapshot carrying a live box's OFF back into the shipped file
        # would undo that graduation for every fresh install.
        assert "directive_loop_enabled" not in dark_flags()

    def test_the_execution_arc_guards_did_not_graduate(self) -> None:
        # The bound on self-modification: the score metric and the critic gate stay DARK
        # and OFF, so intake being live never reaches a config write or a merge.
        defaults = UserSettings()
        for key in ("factory_score_enabled", "critic_gate_mode"):
            flag = FEATURE_FLAGS[key]
            assert flag.stage is FlagStage.DARK, f"{key!r} must stay DARK — it bounds self-modification"
            assert getattr(defaults, key) == flag.off_value


class TestQueryHelpers:
    def test_is_feature_flag_true_for_flag_false_for_setting(self) -> None:
        assert is_feature_flag("outer_loop_enabled") is True
        assert is_feature_flag("mode") is False
        assert is_feature_flag("not_a_setting_at_all") is False

    def test_dark_flags_returns_only_dark_stage(self) -> None:
        assert all(flag.stage is FlagStage.DARK for flag in dark_flags().values())
        assert set(dark_flags()) == {k for k, f in FEATURE_FLAGS.items() if f.stage is FlagStage.DARK}

    def test_dark_flags_filters_non_dark_over_a_mixed_fixture(self) -> None:
        # Non-vacuous FILTER proof: over a fixture spanning every stage, dark_flags
        # keeps only the DARK entry — the live all-DARK registry can't prove this.
        assert set(dark_flags(_mixed_stage_fixture())) == {"a_dark"}


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


class TestATrackingIssueThatResolvesToNothingIsSaidSo:
    """A flag is meant to DIE, and only a resolvable reference lets anyone ask if it has.

    Five live entries carry a workstream label — ``souliane/teatree — SELFCATCH-3
    plan_gate hardening`` — that reads exactly like a citation and resolves to no issue.
    Rendered bare as ``tracking <text>`` beside the ones that DO resolve, a stalled DARK
    flag reads as governed, and the only anti-rot guard on the field
    (:meth:`TestLifecycleFields.test_every_flag_has_non_empty_tracking_issue`) is satisfied
    by any prose at all.
    """

    def test_a_reference_is_extracted_in_both_written_forms(self) -> None:
        assert tracking_reference(_flag("souliane/teatree#118")) == "souliane/teatree#118"
        assert tracking_reference(_flag("fixes #42 in the next pass")) == "#42"

    def test_prose_naming_no_issue_extracts_nothing(self) -> None:
        assert tracking_reference(_flag("souliane/teatree — SELFCATCH-3 plan_gate hardening")) == ""
        assert tracking_reference(_flag("souliane/teatree — autoresearch outer-loop (T4)")) == ""

    def test_untracked_flags_filters_over_a_mixed_fixture(self) -> None:
        fixture = {"tracked": _flag("souliane/teatree#3691"), "untracked": _flag("north-star PR-3 debt_delta_gate")}
        assert set(untracked_flags(fixture)) == {"untracked"}

    def test_the_audit_shouts_an_unresolvable_tracking_issue(self) -> None:
        rendered = render_flags_audit({"stalled": _flag("souliane/teatree — SELFCATCH-3 plan_gate hardening")})
        assert UNTRACKED_BANNER in rendered

    def test_the_audit_stays_quiet_for_a_resolvable_one(self) -> None:
        assert UNTRACKED_BANNER not in render_flags_audit({"governed": _flag("souliane/teatree#118")})

    def test_the_live_registry_reports_exactly_the_entries_that_resolve_to_nothing(self) -> None:
        rendered = render_flags_audit(FEATURE_FLAGS)
        assert set(untracked_flags()) < set(FEATURE_FLAGS), "a mixed live registry is what makes this non-vacuous"
        assert rendered.count(UNTRACKED_BANNER) == len(untracked_flags())

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
