# test-path: cross-cutting
"""The two export filters, and the byte-identical ``defaults.toml`` round trip they buy.

``config/defaults.toml`` is the shipped-default FLOOR and the ``ConfigSetting`` store holds
only DELTAS from it, so a dump of the store and the shipped file were never inverses: a
value equal to the shipped default writes no row, which means importing ``defaults.toml``
yields zero rows and exporting them again emits no ``[teatree]`` table at all.

Two independent filters close that. *default keys only* chooses which keys are ELIGIBLE
(the ``Category.DEFAULT`` set the file carries); *include defaults* chooses divergent-only
versus ALL of them. Ticking both is the defaults shape, and the round trip below is the
guarantee it exists for — asserted on the emitted key set with EQUALITY, because a subset
check still lets a key vanish silently, which is exactly what makes "export, then drop the
file over ``config/defaults.toml``" dangerous.
"""

import os
import tomllib
from unittest import mock

from django.test import TestCase

from teatree.config import defaults_snapshot
from teatree.config.cold_defaults import DEFAULTS_TOML, flatten_settings_table
from teatree.config.defaults_snapshot import default_category_keys
from teatree.config.setting_help import setting_help
from teatree.core import config_migration
from teatree.core.config_migration import export_db_to_toml, import_toml_to_db
from teatree.core.models import ConfigSetting

_SHIPPED_TEXT = DEFAULTS_TOML.read_text(encoding="utf-8")


def _teatree(text: str) -> dict[str, object]:
    return flatten_settings_table(tomllib.loads(text).get("teatree", {}))


