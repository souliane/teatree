"""``t3 <overlay> config-setting`` admin path for the DB override tier (#1775).

The management command is the sanctioned way to set/clear a ``ConfigSetting``
row (the ORM-touching admin path). Integration-first via ``call_command``
against the real DB; the value is parsed as JSON so a bool kill-switch, a
string, an int, or a list all round-trip into the override store.
"""

import tomllib
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

import teatree.config as config_facade
from teatree.config import get_effective_settings
from teatree.config.enums import Mode
from teatree.core.models import ConfigSetting


class TestConfigSettingSet(TestCase):
    def test_set_bool_creates_row(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True

    def test_set_is_upsert(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        call_command("config_setting", "set", "issue_implementer_enabled", "false")
        assert ConfigSetting.objects.filter(key="issue_implementer_enabled").count() == 1
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False

    def test_set_string_value(self) -> None:
        call_command("config_setting", "set", "issue_implementer_label", '"ready"')
        assert ConfigSetting.objects.get_effective("issue_implementer_label") == "ready"

    def test_set_int_value(self) -> None:
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "3")
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent") == 3

    def test_set_rejects_non_overridable_key(self) -> None:
        # Out of scope of the pilot: only OVERLAY_OVERRIDABLE_SETTINGS keys are
        # accepted so the admin cannot stash a row the resolver would ignore.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "not_a_real_setting", "true")
        assert ConfigSetting.objects.filter(key="not_a_real_setting").exists() is False

    def test_set_rejects_deleted_agent_review_request_disabled_key(self) -> None:
        # #2579 item 1: the parallel side flag ``agent_review_request_disabled``
        # is deleted — review-request blocking is driven off the autonomy tier.
        # Setting the old key must now be refused (it left OVERLAY_OVERRIDABLE_SETTINGS).
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "agent_review_request_disabled", "true")
        assert ConfigSetting.objects.filter(key="agent_review_request_disabled").exists() is False

    def test_set_accepts_new_review_request_post_disabled_key(self) -> None:
        # The Option-A per-overlay escape replacing the deleted flag IS overridable.
        call_command("config_setting", "set", "review_request_post_disabled", "true")
        assert ConfigSetting.objects.get_effective("review_request_post_disabled") is True

    def test_set_rejects_invalid_json(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "issue_implementer_enabled", "not-json")
        assert ConfigSetting.objects.filter(key="issue_implementer_enabled").exists() is False

    def test_set_rejects_out_of_enum_value_and_leaves_reads_working(self) -> None:
        # #258 blocker 1: a value that JSON-parses but is invalid for the
        # setting's type (an out-of-enum ``mode``) must be rejected at WRITE
        # time. Storing it would brick every config read — ``get_effective``'s
        # DB tier coerces each stored value via the registry parser, so a bad
        # ``mode`` row makes ``Mode.parse`` raise on EVERY resolution.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "mode", '"bogus"')
        assert ConfigSetting.objects.filter(key="mode").exists() is False
        # The store is untouched, so config reads still resolve.
        assert get_effective_settings().mode is not None

    def test_set_rejects_quoted_bool_string(self) -> None:
        # #258 blocker 2: a JSON string ``"false"`` for a bool-typed setting
        # must be rejected, not truthy-coerced via ``bool("false") == True``.
        # Silently enabling an opt-in safety setting is the failure mode.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "allow_destructive_disk", '"false"')
        assert ConfigSetting.objects.filter(key="allow_destructive_disk").exists() is False

    def test_set_accepts_real_json_bool_false(self) -> None:
        # The GREEN side of blocker 2: a real JSON boolean ``false`` resolves
        # to Python ``False`` and the opt-in setting stays disabled.
        call_command("config_setting", "set", "allow_destructive_disk", "false")
        assert ConfigSetting.objects.get_effective("allow_destructive_disk") is False

    def test_set_rejects_bool_for_int_setting(self) -> None:
        # #258 fix round 2, blocker 1.1: JSON ``true`` decodes to Python ``True``,
        # and ``int(True) == 1`` (bool is a subclass of int), so the lenient
        # ``int`` registry parser silently ACCEPTED a bool for an int-typed
        # setting and the raw ``True`` was persisted. The strict int parser must
        # REJECT a bool at WRITE time, leaving the store untouched.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "issue_implementer_max_concurrent", "true")
        assert ConfigSetting.objects.filter(key="issue_implementer_max_concurrent").exists() is False

    def test_set_rejects_scalar_for_list_setting(self) -> None:
        # #258 fix round 2, blocker 1.2: ``_parse_excluded_skills`` returned ``[]``
        # for ANY non-list scalar, so ``set excluded_skills true`` passed
        # validation and stored the raw ``True``. The strict list parser must
        # RAISE on a non-list scalar so the bad value is rejected at write time.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "excluded_skills", "true")
        assert ConfigSetting.objects.filter(key="excluded_skills").exists() is False

    def test_set_int_persists_canonical_value(self) -> None:
        # No-regression GREEN guard + canonical-value invariant: a JSON numeric
        # STRING ``"5"`` parses to the int ``5``, and the CANONICAL parsed value
        # (the int, not the raw ``"5"`` string) is persisted — so the DB row and
        # the read-time coercion agree on the int.
        call_command("config_setting", "set", "issue_implementer_max_concurrent", '"5"')
        row = ConfigSetting.objects.get(key="issue_implementer_max_concurrent")
        assert row.value == 5
        assert isinstance(row.value, int)
        assert get_effective_settings().issue_implementer_max_concurrent == 5

    def test_set_list_persists_canonical_value(self) -> None:
        # No-regression GREEN guard for blocker 1.2: a real JSON list is accepted
        # and stored as the canonical parsed list, readable back unchanged.
        call_command("config_setting", "set", "excluded_skills", '["foo"]')
        row = ConfigSetting.objects.get(key="excluded_skills")
        assert row.value == ["foo"]
        assert get_effective_settings().excluded_skills == ["foo"]

    def test_set_enum_persists_normalised_canonical_value(self) -> None:
        # The canonical-persistence change normalises an enum value: an UPPER-case
        # ``"AUTO"`` parses to ``Mode.AUTO`` whose ``StrEnum`` value is the
        # lower-case ``"auto"``. The CANONICAL (normalised) value is stored — not
        # the raw ``"AUTO"`` — so the row and the read tier agree, and the read
        # tier re-parses it to the same enum.
        call_command("config_setting", "set", "mode", '"AUTO"')
        row = ConfigSetting.objects.get(key="mode")
        assert row.value == "auto"
        assert get_effective_settings().mode is Mode.AUTO


