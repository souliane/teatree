"""``ConfigSetting`` store -> TOML export service + its leak/secret guards.

The DB-home store is the single source of truth; ``export_db_to_toml`` serialises
it to TOML for a personal, never-shared backup. Integration-first against the real
DB: global rows render under ``[teatree]``, each overlay scope under
``[overlays.<name>]``, and the export guard withholds secret/tainted rows so a
shared export never leaks a codename.
"""

import json
import os
import sqlite3
import tomllib
from pathlib import Path
from unittest import mock

import pytest
from django.test import TestCase

from teatree.config import COLD_HOOK_SETTINGS, OVERLAY_OVERRIDABLE_SETTINGS, effective_default, get_effective_settings
from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.schema import _DEFAULTS_TOML, setting_choices, shipped_defaults
from teatree.config.seed_defaults import shipped_seed_table
from teatree.config.setting_annotation import choice_token
from teatree.core.config_interchange.migration import export_db_to_toml, import_toml_to_db
from teatree.core.config_interchange.secret_guard import resolve_export_scan_terms
from teatree.core.models import ConfigSetting, Loop, Mode, ModeSchedule
from teatree.loops.preset_seed import seed_default_presets_and_schedules


def _teatree(document: dict[str, object]) -> dict[str, object]:
    """A dump's ``[teatree]`` table, flattened back to the flat key namespace.

    The dump nests the declaration hierarchy as sub-tables; the persisted contract is the
    flat namespace, so every assertion about WHICH keys the dump carries is made there.
    """
    return flatten_settings_table(document.get("teatree", {}))


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
        assert _teatree(doc)["mode"] == "auto"
        assert _teatree(doc)["issue_implementer_max_concurrent"] == 3

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
        teatree = _teatree(tomllib.loads(export_db_to_toml(scan_terms=()).toml))
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


class TestTheDumpSaysWhatEachKeyAccepts(TestCase):
    """A dumped line answers "what may I put here", not only "what is it now".

    The export is where defaults get reviewed away from the dashboard, so a line reading
    ``wip = "full"`` alone leaves the reader unable to tell an open string from one of four
    words. The answer is the schema's own (``setting_choices``, which the dashboard's
    selects are built from) — these pin it reaching the dump intact and still importable.
    """

    def _line(self, key: str) -> str:
        return next(line for line in export_db_to_toml(scan_terms=()).toml.splitlines() if line.startswith(f"{key} ="))

    def test_a_constrained_keys_line_names_every_value_the_schema_admits(self) -> None:
        ConfigSetting.objects.set_value("wip", "full")
        offered = self._line("wip").split("one of:", 1)[1]
        for choice in setting_choices("wip"):
            assert choice_token(choice) in offered, f"{choice!r} missing from {offered!r}"

    def test_an_open_typed_keys_line_names_its_type_and_offers_no_list(self) -> None:
        ConfigSetting.objects.set_value("agent_max_turns", 400)
        line = self._line("agent_max_turns")
        assert "# int" in line
        assert "one of:" not in line

    def test_the_annotated_dump_still_imports_back_into_the_store(self) -> None:
        # The annotation lives in a TOML comment, so it must be invisible to the inverse.
        # Both values diverge from their shipped default, so a row is genuinely rewritten
        # (import skips a value equal to the default, which would prove nothing here).
        ConfigSetting.objects.set_value("wip", "slow")
        ConfigSetting.objects.set_value("agent_max_turns", 400)
        dumped = export_db_to_toml(scan_terms=()).toml
        ConfigSetting.objects.all().delete()
        result = import_toml_to_db(dumped, scan_terms=())
        assert not result.rejected, result.rejected
        assert ConfigSetting.objects.get_effective("wip") == "slow"
        assert ConfigSetting.objects.get_effective("agent_max_turns") == 400


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
        assert _teatree(tomllib.loads(dump))["mode"] == "auto"


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
        assert _teatree(doc)["mode"] == "auto"
        assert "banned_brands" not in _teatree(doc)
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
        teatree = _teatree(tomllib.loads(result.toml))
        assert teatree["banned_brands"] == ["acmebrand"]
        assert teatree["ban_close_trailers_on_namespaces"] == ["acmecorp"]
        assert result.redacted == ()

    def test_clean_rows_are_untouched_by_the_scan(self) -> None:
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("excluded_skills", ["foo"])
        result = export_db_to_toml(scan_terms=("acmecorp",))
        teatree = _teatree(tomllib.loads(result.toml))
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
        assert _teatree(doc)["mode"] == "auto"
        # No terms resolved => nothing to redact; the export is valid and complete.
        assert export.redacted == ()