class TestBothFiltersOffChangesNothing(TestCase):
    """Both unticked reproduces the dump's CONTENT exactly — the filters are additive.

    Content, not bytes: nesting the ``[teatree]`` table is a separate deliverable of the
    same change and it re-shapes every TOML surface, so the pre-change dump's banner
    comments are gone from an unfiltered dump too. What "unticked changes nothing" means
    is what the filters themselves govern — the emitted KEY SET is still the DB deltas
    alone, with no shipped-default filler and every sibling table the dump has always had.
    """

    def setUp(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("merge_wip", 4)
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo")

    def test_the_filters_default_to_off(self) -> None:
        assert (
            export_db_to_toml(scan_terms=()).toml
            == export_db_to_toml(scan_terms=(), default_keys_only=False, include_defaults=False).toml
        )

    def test_the_unfiltered_dump_is_exactly_the_overridden_rows(self) -> None:
        # The golden: an unfiltered dump carries the DB deltas and nothing else — no
        # shipped-default filler, and every sibling table the dump has always emitted.
        dump = export_db_to_toml(scan_terms=()).toml
        assert _teatree(dump) == {"merge_wip": 4, "mode": "auto"}
        assert tomllib.loads(dump)["overlays"] == {"demo": {"issue_implementer_label": "scoped"}}

    def test_the_unfiltered_dump_renders_the_deltas_in_the_nested_shape(self) -> None:
        # Each `[teatree]` key carries its authored help text as a trailing comment; the
        # sentences are read from the one table that authors them, never re-typed here.
        assert export_db_to_toml(scan_terms=()).toml == (
            '[teatree.Agents."Mode & harness"]\n'
            f'mode = "auto" # {setting_help("mode")}\n'
            "\n"
            '[teatree.Loops."Cadence & throughput"]\n'
            f"merge_wip = 4 # {setting_help('merge_wip')}\n"
            "\n"
            "[overlays.demo]\n"
            'issue_implementer_label = "scoped"\n'
        )


class TestFilterOneRestrictsTheEligibleKeys(TestCase):
    """*Export default keys only* drops registries, secrets, identifiers and overlay scopes."""

    def test_a_secret_row_is_out_of_scope_even_with_include_private(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["acmebrand"])
        ConfigSetting.objects.set_value("mode", "auto")
        dump = export_db_to_toml(include_private=True, scan_terms=(), default_keys_only=True).toml
        assert "banned_brands" not in _teatree(dump)
        assert _teatree(dump)["mode"] == "auto"

    def test_an_overlay_scope_is_dropped(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_label", "scoped", scope="demo")
        ConfigSetting.objects.set_value("mode", "auto")
        assert "overlays" not in tomllib.loads(export_db_to_toml(scan_terms=(), default_keys_only=True).toml)

    def test_a_registry_key_is_dropped(self) -> None:
        ConfigSetting.objects.set_value("e2e_repos", {"demo": {"url": "https://example.invalid/x"}})
        ConfigSetting.objects.set_value("mode", "auto")
        assert "e2e_repos" not in tomllib.loads(export_db_to_toml(scan_terms=(), default_keys_only=True).toml)

    def test_the_filter_alone_still_emits_only_the_divergent_keys(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        assert set(_teatree(export_db_to_toml(scan_terms=(), default_keys_only=True).toml)) == {"mode"}


class TestFilterTwoEmitsTheUnchangedKeysToo(TestCase):
    """*Include values that are the same as default* fills every key with no DB row."""

    def test_a_key_with_no_row_is_emitted_at_its_shipped_value(self) -> None:
        emitted = _teatree(export_db_to_toml(scan_terms=(), include_defaults=True).toml)
        assert emitted["merge_wip"] == flatten_settings_table(tomllib.loads(_SHIPPED_TEXT)["teatree"])["merge_wip"]

    def test_a_db_row_still_wins_over_the_shipped_value(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        assert _teatree(export_db_to_toml(scan_terms=(), include_defaults=True).toml)["merge_wip"] == 4

    def test_the_filter_alone_keeps_the_wider_eligible_key_set(self) -> None:
        # Without filter 1 the eligible set is every ``[teatree]``-emittable key, so a
        # Secret key carrying a row still rides along under ``include_private``.
        ConfigSetting.objects.set_value("banned_brands", ["acmebrand"])
        emitted = set(_teatree(export_db_to_toml(include_private=True, scan_terms=(), include_defaults=True).toml))
        assert emitted == set(default_category_keys()) | {"banned_brands"}

    def test_a_key_no_persisted_tier_reaches_is_left_out_never_invented(self) -> None:
        # A Secret/Personal key with no row and no shipped value has only an in-code
        # dataclass default, which is not in stored form and was never in this table.
        emitted = set(_teatree(export_db_to_toml(include_private=True, scan_terms=(), include_defaults=True).toml))
        assert "handover_mirror_path" not in emitted

    def test_a_process_env_override_is_never_baked_into_the_file(self) -> None:
        # ``T3_*`` is machine-local state. A file every fresh install reads must not carry
        # it, so the fill walks only the tiers a file can express.
        shipped = flatten_settings_table(tomllib.loads(_SHIPPED_TEXT)["teatree"])
        with mock.patch.dict(os.environ, {"T3_MERGE_WIP": "9"}):
            emitted = _teatree(export_db_to_toml(scan_terms=(), include_defaults=True).toml)
        assert emitted["merge_wip"] == shipped["merge_wip"] != 9


class TestBothFiltersAreTheDefaultsShape(TestCase):
    def test_the_emitted_key_set_equals_the_default_category_set(self) -> None:
        # EQUALITY, not a subset: a subset check still passes while a key vanishes, and a
        # vanished key is what makes replacing the shipped file destructive.
        dump = export_db_to_toml(scan_terms=(), default_keys_only=True, include_defaults=True).toml
        assert set(_teatree(dump)) == set(default_category_keys())

    def test_a_redacted_default_key_falls_back_to_its_public_shipped_value(self) -> None:
        # The shape is only meaningful COMPLETE. A DEFAULT key whose live value trips the
        # content scan keeps its shipped value — public by construction — and is reported.
        ConfigSetting.objects.set_value("issue_implementer_label", "acmecorp-ready")
        result = export_db_to_toml(scan_terms=("acmecorp",), default_keys_only=True, include_defaults=True)
        shipped = flatten_settings_table(tomllib.loads(_SHIPPED_TEXT)["teatree"])
        assert set(_teatree(result.toml)) == set(default_category_keys())
        assert _teatree(result.toml)["issue_implementer_label"] == shipped["issue_implementer_label"]
        assert [row.key for row in result.redacted] == ["issue_implementer_label"]

    def test_a_live_override_reaches_the_shape(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        dump = export_db_to_toml(scan_terms=(), default_keys_only=True, include_defaults=True).toml
        assert _teatree(dump)["merge_wip"] == 4


class TestTheByteIdenticalRoundTrip(TestCase):
    """``export(defaults-shape)`` after ``import(defaults.toml)`` reproduces the file exactly."""

    def test_importing_the_shipped_file_writes_no_row(self) -> None:
        # The premise the round trip rests on, stated: every shipped value IS the effective
        # default, so importing the file is a no-op on the store.
        result = import_toml_to_db(_SHIPPED_TEXT, scan_terms=(), allow_safety_posture=True)
        assert result.rejected == ()
        assert result.written == ()
        assert ConfigSetting.objects.count() == 0

    def test_the_round_trip_is_byte_for_byte(self) -> None:
        import_toml_to_db(_SHIPPED_TEXT, scan_terms=(), allow_safety_posture=True)
        exported = export_db_to_toml(scan_terms=(), default_keys_only=True, include_defaults=True).toml
        assert exported == _SHIPPED_TEXT

    def test_the_round_trip_carries_the_header_and_every_seed_table(self) -> None:
        exported = export_db_to_toml(scan_terms=(), default_keys_only=True, include_defaults=True).toml
        assert set(tomllib.loads(exported)) == {"teatree", "loops", "modes", "schedules"}
        assert exported.startswith("# teatree shipped defaults")
        assert exported.endswith("\n")

    def test_a_live_override_is_the_only_thing_that_moves(self) -> None:
        # The control for the assertion above: with a row in the store the round trip is
        # NOT byte-identical, and the single differing line is that row's.
        ConfigSetting.objects.set_value("merge_wip", 4)
        exported = export_db_to_toml(scan_terms=(), default_keys_only=True, include_defaults=True).toml
        differing = [
            line
            for line, shipped in zip(exported.splitlines(), _SHIPPED_TEXT.splitlines(), strict=True)
            if line != shipped
        ]
        assert differing == [f"merge_wip = 4 # {setting_help('merge_wip')}"]


class TestOneEmitterForBothWriters(TestCase):
    """The shipped file and the defaults-shape export come from ONE writer, not two."""

    def test_the_export_renders_through_the_snapshot_emitter(self) -> None:
        assert config_migration.render_shipped_file is defaults_snapshot.render_toml

    def test_the_two_callers_produce_identical_text_for_the_same_values(self) -> None:
        shipped = flatten_settings_table(tomllib.loads(_SHIPPED_TEXT)["teatree"])
        exported = export_db_to_toml(scan_terms=(), default_keys_only=True, include_defaults=True).toml
        assert exported == defaults_snapshot.render_toml(shipped, base_text=_SHIPPED_TEXT)