class TestConfigSettingClear(TestCase):
    def test_clear_removes_row(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        call_command("config_setting", "clear", "issue_implementer_enabled")
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is None

    def test_clear_absent_key_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "clear", "never_set")


class TestConfigSettingList(TestCase):
    def test_list_shows_rows(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        assert "issue_implementer_enabled" in out.getvalue()

    def test_list_empty_is_clean(self) -> None:
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        assert "no" in out.getvalue().lower()


class TestConfigSettingGet(TestCase):
    def test_get_reports_stored_db_value(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 7)
        out = StringIO()
        call_command("config_setting", "get", "issue_implementer_max_concurrent", stdout=out)
        rendered = out.getvalue()
        assert "7" in rendered
        # The source is named so the operator knows it came from the DB tier, not
        # the file/env fallback.
        assert "db" in rendered.lower()

    def test_get_reports_file_fallback_when_no_db_row(self) -> None:
        # No DB row -> get reports the resolved file/env value and names the
        # fallback source, so an absent override is visible (dual-read read-side).
        assert ConfigSetting.objects.filter(key="issue_implementer_max_concurrent").exists() is False
        out = StringIO()
        call_command("config_setting", "get", "issue_implementer_max_concurrent", stdout=out)
        rendered = out.getvalue().lower()
        assert "file" in rendered or "fallback" in rendered

    def test_get_rejects_non_overridable_key(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "get", "not_a_real_setting", stderr=StringIO())


class TestConfigSettingFlagTrailer(TestCase):
    """Set/get of a feature-flag key carries a governance trailer, a setting does not."""

    def test_set_of_a_flag_key_prints_the_flag_trailer(self) -> None:
        out = StringIO()
        call_command("config_setting", "set", "outer_loop_enabled", "true", stdout=out)
        rendered = out.getvalue()
        assert "feature flag" in rendered
        assert "stage=dark" in rendered
        assert "tracking" in rendered

    def test_set_of_a_durable_setting_has_no_flag_trailer(self) -> None:
        out = StringIO()
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "3", stdout=out)
        assert "feature flag" not in out.getvalue()

    def test_get_of_a_flag_key_prints_the_flag_trailer(self) -> None:
        out = StringIO()
        call_command("config_setting", "get", "outer_loop_enabled", stdout=out)
        assert "feature flag" in out.getvalue()