class TestExportScanTermsRoutesThroughRegistry:
    """``resolve_export_scan_terms`` resolves through the consolidated registry.

    Seeds the canonical config DB directly (via ``T3_CONFIG_DB``) so ``cold_reader``
    reads it, then asserts the export scan set is the union of every ban class,
    ``overlay`` included, and excludes the ``allow`` carve-out.
    """

    def _seed_registry(self, tmp_path: Path, registry: dict[str, list[str]]) -> Path:
        db = tmp_path / "registry.sqlite3"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS teatree_config_setting ("
            "id INTEGER PRIMARY KEY, scope TEXT NOT NULL DEFAULT '', key TEXT NOT NULL, value TEXT NOT NULL)"
        )
        conn.execute(
            "INSERT INTO teatree_config_setting (scope, key, value) VALUES ('', 'banned_term_registry', ?)",
            (json.dumps(registry),),
        )
        conn.commit()
        conn.close()
        return db

    def test_union_includes_overlay_and_excludes_allow(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        db = self._seed_registry(
            tmp_path,
            {"leak": ["democorp"], "prose_collider": ["widget-margin"], "overlay": ["acme-internal"], "allow": ["ok"]},
        )
        monkeypatch.setenv("T3_CONFIG_DB", str(db))
        monkeypatch.delenv("T3_BANNED_TERMS", raising=False)
        monkeypatch.delenv("TEATREE_TERM_REGISTRY", raising=False)
        assert set(resolve_export_scan_terms()) == {"democorp", "widget-margin", "acme-internal"}


class TestImportTomlToDb(TestCase):
    """``import_toml_to_db`` — the precise inverse of ``export_db_to_toml`` (PR: phase 4).

    Loads a ``config_setting export`` dump back into the store: retired aliases fold,
    unknown/secret rows are rejected wholesale, every value is validated through the
    resolver's own parser, and a value equal to the shipped default writes no row.
    """

    def test_round_trip_writes_values_into_the_store(self) -> None:
        result = import_toml_to_db(
            '[teatree]\nmode = "interactive"\nissue_implementer_max_concurrent = 9\n', scan_terms=()
        )
        assert result.rejected == ()
        assert ConfigSetting.objects.get_effective("mode") == "interactive"
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent") == 9

    def test_unknown_key_rejects_the_whole_import(self) -> None:
        result = import_toml_to_db('[teatree]\nnot_a_setting = 1\nmode = "auto"\n', scan_terms=())
        assert [(r.key, r.reason) for r in result.rejected] == [("not_a_setting", "unknown key")]
        # Atomic: the clean `mode` row is NOT written when a sibling row is rejected.
        assert result.written == ()
        assert ConfigSetting.objects.count() == 0

    def test_secret_key_is_rejected(self) -> None:
        result = import_toml_to_db('[teatree]\nbanned_terms = ["synthetic"]\n', scan_terms=())
        assert len(result.rejected) == 1
        assert result.rejected[0].key == "banned_terms"
        assert "private-key" in result.rejected[0].reason
        assert ConfigSetting.objects.count() == 0

    def test_value_carrying_a_banned_term_is_rejected(self) -> None:
        result = import_toml_to_db(
            '[overlays.proj]\nban_close_trailers_on_namespaces = ["acmecorp"]\n', scan_terms=("acmecorp",)
        )
        assert len(result.rejected) == 1
        assert result.rejected[0].reason == "secret (banned-term:acmecorp)"

    def test_removed_key_is_rejected_loudly(self) -> None:
        result = import_toml_to_db('[teatree]\nbranch_prefix = "x"\n', scan_terms=())
        assert len(result.rejected) == 1
        assert result.rejected[0].key == "branch_prefix"
        assert result.rejected[0].reason.startswith("removed")

    def test_a_safety_posture_key_is_rejected_unless_the_caller_allows_it(self) -> None:
        result = import_toml_to_db('[teatree]\nautonomy = "babysit"\n', scan_terms=())
        assert [(r.key, r.reason) for r in result.rejected] == [("autonomy", "safety-posture")]
        assert ConfigSetting.objects.count() == 0

    def test_an_allowed_safety_posture_key_writes_and_is_flagged(self) -> None:
        result = import_toml_to_db('[teatree]\nautonomy = "babysit"\n', scan_terms=(), allow_safety_posture=True)
        assert result.rejected == ()
        assert [(r.key, r.is_safety_posture) for r in result.written] == [("autonomy", True)]
        assert ConfigSetting.objects.get_effective("autonomy") == "babysit"

    def test_an_ordinary_key_is_not_flagged_safety_posture(self) -> None:
        result = import_toml_to_db('[teatree]\nmode = "interactive"\n', scan_terms=())
        assert [(r.key, r.is_safety_posture) for r in result.written] == [("mode", False)]

    def test_a_restored_private_row_is_flagged_and_withholds_its_value(self) -> None:
        text = '[backup]\ninclude_private = true\n[teatree]\nslack_user_id = "synthetic-user-ref"\nmerge_wip = 4\n'
        result = import_toml_to_db(text, scan_terms=(), restore_private=True)
        assert [(r.key, r.is_private) for r in result.written] == [("slack_user_id", True), ("merge_wip", False)]
        rendered = {r.key: r.toml_value for r in result.written}
        assert "synthetic-user-ref" not in rendered["slack_user_id"]
        assert rendered["merge_wip"] == "4"

    def test_a_row_private_only_by_its_value_is_flagged_too(self) -> None:
        # The fourth withhold class: no key rule catches it, so `is_private` must be asked of
        # `redaction_reason` and not of `_unstorable_reason` (which returns None under the flag).
        text = '[backup]\ninclude_private = true\n[teatree]\ndashboard_instance_label = "acmecorp"\n'
        result = import_toml_to_db(text, scan_terms=("acmecorp",), restore_private=True)
        assert [(r.key, r.is_private) for r in result.written] == [("dashboard_instance_label", True)]
        assert "acmecorp" not in result.written[0].toml_value

    def test_a_safety_posture_value_equal_to_its_default_is_skipped_not_rejected(self) -> None:
        # It writes no row, so there is nothing for the confirm gate to authorize.
        result = import_toml_to_db('[teatree]\nautonomy = "full"\n', scan_terms=())
        assert result.rejected == ()
        assert [r.key for r in result.skipped_default] == ["autonomy"]

    def test_a_safety_posture_value_the_store_already_holds_is_unchanged_not_rejected(self) -> None:
        # The `unchanged` disposition is evaluated BEFORE the safety-posture refusal, so a box
        # re-importing its own export is not refused over a posture it is already running. The
        # refusal guards a CHANGE of posture, and this row changes nothing there is to authorize.
        ConfigSetting.objects.set_value("autonomy", "babysit")
        result = import_toml_to_db('[teatree]\nautonomy = "babysit"\nmode = "interactive"\n', scan_terms=())
        assert result.rejected == ()
        assert [r.key for r in result.unchanged] == ["autonomy"]
        assert [r.key for r in result.written] == ["mode"]

    def test_a_safety_posture_change_still_refuses_the_whole_file(self) -> None:
        # The anti-vacuous half of the pair above: relaxing the UNCHANGED case must not relax
        # the case the gate exists for. One posture change still costs every clean row.
        ConfigSetting.objects.set_value("autonomy", "babysit")
        result = import_toml_to_db('[teatree]\nautonomy = "notify"\nmode = "interactive"\n', scan_terms=())
        assert [(r.key, r.reason) for r in result.rejected] == [("autonomy", "safety-posture")]
        assert result.written == ()
        assert ConfigSetting.objects.get_effective("mode") != "interactive"

    def test_retired_alias_folds_onto_its_replacement(self) -> None:
        # `speed` was renamed to `wip`; the stored value migrates onto the live key.
        result = import_toml_to_db('[teatree]\nspeed = "slow"\n', scan_terms=())
        assert result.rejected == ()
        assert ("speed", "wip") in result.folded
        assert [(r.scope, r.key) for r in result.written] == [("", "wip")]
        assert ConfigSetting.objects.get_effective("wip") == "slow"
        assert ConfigSetting.objects.get_effective("speed") is None

    def test_invalid_value_is_rejected(self) -> None:
        # A quoted "false" for a bool-typed setting fails the strict parser (#258).
        result = import_toml_to_db('[teatree]\nissue_implementer_enabled = "false"\n', scan_terms=())
        assert len(result.rejected) == 1
        assert result.rejected[0].reason.startswith("invalid")
        assert ConfigSetting.objects.count() == 0

    def test_value_equal_to_effective_default_writes_no_row(self) -> None:
        # issue_implementer_enabled's effective default is True (#3895), so a row
        # equal to it is redundant and skipped.
        result = import_toml_to_db("[teatree]\nissue_implementer_enabled = true\n", scan_terms=())
        assert result.rejected == ()
        assert result.written == ()
        assert [(r.scope, r.key) for r in result.skipped_default] == [("", "issue_implementer_enabled")]
        assert ConfigSetting.objects.count() == 0

    def test_import_of_the_shipped_default_value_writes_no_row(self) -> None:
        # The import-skip authority is the resolver's own default, and the resolver reads
        # defaults.toml — so a value equal to the shipped default is provably redundant:
        # skipping it and writing it resolve to the SAME value. (Before the TOML tier was
        # wired in, a shipped value diverging from the dataclass default had to be written
        # or it silently resolved to the dataclass value instead.)
        toml_default = shipped_defaults().provision_ram_ceiling_percent
        assert effective_default("provision_ram_ceiling_percent") == toml_default
        result = import_toml_to_db(f"[teatree]\nprovision_ram_ceiling_percent = {toml_default}\n", scan_terms=())
        assert result.rejected == ()
        assert result.written == ()
        assert [(r.scope, r.key) for r in result.skipped_default] == [("", "provision_ram_ceiling_percent")]
        assert get_effective_settings().provision_ram_ceiling_percent == toml_default

    def test_defaults_toml_imports_to_zero_rows(self) -> None:
        # The zero-row normalization invariant: every key in defaults.toml is by
        # definition equal to the shipped default, so a clean import of the file itself
        # writes NOTHING (preserving zero-seed + `restore = delete row`), and every
        # resolved value still equals what defaults.toml declares.
        result = import_toml_to_db(_DEFAULTS_TOML.read_text(encoding="utf-8"), scan_terms=())
        assert result.rejected == ()
        assert result.written == ()
        assert len(result.skipped_default) > 0
        assert ConfigSetting.objects.count() == 0
        resolved = get_effective_settings().provision_ram_ceiling_percent
        assert resolved == shipped_defaults().provision_ram_ceiling_percent

    def test_dry_run_classifies_without_writing(self) -> None:
        result = import_toml_to_db('[teatree]\nmode = "interactive"\n', scan_terms=(), dry_run=True)
        assert result.dry_run is True
        assert [(r.scope, r.key) for r in result.written] == [("", "mode")]
        assert ConfigSetting.objects.count() == 0

    def test_overlay_scoped_overridable_key_writes_a_scoped_row(self) -> None:
        # An `[overlays.<name>]` table with an OVERRIDABLE key imports as a scope-tagged row.
        result = import_toml_to_db('[overlays.proj]\nmode = "interactive"\n', scan_terms=())
        assert result.rejected == ()
        assert [(r.scope, r.key) for r in result.written] == [("proj", "mode")]
        assert ConfigSetting.objects.get_effective("mode", scope="proj") == "interactive"

    def test_a_non_dict_overlays_entry_is_skipped_not_fatal(self) -> None:
        # A malformed `overlays.<name>` scalar (not a sub-table) is defensively skipped.
        result = import_toml_to_db('[overlays]\nfoo = "bar"\n', scan_terms=())
        assert result.rejected == ()
        assert result.written == ()
        assert ConfigSetting.objects.count() == 0

    def test_overlay_scope_and_registry_definition_keys_split(self) -> None:
        # An `[overlays.<name>]` table splits: a per-overlay SETTING becomes a scope row,
        # a definition key (class/path) folds back into the `overlays` registry row.
        toml = '[overlays.myov]\nmode = "interactive"\nclass = "pkg.settings"\n'
        result = import_toml_to_db(toml, scan_terms=())
        assert result.rejected == ()
        assert ConfigSetting.objects.get_effective("mode", scope="myov") == "interactive"
        assert ConfigSetting.objects.get_effective("overlays") == {"myov": {"class": "pkg.settings"}}


class TestExportImportRoundTripIsByteStable(TestCase):
    """``export -> import -> export`` is a fixed point at the byte level (phase 4 golden).

    A shared export withholds Secret values but INCLUDES Personal ones; feeding it back
    through import rebuilds the identical store, so the second export is byte-for-byte the
    first. All values are non-default (a default value imports to no row) and span every
    section: global settings, a Personal key, per-overlay scope rows, the overlays
    definition registry, and the e2e-repos registry.
    """

    def _seed_representative_store(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=False)
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 9)
        ConfigSetting.objects.set_value("excluded_skills", ["zzz"])
        ConfigSetting.objects.set_value("workspace_dir", "/tmp/ws")  # Personal — included in a shared export
        ConfigSetting.objects.set_value("mode", "interactive", scope="myov")
        ConfigSetting.objects.set_value("boost_concurrency", 5, scope="myov")
        ConfigSetting.objects.set_value("overlays", {"myov": {"class": "pkg.settings"}})
        ConfigSetting.objects.set_value("e2e_repos", {"myrepo": {"branch": "dev", "url": "git@x:r.git"}})

    def test_export_import_export_is_byte_identical(self) -> None:
        self._seed_representative_store()
        export1 = export_db_to_toml(scan_terms=()).toml
        ConfigSetting.objects.all().delete()

        result = import_toml_to_db(export1, scan_terms=())
        assert result.rejected == ()

        export2 = export_db_to_toml(scan_terms=()).toml
        assert export2 == export1

    def test_secret_value_is_withheld_from_export_and_personal_is_kept(self) -> None:
        self._seed_representative_store()
        ConfigSetting.objects.set_value("banned_brands", ["synthetic"])  # Secret
        dump = export_db_to_toml(scan_terms=()).toml
        assert "banned_brands" not in dump  # Secret withheld
        assert "/tmp/ws" in dump  # Personal kept
        # And the withheld Secret never round-trips back in.
        assert import_toml_to_db(dump, scan_terms=()).rejected == ()


