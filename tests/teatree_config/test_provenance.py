"""Which tier of the resolution chain actually supplied a setting's effective value.

The defect this exists for: the settings page named the setting's KIND (``default`` /
``personal`` / ``secret``), which reads ``default`` for hundreds of consecutive rows and,
beside a *shipped default* column, is read as "this value came from the default" — on rows
saying the value differs from that default.

Every case below pins ONE tier winning over the ones beneath it, so a precedence order that
drifts out of step with ``resolution.get_effective_settings`` turns this red rather than
quietly crediting the wrong tier.
"""

import os
from unittest import mock

from django.test import TestCase

from teatree.config.cold_defaults import shipped_defaults_table
from teatree.config.provenance import OVERRIDING_SOURCES, ResolvedSetting, ValueSource, resolve_settings
from teatree.core.models import ConfigSetting

#: The `ConfigSetting` read one `resolve_settings` call makes, whatever it is asked for.
_TIER_READ_QUERIES = 1


def _one(key: str, **kwargs: object) -> ResolvedSetting:
    return resolve_settings([key], **kwargs)[key]


class TestTheWinningTierIsNamed(TestCase):
    def test_no_row_anywhere_credits_the_declared_default(self) -> None:
        assert _one("merge_wip").source is ValueSource.CODE_DEFAULT

    def test_a_global_row_beats_the_declared_default(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        resolved = _one("merge_wip")
        assert (resolved.value, resolved.source) == (4, ValueSource.DB_GLOBAL)

    def test_an_overlay_row_beats_the_global_row_in_that_scope(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        ConfigSetting.objects.set_value("merge_wip", 7, scope="demo")
        assert _one("merge_wip", scope="demo").value == 7
        assert _one("merge_wip", scope="demo").source is ValueSource.DB_OVERLAY
        # The control: the same key in the GLOBAL view is untouched by the overlay row.
        assert _one("merge_wip").source is ValueSource.DB_GLOBAL

    def test_an_env_override_beats_every_stored_tier(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        with mock.patch.dict(os.environ, {"T3_MERGE_WIP": "9"}):
            resolved = _one("merge_wip")
        assert (resolved.value, resolved.source) == (9, ValueSource.ENV)

    def test_a_key_no_stored_tier_carries_falls_back_to_the_code_default(self) -> None:
        # A Personal key is absent from the shipped file by construction, so its only
        # value is the in-code dataclass default.
        assert _one("handover_mirror_path").source is ValueSource.CODE_DEFAULT


class TestPersistedOnlyRestrictsTheWalk(TestCase):
    """A FILE cannot express this machine's env or the active overlay's own constants."""

    def test_env_is_invisible_to_a_persisted_only_walk(self) -> None:
        with mock.patch.dict(os.environ, {"T3_MERGE_WIP": "9"}):
            resolved = _one("merge_wip", persisted_only=True)
        assert resolved.source is ValueSource.CODE_DEFAULT
        assert resolved.value != 9

    def test_a_stored_row_is_still_seen(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        assert _one("merge_wip", persisted_only=True).source is ValueSource.DB_GLOBAL


class TestTheDeclaredTierServesWhatTheDeclarationStates(TestCase):
    """The bottom tier answers from the resolver's own default authority, not the dataclass.

    A cold-hook gate and a cold read state their default on a registry entry and have no
    ``UserSettings`` field at all, so a dataclass-only read credits ``code default`` while
    serving ``None`` — a blank gate on the dashboard, and a value the defaults-shape export
    cannot render at all.
    """

    def test_a_shipped_key_with_no_dataclass_field_serves_its_registered_default(self) -> None:
        resolved = _one("answer_first_gate_enabled")
        assert (resolved.value, resolved.source) == (True, ValueSource.CODE_DEFAULT)

    def test_every_shipped_key_resolves_to_what_the_rendered_file_carries(self) -> None:
        # Crosses the render/resolve boundary: the file is RENDERED from the declarations,
        # so an unset key resolving to something else means one of the two sides is lying.
        shipped = shipped_defaults_table()
        resolved = resolve_settings(sorted(shipped), persisted_only=True)
        drift = {key: (resolved[key].value, value) for key, value in shipped.items() if resolved[key].value != value}
        assert not drift, f"the declared tier disagrees with the file rendered from it: {drift}"


class TestIsOverridden(TestCase):
    def test_an_operator_tier_reads_as_overridden(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        assert _one("merge_wip").is_overridden is True

    def test_a_shipped_tier_does_not(self) -> None:
        assert _one("merge_wip").is_overridden is False

    def test_the_overriding_set_is_exactly_the_operator_tiers(self) -> None:
        # A refused env value is the operator's own pin too — wrong, but theirs.
        operator_tiers = {
            ValueSource.ENV,
            ValueSource.INVALID_ENV,
            ValueSource.DB_OVERLAY,
            ValueSource.DB_GLOBAL,
        }
        assert operator_tiers == OVERRIDING_SOURCES


class TestTheTiersAreReadOncePerCall(TestCase):
    def test_a_page_of_keys_costs_no_more_than_a_single_key(self) -> None:
        # The property the settings page's row count depends on: N keys is not N reads.
        with self.assertNumQueries(_TIER_READ_QUERIES):
            resolve_settings(["merge_wip"])
        with self.assertNumQueries(_TIER_READ_QUERIES):
            resolve_settings(["merge_wip", "mode", "wip", "autonomy", "autoload"])

    def test_every_requested_key_comes_back(self) -> None:
        requested = ["merge_wip", "mode", "autoload"]
        assert sorted(resolve_settings(requested)) == sorted(requested)


class TestARefusedEnvValueIsReportedNotRaised(TestCase):
    """The page whose job is surfacing a bad `T3_*` value must not die on one (#4585).

    `env_setting_overrides` raises on a value the parser refuses, and nothing between it
    and the dash settings page caught it — so the one surface that could have named
    `T3_MODE=1` returned a 500 instead.
    """

    def test_the_refused_value_is_shown_with_its_own_source(self) -> None:
        with mock.patch.dict(os.environ, {"T3_MODE": "1"}):
            resolved = _one("mode")

        assert (resolved.value, resolved.source) == ("1", ValueSource.INVALID_ENV)

    def test_a_refused_value_reads_as_an_operator_override(self) -> None:
        # It is the operator's own pin, wrong or not — crediting a shipped tier would say
        # a value is in force that the resolver refuses to produce.
        with mock.patch.dict(os.environ, {"T3_MODE": "1"}):
            assert _one("mode").is_overridden is True

    def test_the_refusal_outranks_every_stored_tier(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive")
        with mock.patch.dict(os.environ, {"T3_MODE": "1"}):
            assert _one("mode").source is ValueSource.INVALID_ENV

    def test_the_other_keys_on_the_page_still_resolve(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        with mock.patch.dict(os.environ, {"T3_MODE": "1"}):
            rows = resolve_settings(["mode", "merge_wip"])

        assert rows["merge_wip"].source is ValueSource.DB_GLOBAL
        assert rows["mode"].source is ValueSource.INVALID_ENV

    def test_a_file_export_still_refuses_a_tier_it_could_not_read(self) -> None:
        # `persisted_only` empties the env tier, so a refusal there changes nothing about
        # what a TOML export writes.
        with mock.patch.dict(os.environ, {"T3_MODE": "1"}):
            assert _one("mode", persisted_only=True).source is not ValueSource.INVALID_ENV
