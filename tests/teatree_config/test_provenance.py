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

from teatree.config.provenance import (
    OVERRIDING_SOURCES,
    PERSISTED_SOURCES,
    ResolvedSetting,
    ValueSource,
    resolve_settings,
)
from teatree.core.models import ConfigSetting

#: The `ConfigSetting` read one `resolve_settings` call makes, whatever it is asked for.
_TIER_READ_QUERIES = 1


def _one(key: str, **kwargs: object) -> ResolvedSetting:
    return resolve_settings([key], **kwargs)[key]


class TestTheWinningTierIsNamed(TestCase):
    def test_no_row_anywhere_credits_the_shipped_file(self) -> None:
        assert _one("merge_wip").source is ValueSource.SHIPPED_FILE

    def test_a_global_row_beats_the_shipped_file(self) -> None:
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
        assert resolved.source is ValueSource.SHIPPED_FILE
        assert resolved.value != 9

    def test_a_stored_row_is_still_seen(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        assert _one("merge_wip", persisted_only=True).source is ValueSource.DB_GLOBAL

    def test_every_source_a_persisted_walk_can_return_is_persisted(self) -> None:
        sources = {entry.source for entry in resolve_settings(["merge_wip", "mode"], persisted_only=True).values()}
        assert sources <= PERSISTED_SOURCES


class TestIsOverridden(TestCase):
    def test_an_operator_tier_reads_as_overridden(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        assert _one("merge_wip").is_overridden is True

    def test_a_shipped_tier_does_not(self) -> None:
        assert _one("merge_wip").is_overridden is False

    def test_the_overriding_set_is_exactly_the_operator_tiers(self) -> None:
        assert {ValueSource.ENV, ValueSource.DB_OVERLAY, ValueSource.DB_GLOBAL} == OVERRIDING_SOURCES


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