class _SeedRowsTestCase(TestCase):
    """Migrations seed the ``Loop`` rows; the modes/schedules come from the install seed."""

    def setUp(self) -> None:
        seed_default_presets_and_schedules()


class TestSeedTableExport(_SeedRowsTestCase):
    """The loop / mode / schedule rows export as DIVERGENCES from the shipped seed.

    A ``ConfigSetting`` row exists only where an operator moved a value off its default, so
    the seed families follow the same rule: a box running the shipped seed exports no seed
    table at all, and a returned object exports exactly the fields it was tuned away from.
    """

    def test_an_untouched_box_exports_no_seed_table(self) -> None:
        doc = tomllib.loads(export_db_to_toml(scan_terms=()).toml)
        assert not {"loops", "modes", "schedules"} & set(doc)

    def test_a_retuned_loop_exports_only_the_diverging_field(self) -> None:
        Loop.objects.filter(name="inbox").update(delay_seconds=90)
        doc = tomllib.loads(export_db_to_toml(scan_terms=()).toml)
        assert doc["loops"] == {"inbox": {"delay_seconds": 90}}

    def test_a_disabled_loop_exports_its_default_enabled_flag(self) -> None:
        Loop.objects.filter(name="inbox").update(enabled=False)
        doc = tomllib.loads(export_db_to_toml(scan_terms=()).toml)
        assert doc["loops"] == {"inbox": {"default_enabled": False}}

    def test_a_retuned_mode_and_schedule_export_their_diverging_fields(self) -> None:
        Mode.objects.filter(name="off").update(description="my own words")
        ModeSchedule.objects.filter(name="standard").update(timezone="UTC")
        doc = tomllib.loads(export_db_to_toml(scan_terms=()).toml)
        assert doc["modes"] == {"off": {"description": "my own words"}}
        assert doc["schedules"] == {"standard": {"timezone": "UTC"}}

    def test_an_overlay_scoped_export_carries_no_seed_table(self) -> None:
        # The seed objects are global; an `--overlay` dump is scoped to that overlay's rows.
        Loop.objects.filter(name="inbox").update(delay_seconds=90)
        ConfigSetting.objects.set_value("mode", "auto", scope="myproj")
        doc = tomllib.loads(export_db_to_toml(overlay="myproj", scan_terms=()).toml)
        assert "loops" not in doc


