"""``ConfigSetting`` store -> TOML export service + its leak/secret guards.

The DB-home store is the single source of truth; ``export_db_to_toml`` serialises
it to TOML for a personal, never-shared backup. Integration-first against the real
DB: global rows render under ``[teatree]``, each overlay scope under
``[overlays.<name>]``, and the export guard withholds secret/tainted rows so a
shared export never leaks a codename.
"""

import os
import tomllib
from unittest import mock

from django.test import TestCase

from teatree.config import COLD_HOOK_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS
from teatree.core.config_migration import export_db_to_toml
from teatree.core.models import ConfigSetting


class TestExportDbToToml(TestCase):
    """``ConfigSetting`` store -> TOML export — the precise inverse of import (PR6).

    Serialises the DB override store back to TOML so the import/export pair is a
    full round-trip interchange: global rows -> ``[teatree]``, each overlay scope
    -> ``[overlays.<name>]``, each stored value rendered as its native TOML scalar.
    """

    def test_global_rows_render_under_teatree_table(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 3)
        doc = tomllib.loads(export_db_to_toml(scan_terms=()).toml)
        assert doc["teatree"]["mode"] == "auto"
        assert doc["teatree"]["issue_implementer_max_concurrent"] == 3

    def test_overlay_rows_render_under_overlays_name_table(self) -> None:
        ConfigSetting.objects.set_value("mode", "interactive", scope="myproj")
        doc = tomllib.loads(export_db_to_toml(scan_terms=()).toml)
        assert doc["overlays"]["myproj"]["mode"] == "interactive"
        # An overlay-only store carries no global [teatree] table.
        assert "teatree" not in doc

    def test_native_scalar_types_round_trip(self) -> None:
        # Each JSON-stored value decodes to its native TOML scalar, not a string.
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 5)
        ConfigSetting.objects.set_value("issue_implementer_label", "ready")
        ConfigSetting.objects.set_value("excluded_skills", ["foo", "bar"])
        teatree = tomllib.loads(export_db_to_toml(scan_terms=()).toml)["teatree"]
        assert teatree["issue_implementer_enabled"] is True
        assert teatree["issue_implementer_max_concurrent"] == 5
        assert isinstance(teatree["issue_implementer_max_concurrent"], int)
        assert teatree["issue_implementer_label"] == "ready"
        assert teatree["excluded_skills"] == ["foo", "bar"]

    def test_overlay_filter_dumps_only_that_overlay(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")  # global
        ConfigSetting.objects.set_value("mode", "interactive", scope="myproj")
        ConfigSetting.objects.set_value("mode", "auto", scope="other")
        doc = tomllib.loads(export_db_to_toml(overlay="myproj", scan_terms=()).toml)
        assert doc["overlays"]["myproj"]["mode"] == "interactive"
        assert "teatree" not in doc
        assert "other" not in doc["overlays"]

    def test_empty_store_exports_empty_document(self) -> None:
        assert export_db_to_toml(scan_terms=()).toml.strip() == ""


class TestBannedTermsNeverLeaveTheStoreViaExport(TestCase):
    """The secret banned-terms/brands list is DB-home but never reaches a SHARED export.

    Codename lists moved into the ``ConfigSetting`` store (the DB is personal); the
    leak surface is the export path, guarded by ``SECRET_SETTINGS`` — a shared
    ``config_setting export`` withholds the row so no codename is dumped. All terms
    here are SYNTHETIC, so this public test leaks nothing.
    """

    def test_banned_terms_keys_are_not_in_the_overridable_or_cold_hook_registries(self) -> None:
        # They are DB-home via the COLD_SETTINGS registry, not the overridable /
        # cold-hook settings partitions.
        for key in ("banned_terms", "banned_brands", "banned_terms_allowlist"):
            assert key not in OVERLAY_OVERRIDABLE_SETTINGS
            assert key not in COLD_HOOK_SETTINGS

    def test_export_withholds_a_stored_brand_row(self) -> None:
        ConfigSetting.objects.set_value("banned_terms", ["acmebrand"])
        ConfigSetting.objects.set_value("mode", "auto")
        dump = export_db_to_toml(scan_terms=()).toml
        assert "acmebrand" not in dump
        assert "banned_terms" not in dump
        # The legitimate operational key still exports.
        assert tomllib.loads(dump)["teatree"]["mode"] == "auto"


class TestExportSecretGuard(TestCase):
    """The export secret guard withholds private rows from a SHARED config dump.

    Two complementary defenses, BOTH required: the ``SECRET_SETTINGS`` private-key
    denylist AND an active banned-term scan over every key+value (which catches a
    non-listed key whose VALUE carries a customer term — the case a static keylist
    can never enumerate). ``include_private`` bypasses both for a personal backup.
    All terms here are SYNTHETIC, so this public test leaks nothing.
    """

    def test_private_key_is_withheld_by_default(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["acmebrand"])
        ConfigSetting.objects.set_value("mode", "auto")
        result = export_db_to_toml(scan_terms=())
        doc = tomllib.loads(result.toml)
        assert doc["teatree"]["mode"] == "auto"
        assert "banned_brands" not in doc["teatree"]
        assert [(r.key, r.reason) for r in result.redacted] == [("banned_brands", "private-key")]

    def test_value_carrying_a_banned_term_is_withheld_by_content_scan(self) -> None:
        ConfigSetting.objects.set_value("ban_close_trailers_on_namespaces", ["acmecorp"], scope="proj")
        result = export_db_to_toml(scan_terms=("acmecorp",))
        doc = tomllib.loads(result.toml)
        assert "overlays" not in doc  # the scope's only row was withheld
        assert len(result.redacted) == 1
        assert result.redacted[0].key == "ban_close_trailers_on_namespaces"
        assert result.redacted[0].reason == "banned-term:acmecorp"

    def test_include_private_exports_everything(self) -> None:
        ConfigSetting.objects.set_value("banned_brands", ["acmebrand"])
        ConfigSetting.objects.set_value("ban_close_trailers_on_namespaces", ["acmecorp"])
        result = export_db_to_toml(include_private=True, scan_terms=("acmecorp", "acmebrand"))
        teatree = tomllib.loads(result.toml)["teatree"]
        assert teatree["banned_brands"] == ["acmebrand"]
        assert teatree["ban_close_trailers_on_namespaces"] == ["acmecorp"]
        assert result.redacted == ()

    def test_clean_rows_are_untouched_by_the_scan(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("excluded_skills", ["foo"])
        result = export_db_to_toml(scan_terms=("acmecorp",))
        teatree = tomllib.loads(result.toml)["teatree"]
        assert teatree["mode"] == "auto"
        assert teatree["excluded_skills"] == ["foo"]
        assert result.redacted == ()


class TestExportScanTermsResolveFailsSafe(TestCase):
    """``export_db_to_toml(scan_terms=None)`` fails SAFE when the live config has no terms.

    The DEFAULT machine state — no ``banned_terms`` configured and no
    ``T3_BANNED_TERMS`` env — makes ``resolve_banned_terms`` raise
    ``BannedTermsUnsetError``. The export's live-resolve path (``scan_terms=None``,
    the production ``config_setting export`` caller) must degrade to an EMPTY
    scan-term list rather than propagate the raise. Every other export test passes
    ``scan_terms`` explicitly, so this live-resolve path is otherwise uncovered.
    """

    def test_export_does_not_crash_when_config_lacks_banned_terms(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        # Full env minus the two override vars so neither resolver short-circuits
        # on an env value; with no banned_terms configured the live resolve must
        # degrade to an empty scan-term list rather than raise.
        env = {k: v for k, v in os.environ.items() if k not in {"T3_BANNED_TERMS", "TEATREE_BANNED_BRANDS"}}
        with mock.patch.dict(os.environ, env, clear=True):
            export = export_db_to_toml()  # scan_terms=None -> live resolve
        doc = tomllib.loads(export.toml)
        assert doc["teatree"]["mode"] == "auto"
        # No terms resolved => nothing to redact; the export is valid and complete.
        assert export.redacted == ()