class TestConfigSettingFlagsAudit(TestCase):
    """``config_setting flags`` is the read-only dead-toggle audit report."""

    def test_flags_lists_every_registered_flag_with_its_stage(self) -> None:
        out = StringIO()
        call_command("config_setting", "flags", stdout=out)
        rendered = out.getvalue()
        # loop_runner_enabled was graduated out by PR-28 (durable kill-switch, not a
        # dying flag); the live registry is all-DARK, so its rows render stage=dark.
        for key in ("outer_loop_enabled", "teams_enabled"):
            assert key in rendered
        assert "loop_runner_enabled" not in rendered
        assert "stage=dark" in rendered

    def test_flags_is_read_only_creates_no_rows(self) -> None:
        call_command("config_setting", "flags", stdout=StringIO())
        assert ConfigSetting.objects.count() == 0


class TestConfigSettingImport(TestCase):
    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_import_seeds_operational_keys_from_toml(self) -> None:
        self.config_path.write_text(
            "[teatree]\nissue_implementer_enabled = true\nissue_implementer_max_concurrent = 4\n",
            encoding="utf-8",
        )
        call_command("config_setting", "import", stdout=StringIO())
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent") == 4

    def test_import_skips_bootstrap_and_unknown_keys(self) -> None:
        # A bootstrap-file-only key (private_repos) and an unknown key must NOT be
        # imported into the DB store — only operational overridable keys move.
        self.config_path.write_text(
            '[teatree]\nprivate_repos = ["acme/secret"]\nnot_a_real_setting = "x"\nmode = "auto"\n',
            encoding="utf-8",
        )
        call_command("config_setting", "import", stdout=StringIO())
        assert ConfigSetting.objects.filter(key="private_repos").exists() is False
        assert ConfigSetting.objects.filter(key="not_a_real_setting").exists() is False
        # The one operational key did move.
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_import_is_idempotent(self) -> None:
        self.config_path.write_text("[teatree]\nissue_implementer_max_concurrent = 4\n", encoding="utf-8")
        call_command("config_setting", "import", stdout=StringIO())
        call_command("config_setting", "import", stdout=StringIO())
        assert ConfigSetting.objects.filter(key="issue_implementer_max_concurrent").count() == 1

    def test_import_walks_per_overlay_tables_into_overlay_scope(self) -> None:
        # An operational DB-home key under [overlays.<name>] migrates into that
        # overlay's DB scope, not the global scope (#1775 in-PR add).
        self.config_path.write_text(
            '[teatree]\nmode = "interactive"\n\n[overlays.myproj]\npath = "~/p"\nmode = "auto"\n',
            encoding="utf-8",
        )
        call_command("config_setting", "import", stdout=StringIO())
        # The global row carries the [teatree] value; the overlay scope carries
        # the [overlays.myproj] override.
        assert ConfigSetting.objects.get_effective("mode") == "interactive"
        assert ConfigSetting.objects.get_effective("mode", scope="myproj") == "auto"

    def test_import_skips_non_setting_overlay_keys(self) -> None:
        # Overlay-table bootstrap keys (path/url/…) are not settings and must NOT
        # become ConfigSetting rows.
        self.config_path.write_text(
            '[teatree]\n\n[overlays.myproj]\npath = "~/p"\nurl = "git@x"\n',
            encoding="utf-8",
        )
        call_command("config_setting", "import", stdout=StringIO())
        assert ConfigSetting.objects.filter(scope="myproj").exists() is False

    def test_import_per_overlay_is_idempotent(self) -> None:
        self.config_path.write_text(
            '[teatree]\n\n[overlays.myproj]\npath = "~/p"\nmode = "auto"\n',
            encoding="utf-8",
        )
        call_command("config_setting", "import", stdout=StringIO())
        call_command("config_setting", "import", stdout=StringIO())
        assert ConfigSetting.objects.filter(key="mode", scope="myproj").count() == 1

    def test_import_no_clobber_preserves_a_db_set_value(self) -> None:
        # The ``t3 setup`` auto-migration mode: a value the user changed via
        # ``config_setting set`` must survive a later import of a stale TOML value.
        ConfigSetting.objects.set_value("mode", "auto")
        self.config_path.write_text('[teatree]\nmode = "interactive"\n', encoding="utf-8")
        call_command("config_setting", "import", "--no-clobber", stdout=StringIO())
        assert ConfigSetting.objects.get_effective("mode") == "auto"

    def test_import_default_clobbers_a_db_set_value(self) -> None:
        # Without --no-clobber the manual import refreshes from the file.
        ConfigSetting.objects.set_value("mode", "auto")
        self.config_path.write_text('[teatree]\nmode = "interactive"\n', encoding="utf-8")
        call_command("config_setting", "import", stdout=StringIO())
        assert ConfigSetting.objects.get_effective("mode") == "interactive"