class TestSeedTableImport(_SeedRowsTestCase):
    """``[loops]`` / ``[modes]`` / ``[schedules]`` import back onto the rows they name.

    Understood PER TABLE, not ignored: an unknown entry, an unknown field or a wrong-typed
    value refuses the whole import, and a value equal to the shipped seed writes nothing.
    """

    def test_a_diverging_loop_field_writes_onto_the_row(self) -> None:
        result = import_toml_to_db("[loops.inbox]\ndelay_seconds = 90\n", scan_terms=())
        assert result.rejected == ()
        assert [(r.scope, r.key, r.value) for r in result.written] == [("loops.inbox", "delay_seconds", 90)]
        assert Loop.objects.get(name="inbox").delay_seconds == 90

    def test_a_mode_and_a_schedule_field_write_onto_their_rows(self) -> None:
        toml = '[modes.off]\ndescription = "my own words"\n\n[schedules.standard]\ntimezone = "UTC"\n'
        assert import_toml_to_db(toml, scan_terms=()).rejected == ()
        assert Mode.objects.get(name="off").description == "my own words"
        assert ModeSchedule.objects.get(name="standard").timezone == "UTC"

    def test_a_shipped_entry_with_no_row_yet_is_rejected_not_raised(self) -> None:
        # A box that never ran the install seed carries no Mode rows, so the write has
        # nothing to land on. Refuse the whole import and say so, rather than raise
        # DoesNotExist halfway through and leave a partly-written store behind.
        Mode.objects.all().delete()
        result = import_toml_to_db('[modes.off]\ndescription = "my own words"\n', scan_terms=())
        assert [(r.scope, r.reason) for r in result.rejected] == [
            ("modes.off", "no modes row yet — run `t3 setup` to seed it")
        ]
        assert result.written == ()

    def test_a_value_equal_to_the_shipped_seed_writes_nothing(self) -> None:
        result = import_toml_to_db("[loops.inbox]\ndelay_seconds = 60\n", scan_terms=())
        assert result.written == ()
        assert [(r.scope, r.key) for r in result.skipped_default] == [("loops.inbox", "delay_seconds")]

    def test_an_unknown_entry_rejects_the_whole_import(self) -> None:
        result = import_toml_to_db(
            "[loops.inbox]\ndelay_seconds = 90\n\n[loops.nope]\ndelay_seconds = 1\n", scan_terms=()
        )
        assert [(r.scope, r.reason) for r in result.rejected] == [("loops.nope", "unknown loops entry")]
        assert result.written == ()
        assert Loop.objects.get(name="inbox").delay_seconds == 60

    def test_an_unknown_field_is_rejected(self) -> None:
        result = import_toml_to_db("[loops.inbox]\nlast_run_at = 1\n", scan_terms=())
        assert [(r.key, r.reason) for r in result.rejected] == [("last_run_at", "unknown field")]

    def test_a_diverging_shipped_only_field_is_rejected(self) -> None:
        # `prompt_body` seeds a separate Prompt row and `slots` are child rows with their
        # own editor — neither has a row to write onto, so moving one means editing the file.
        result = import_toml_to_db('[loops.arch_review]\nprompt_body = "x"\n', scan_terms=())
        assert [(r.key, r.reason) for r in result.rejected] == [
            ("prompt_body", "shipped-only field — tune it in config/defaults.toml")
        ]

    def test_a_shipped_only_field_at_its_shipped_value_is_a_no_op(self) -> None:
        body = shipped_seed_table("loops")["arch_review"]["prompt_body"]
        result = import_toml_to_db(f"[loops.arch_review]\nprompt_body = {body!r}\n", scan_terms=())
        assert result.rejected == ()
        assert result.written == ()
        assert [(r.scope, r.key) for r in result.skipped_default] == [("loops.arch_review", "prompt_body")]

    def test_a_wrong_typed_value_is_rejected(self) -> None:
        result = import_toml_to_db("[loops.inbox]\ndelay_seconds = true\n", scan_terms=())
        assert [(r.key, r.reason) for r in result.rejected] == [("delay_seconds", "invalid: expected int")]

    def test_a_non_table_seed_entry_is_skipped_not_fatal(self) -> None:
        result = import_toml_to_db('[loops]\ninbox = "bogus"\n', scan_terms=())
        assert result.rejected == ()
        assert result.written == ()

    def test_dry_run_classifies_a_seed_row_without_writing(self) -> None:
        result = import_toml_to_db("[loops.inbox]\ndelay_seconds = 90\n", scan_terms=(), dry_run=True)
        assert [(r.scope, r.key) for r in result.written] == [("loops.inbox", "delay_seconds")]
        assert Loop.objects.get(name="inbox").delay_seconds == 60

    def test_the_shipped_file_imports_to_zero_writes_per_seed_table(self) -> None:
        # The zero-write invariant now holds per-table rather than by silent exclusion: the
        # importer UNDERSTANDS every seed entry in the file and finds each equal to what is
        # shipped. The control is the reject cases above — an entry the file does not carry
        # is refused, so "no writes" cannot be an artefact of the tables being ignored.
        result = import_toml_to_db(_DEFAULTS_TOML.read_text(encoding="utf-8"), scan_terms=())
        assert result.rejected == ()
        assert result.written == ()
        seeded = {row.scope.split(".", 1)[0] for row in result.skipped_default if "." in row.scope}
        assert seeded == {"loops", "modes", "schedules"}


class TestSeedTableRoundTripIsByteStable(_SeedRowsTestCase):
    def test_export_import_export_is_byte_identical_for_a_retuned_box(self) -> None:
        Loop.objects.filter(name="inbox").update(delay_seconds=90, colleague_facing=True)
        Mode.objects.filter(name="off").update(description="my own words")
        ModeSchedule.objects.filter(name="standard").update(timezone="UTC")
        export1 = export_db_to_toml(scan_terms=()).toml

        Loop.objects.filter(name="inbox").update(delay_seconds=60, colleague_facing=False)
        Mode.objects.filter(name="off").update(description="the shipped words")
        ModeSchedule.objects.filter(name="standard").update(timezone="Europe/Vienna")
        assert import_toml_to_db(export1, scan_terms=()).rejected == ()

        assert export_db_to_toml(scan_terms=()).toml == export1


class TestExportCarriesTheSettingsHierarchy(TestCase):
    """The TOML dump nests ``[teatree]`` by the same tree the dashboard renders.

    The hierarchy is real sub-tables; the KEY NAMESPACE stays flat, because that namespace
    is the persisted contract every reader, env override and cold sqlite3 read depends on.
    ``flatten_settings_table`` is what closes the two, on every read and on import.
    """

    def _dump(self) -> str:
        ConfigSetting.objects.set_value("require_merge_evidence", value=True)
        ConfigSetting.objects.set_value("architectural_review_cadence_hours", value=99)
        ConfigSetting.objects.set_value("autoload", value=True)
        return export_db_to_toml(include_private=True).toml

    def test_each_level_of_the_path_is_a_real_table_above_its_keys(self) -> None:
        dump = self._dump()
        assert '[teatree.Gates.Quality."Merge & done"]' in dump
        assert '[teatree.Gates.Quality."Architectural review"]' in dump
        assert dump.index('[teatree.Gates.Quality."Merge & done"]') < dump.index("require_merge_evidence")

    def test_a_shared_parent_level_is_a_path_prefix_not_a_repeated_section(self) -> None:
        dump = self._dump()
        assert "\n[teatree.Gates]\n" not in dump, "an intermediate level printed a header of its own"
        quality = [line for line in dump.splitlines() if line.startswith("[teatree.Gates.Quality")]
        assert quality == ['[teatree.Gates.Quality."Architectural review"]', '[teatree.Gates.Quality."Merge & done"]']

    def test_keys_are_ordered_by_the_hierarchy_not_alphabetically(self) -> None:
        dump = self._dump()
        # ``autoload`` sorts first alphabetically but its group renders before the gates,
        # so hierarchy order and alphabetical order are distinguishable here.
        assert dump.index("autoload") < dump.index("architectural_review_cadence_hours")

    def test_the_grouped_dump_still_re_imports_exactly(self) -> None:
        dump = self._dump()
        ConfigSetting.objects.all().delete()
        result = import_toml_to_db(dump, allow_safety_posture=True)
        assert not result.rejected, result.rejected
        assert ConfigSetting.objects.get_effective("require_merge_evidence", scope="") is True
        assert ConfigSetting.objects.get_effective("architectural_review_cadence_hours", scope="") == 99

    def test_the_dump_is_a_deterministic_function_of_the_store(self) -> None:
        assert self._dump() == export_db_to_toml(include_private=True).toml