class TestConfigSettingOverlayScope(TestCase):
    """``--overlay`` scoping on set / clear / get / list (per-overlay + global)."""

    def test_set_with_overlay_writes_overlay_scoped_row(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true", "--overlay", "ov")
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled", scope="ov") is True
        # The global scope is untouched by an overlay-scoped write.
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is None

    def test_set_global_and_overlay_coexist_via_cli(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "false")
        call_command("config_setting", "set", "issue_implementer_enabled", "true", "--overlay", "ov")
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled", scope="ov") is True

    def test_clear_with_overlay_is_scope_isolated(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "false")
        call_command("config_setting", "set", "issue_implementer_enabled", "true", "--overlay", "ov")
        call_command("config_setting", "clear", "issue_implementer_enabled", "--overlay", "ov")
        # The overlay row is gone; the global row survives.
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled", scope="ov") is None
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False

    def test_clear_overlay_absent_row_exits_nonzero(self) -> None:
        # A global row exists, but clearing the overlay scope (no row there) is loud.
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        with pytest.raises(SystemExit):
            call_command("config_setting", "clear", "issue_implementer_enabled", "--overlay", "ov")

    def test_get_with_overlay_reports_db_source(self) -> None:
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "7", "--overlay", "ov")
        out = StringIO()
        call_command("config_setting", "get", "issue_implementer_max_concurrent", "--overlay", "ov", stdout=out)
        rendered = out.getvalue().lower()
        assert "7" in rendered
        assert "db" in rendered
        assert "ov" in rendered

    def test_list_names_each_rows_scope(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        call_command("config_setting", "set", "issue_implementer_label", '"ready"', "--overlay", "ov")
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        rendered = out.getvalue()
        assert "global" in rendered
        assert "ov" in rendered


class TestConfigSettingExport(TestCase):
    """``config_setting export`` — the inverse of import (TOML round-trip, PR6)."""

    @pytest.fixture(autouse=True)
    def _config_path(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.config_path = tmp_path / ".teatree.toml"
        monkeypatch.setattr(config_facade, "CONFIG_PATH", self.config_path)
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_export_to_stdout_dumps_teatree_and_overlay_tables(self) -> None:
        call_command("config_setting", "set", "mode", '"auto"')
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "3")
        call_command("config_setting", "set", "mode", '"interactive"', "--overlay", "myproj")
        out = StringIO()
        call_command("config_setting", "export", stdout=out)
        doc = tomllib.loads(out.getvalue())
        assert doc["teatree"]["mode"] == "auto"
        assert doc["teatree"]["issue_implementer_max_concurrent"] == 3
        assert isinstance(doc["teatree"]["issue_implementer_max_concurrent"], int)
        assert doc["overlays"]["myproj"]["mode"] == "interactive"

    def test_export_output_writes_a_file(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        target = self.tmp_path / "dump.toml"
        call_command("config_setting", "export", "--output", str(target))
        doc = tomllib.loads(target.read_text(encoding="utf-8"))
        assert doc["teatree"]["issue_implementer_enabled"] is True

    def test_export_overlay_scopes_the_dump(self) -> None:
        call_command("config_setting", "set", "mode", '"auto"')  # global
        call_command("config_setting", "set", "mode", '"interactive"', "--overlay", "myproj")
        out = StringIO()
        call_command("config_setting", "export", "--overlay", "myproj", stdout=out)
        doc = tomllib.loads(out.getvalue())
        assert doc["overlays"]["myproj"]["mode"] == "interactive"
        # The global scope is excluded when a single overlay is requested.
        assert "teatree" not in doc

    def test_export_import_export_is_a_fixed_point(self) -> None:
        # Round-trip idempotence through the real import command: export -> clear ->
        # import the exported file -> export yields byte-identical TOML, for
        # operational + cold-hook keys across global and overlay scopes. The first
        # export lands on the monkeypatched CONFIG_PATH so the import re-reads it.
        ConfigSetting.objects.set_value("mode", "auto")
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 3)
        ConfigSetting.objects.set_value("excluded_skills", ["foo", "bar"])
        ConfigSetting.objects.set_value("orchestrator_turn_budget", 40)  # cold-hook, global-only
        ConfigSetting.objects.set_value("self_dm_gate_enabled", value=False)  # cold-hook
        ConfigSetting.objects.set_value("mode", "interactive", scope="myproj")
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True, scope="myproj")

        call_command("config_setting", "export", "--output", str(self.config_path))
        first_text = self.config_path.read_text(encoding="utf-8")
        # Anti-vacuity: an empty dump would round-trip trivially.
        assert "[teatree]" in first_text
        assert "[overlays.myproj]" in first_text

        ConfigSetting.objects.all().delete()
        call_command("config_setting", "import", stdout=StringIO())

        second = self.tmp_path / "export2.toml"
        call_command("config_setting", "export", "--output", str(second))
        assert second.read_text(encoding="utf-8") == first_text
