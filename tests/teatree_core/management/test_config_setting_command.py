"""``t3 <overlay> config-setting`` admin path for the DB override tier (#1775).

The management command is the sanctioned way to set/clear a ``ConfigSetting``
row (the ORM-touching admin path). Integration-first via ``call_command``
against the real DB; the value is parsed as JSON so a bool kill-switch, a
string, an int, or a list all round-trip into the override store.
"""

import re
import tomllib
from io import StringIO
from pathlib import Path
from typing import ClassVar

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.config.cold_defaults import flatten_settings_table
from teatree.config.enums import Mode
from teatree.config.retired_settings import CLEAR_REMEDY, RENAMED_SETTING_KEYS, removed_setting
from teatree.config.setting_groups import UNGROUPED_PATH
from teatree.core.models import ConfigSetting


def _teatree(document: dict[str, object]) -> dict[str, object]:
    """A dump's ``[teatree]`` table, flattened back to the flat key namespace."""
    return flatten_settings_table(document.get("teatree", {}))


class TestConfigSettingSet(TestCase):
    def test_set_bool_creates_row(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is True

    def test_set_is_upsert(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        call_command("config_setting", "set", "issue_implementer_enabled", "false")
        assert ConfigSetting.objects.filter(key="issue_implementer_enabled").count() == 1
        assert ConfigSetting.objects.get_effective("issue_implementer_enabled") is False

    def test_set_banned_terms_required(self) -> None:
        # The exact command the banned-terms scanner's unset warning tells the operator to run.
        # It was refused as an unknown key, so the only available behaviour was the fail-open (#4008).
        call_command("config_setting", "set", "banned_terms_required", "true")
        assert ConfigSetting.objects.get_effective("banned_terms_required") is True

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

    def test_set_rejects_inconsistent_harness_provider_pair(self) -> None:
        # #3688: an agent_harness_provider valid only under pydantic_ai, written
        # while agent_harness sits at its claude_sdk default, is refused at WRITE
        # time (exit 2) with the store left untouched — one loud error instead of
        # a fleet-wide repair-halt flood on every later dispatch.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "agent_harness_provider", '"openai_compatible"')
        assert ConfigSetting.objects.filter(key="agent_harness_provider").exists() is False

    def test_set_accepts_consistent_harness_provider_pair(self) -> None:
        call_command("config_setting", "set", "agent_harness", '"pydantic_ai"')
        call_command("config_setting", "set", "agent_harness_provider", '"openai_compatible"')
        assert ConfigSetting.objects.get_effective("agent_harness_provider") == "openai_compatible"

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

    def test_list_groups_rows_under_the_same_nested_hierarchy(self) -> None:
        ConfigSetting.objects.set_value("require_merge_evidence", value=True)
        ConfigSetting.objects.set_value("autoload", value=True)
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        rendered = out.getvalue()
        assert "Gates" in rendered
        assert "Quality" in rendered
        assert "Merge & done" in rendered
        assert rendered.index("Merge & done") < rendered.index("require_merge_evidence")
        # The level's indent is what makes the hierarchy readable in a flat terminal.
        assert re.search(r"^\s+Gates$", rendered, re.MULTILINE)
        assert re.search(r"^(\s+)Quality$", rendered, re.MULTILINE)
        gates_indent = re.search(r"^(\s+)Gates$", rendered, re.MULTILINE).group(1)
        quality_indent = re.search(r"^(\s+)Quality$", rendered, re.MULTILINE).group(1)
        assert len(quality_indent) > len(gates_indent), "a child level is not indented under its parent"

    def test_list_shows_a_row_no_declaration_owns_rather_than_hiding_it(self) -> None:
        ConfigSetting.objects.create(key="a_key_no_declaration_base_carries", value=True, scope="")
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        rendered = out.getvalue()
        assert "a_key_no_declaration_base_carries" in rendered
        assert UNGROUPED_PATH[0] in rendered


class TestConfigSettingListMarksDeadRows(TestCase):
    """A stored row no live declaration owns says so (souliane/teatree#3862).

    The Ungrouped banner reads as "uncategorised setting", not "dead key", so a row
    the resolver silently drops rendered here as a live control — a stored
    ``issue_implementer_require_label = True`` was read as a live intake gate while
    ``decide_intake`` admits a trusted author with no label at all.
    """

    def _rendered(self) -> str:
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        return out.getvalue()

    def _row_line(self, key: str) -> str:
        return next(line for line in self._rendered().splitlines() if line.strip().startswith(f"{key} ="))

    def test_a_retired_row_is_marked_dead_with_its_remedy(self) -> None:
        ConfigSetting.objects.create(key="issue_implementer_require_label", value=True, scope="")
        line = self._row_line("issue_implementer_require_label")
        assert "retired" in line
        assert "config_setting clear" in line

    def test_an_unrecorded_stale_row_is_marked_too(self) -> None:
        ConfigSetting.objects.create(key="a_key_no_declaration_base_carries", value=True, scope="")
        assert "not a declared setting" in self._row_line("a_key_no_declaration_base_carries")

    def test_an_internal_state_row_is_named_state_not_offered_the_clear_remedy(self) -> None:
        # The stamp row is live state the transition chain rewrites every pass; telling
        # the operator to clear it would make the next pass read a switch that never
        # happened. Not-a-known-key is not the same question as not-in-use.
        ConfigSetting.objects.create(key="loop_preset_transition_stamp", value="maintenance", scope="")
        line = self._row_line("loop_preset_transition_stamp")
        assert "internal state" in line
        assert "config_setting clear" not in line

    def test_a_live_row_carries_no_marker(self) -> None:
        # Positive control: the marker must distinguish, not decorate every row.
        ConfigSetting.objects.set_value("issue_implementer_enabled", value=True)
        line = self._row_line("issue_implementer_enabled")
        assert "retired" not in line
        assert "not a declared setting" not in line


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

    def test_get_reports_env_default_source_when_no_db_row(self) -> None:
        # No DB row -> get reports the resolved code-default value and names the
        # env/default source (DB-home: no file fallback), so an absent override
        # is visible.
        assert ConfigSetting.objects.filter(key="issue_implementer_max_concurrent").exists() is False
        out = StringIO()
        call_command("config_setting", "get", "issue_implementer_max_concurrent", stdout=out)
        rendered = out.getvalue().lower()
        assert "env/default" in rendered

    def test_get_rejects_non_overridable_key(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "get", "not_a_real_setting", stderr=StringIO())


class TestRetiredKeyRefusalRendersTheRecord(TestCase):
    """A retired key is answered with its retirement record (souliane/teatree#4094).

    The bare unknown-key refusal is indistinguishable from a typo, so a reader who
    knows the setting used to exist concludes the mechanism is lost rather than
    superseded — twice, in one session, from ``issue_implementer_require_label``.
    """

    def _refusal(self, subcommand: str, key: str) -> str:
        err = StringIO()
        args = ["config_setting", subcommand, key] + ([] if subcommand == "get" else ["true"])
        with pytest.raises(SystemExit):
            call_command(*args, stderr=err, stdout=StringIO())
        return err.getvalue()

    def test_get_of_a_removed_key_renders_its_reason_and_remedy(self) -> None:
        entry = removed_setting("issue_implementer_require_label")
        rendered = self._refusal("get", "issue_implementer_require_label")
        assert entry.reason in rendered
        assert CLEAR_REMEDY.format(key="issue_implementer_require_label") in rendered
        assert "is not a known config setting" not in rendered

    def test_get_of_a_renamed_key_names_its_replacement(self) -> None:
        rendered = self._refusal("get", "speed")
        assert RENAMED_SETTING_KEYS["speed"] in rendered
        assert "is not a known config setting" not in rendered

    def test_get_of_a_genuinely_unknown_key_is_unchanged(self) -> None:
        assert "is not a known config setting" in self._refusal("get", "not_a_real_setting")

    def test_set_of_a_removed_key_renders_the_record_and_still_refuses(self) -> None:
        entry = removed_setting("issue_implementer_require_label")
        assert entry.reason in self._refusal("set", "issue_implementer_require_label")
        assert ConfigSetting.objects.filter(key="issue_implementer_require_label").exists() is False

    def test_set_of_a_renamed_key_points_at_the_replacement(self) -> None:
        assert RENAMED_SETTING_KEYS["speed"] in self._refusal("set", "speed")
        assert ConfigSetting.objects.filter(key="speed").exists() is False

    def test_seed_of_a_removed_key_renders_the_record(self) -> None:
        entry = removed_setting("issue_implementer_require_label")
        assert entry.reason in self._refusal("seed", "issue_implementer_require_label")


class TestConfigSettingColdHookGateKey(TestCase):
    """A cold-hook gate key round-trips through get/list/set/clear.

    ``COLD_HOOK_SETTINGS`` keys (e.g. ``out_of_band_merge_gate_enabled``) that
    ``list`` shows are also settable/gettable/clearable — the unified known-key set.
    """

    def test_get_of_a_gate_key_reports_db_value(self) -> None:
        ConfigSetting.objects.set_value("out_of_band_merge_gate_enabled", value=False)
        out = StringIO()
        call_command("config_setting", "get", "out_of_band_merge_gate_enabled", stdout=out)
        rendered = out.getvalue().lower()
        assert "false" in rendered
        assert "db" in rendered

    def test_get_of_a_gate_key_reports_code_default_when_no_row(self) -> None:
        # No DB row: the resolved value is the in-code ColdHookSetting default
        # (out_of_band_merge_gate_enabled defaults to True), reported as a
        # code/default source — not a refusal.
        assert ConfigSetting.objects.filter(key="out_of_band_merge_gate_enabled").exists() is False
        out = StringIO()
        call_command("config_setting", "get", "out_of_band_merge_gate_enabled", stdout=out)
        rendered = out.getvalue().lower()
        assert "true" in rendered
        assert "default" in rendered

    def test_set_of_a_gate_key_is_accepted_and_round_trips(self) -> None:
        call_command("config_setting", "set", "out_of_band_merge_gate_enabled", "false")
        assert ConfigSetting.objects.get_effective("out_of_band_merge_gate_enabled") is False

    def test_set_of_a_gate_key_rejects_a_quoted_bool_string(self) -> None:
        # The cold-hook parser is strict (mirrors the cold reader): a quoted
        # "false" is not a bool and must be refused at write time.
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "out_of_band_merge_gate_enabled", '"false"')
        assert ConfigSetting.objects.filter(key="out_of_band_merge_gate_enabled").exists() is False

    def test_clear_of_a_gate_key_removes_the_row(self) -> None:
        ConfigSetting.objects.set_value("out_of_band_merge_gate_enabled", value=False)
        call_command("config_setting", "clear", "out_of_band_merge_gate_enabled")
        assert ConfigSetting.objects.get_effective("out_of_band_merge_gate_enabled") is None


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
        for key in ("outer_loop_enabled", "factory_score_enabled"):
            assert key in rendered
        assert "loop_runner_enabled" not in rendered
        assert "stage=dark" in rendered

    def test_flags_is_read_only_creates_no_rows(self) -> None:
        call_command("config_setting", "flags", stdout=StringIO())
        assert ConfigSetting.objects.count() == 0


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
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_export_to_stdout_dumps_teatree_and_overlay_tables(self) -> None:
        call_command("config_setting", "set", "mode", '"auto"')
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "3")
        call_command("config_setting", "set", "mode", '"interactive"', "--overlay", "myproj")
        out = StringIO()
        call_command("config_setting", "export", stdout=out)
        doc = tomllib.loads(out.getvalue())
        assert _teatree(doc)["mode"] == "auto"
        assert _teatree(doc)["issue_implementer_max_concurrent"] == 3
        assert isinstance(_teatree(doc)["issue_implementer_max_concurrent"], int)
        assert doc["overlays"]["myproj"]["mode"] == "interactive"

    def test_export_output_writes_a_file(self) -> None:
        call_command("config_setting", "set", "issue_implementer_enabled", "true")
        target = self.tmp_path / "dump.toml"
        call_command("config_setting", "export", "--output", str(target))
        doc = tomllib.loads(target.read_text(encoding="utf-8"))
        assert _teatree(doc)["issue_implementer_enabled"] is True

    def test_export_overlay_scopes_the_dump(self) -> None:
        call_command("config_setting", "set", "mode", '"auto"')  # global
        call_command("config_setting", "set", "mode", '"interactive"', "--overlay", "myproj")
        out = StringIO()
        call_command("config_setting", "export", "--overlay", "myproj", stdout=out)
        doc = tomllib.loads(out.getvalue())
        assert doc["overlays"]["myproj"]["mode"] == "interactive"
        # The global scope is excluded when a single overlay is requested.
        assert "teatree" not in doc


class TestConfigSettingExportFilters(TestCase):
    """The two export filters over the CLI — both off by default, both together = the file shape."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def _export(self, *flags: str) -> str:
        out = StringIO()
        call_command("config_setting", "export", *flags, stdout=out)
        return out.getvalue()

    def test_no_flag_dumps_only_the_overridden_rows(self) -> None:
        call_command("config_setting", "set", "mode", '"auto"')
        assert set(_teatree(tomllib.loads(self._export()))) == {"mode"}

    def test_default_keys_only_drops_the_overlay_scopes(self) -> None:
        call_command("config_setting", "set", "mode", '"auto"')
        call_command("config_setting", "set", "mode", '"interactive"', "--overlay", "myproj")
        assert "overlays" not in tomllib.loads(self._export("--default-keys-only"))

    def test_include_defaults_emits_the_unoverridden_keys_too(self) -> None:
        emitted = _teatree(tomllib.loads(self._export("--include-defaults")))
        assert "merge_wip" in emitted

    def test_both_flags_produce_the_shipped_file_shape(self) -> None:
        dump = self._export("--default-keys-only", "--include-defaults")
        assert dump.startswith("# teatree shipped defaults")
        assert set(tomllib.loads(dump)) == {"teatree", "loops", "modes", "schedules"}


class TestConfigSettingImport(TestCase):
    """``config_setting import`` — the inverse of ``export`` over the CLI (TOML round-trip)."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def _write_toml(self, text: str) -> Path:
        path = self.tmp_path / "dump.toml"
        path.write_text(text, encoding="utf-8")
        return path

    def test_import_from_file_writes_rows(self) -> None:
        # `interactive` is the value that DIVERGES from the shipped `auto` (#3895); a
        # value equal to the shipped default writes no row by design.
        path = self._write_toml('[teatree]\nmode = "interactive"\nissue_implementer_max_concurrent = 9\n')
        call_command("config_setting", "import", "--input", str(path), stdout=StringIO())
        assert ConfigSetting.objects.get_effective("mode") == "interactive"
        assert ConfigSetting.objects.get_effective("issue_implementer_max_concurrent") == 9

    def test_import_dry_run_writes_nothing(self) -> None:
        path = self._write_toml('[teatree]\nmode = "auto"\n')
        out = StringIO()
        call_command("config_setting", "import", "--input", str(path), "--dry-run", stdout=out)
        assert ConfigSetting.objects.count() == 0
        assert "would import" in out.getvalue()

    def test_import_rejects_unknown_key_and_writes_nothing(self) -> None:
        path = self._write_toml('[teatree]\nnot_a_setting = 1\nmode = "auto"\n')
        err = StringIO()
        with pytest.raises(SystemExit):
            call_command("config_setting", "import", "--input", str(path), stdout=StringIO(), stderr=err)
        assert "rejected not_a_setting" in err.getvalue()
        assert ConfigSetting.objects.count() == 0

    def test_import_reports_a_folded_alias(self) -> None:
        path = self._write_toml('[teatree]\nspeed = "slow"\n')
        out = StringIO()
        call_command("config_setting", "import", "--input", str(path), stdout=out)
        assert "folded retired alias speed -> wip" in out.getvalue()
        assert ConfigSetting.objects.get_effective("wip") == "slow"

    def test_import_rejects_invalid_toml_and_writes_nothing(self) -> None:
        path = self._write_toml("[teatree\nmode = broken")  # malformed TOML
        err = StringIO()
        with pytest.raises(SystemExit):
            call_command("config_setting", "import", "--input", str(path), stdout=StringIO(), stderr=err)
        assert "invalid TOML" in err.getvalue()
        assert ConfigSetting.objects.count() == 0


class TestPrivateBackupRoundTripOverTheCli(TestCase):
    """`export --include-private` then `import --restore-private` — the operator's path (#4156).

    The flag exists to make a COMPLETE backup, and the rows it adds are the ones an ordinary
    import refuses. So the export SAYS what it wrote and how to restore it, the refusal names
    the flag rather than dribbling out per-key secret rejections, and the flag restores.
    """

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    #: One synthetic row per KEY-classified withhold class — a one-class fixture passes against
    #: a one-class fix. The fourth class (a banned term matched on the VALUE) is unit-covered in
    #: ``test_config_migration`` instead: it needs live scan terms, and this command resolves
    #: those through a cold reader that cannot see a test transaction's rows.
    PRIVATE_ROWS: ClassVar[dict[str, str | list[str]]] = {
        "banned_terms": ["synthetic-scanned-term"],  # private-key
        "banned_brands": ["synthetic-brand"],  # private-key
        "anthropic_oauth_pass_paths": ["synthetic/oauth-entry"],  # credential-coordinate
        "slack_user_id": "synthetic-user-ref",  # personal-identifier
    }

    def setUp(self) -> None:
        for key, value in self.PRIVATE_ROWS.items():
            ConfigSetting.objects.set_value(key, value)
        ConfigSetting.objects.set_value("merge_wip", 4)

    @staticmethod
    def _private_value_fragments() -> list[str]:
        """Every private VALUE as it would read on screen — what must never appear in stdout."""
        fragments: list[str] = []
        for value in TestPrivateBackupRoundTripOverTheCli.PRIVATE_ROWS.values():
            fragments.extend(value if isinstance(value, list) else [value])
        return fragments

    def _backup(self) -> tuple[Path, str]:
        path = self.tmp_path / "backup.toml"
        err = StringIO()
        call_command("config_setting", "export", "--include-private", "--output", str(path), stderr=err)
        return path, err.getvalue()

    def _import(self, path: Path, *flags: str) -> tuple[str, str]:
        out, err = StringIO(), StringIO()
        call_command("config_setting", "import", "--input", str(path), *flags, stdout=out, stderr=err)
        return out.getvalue(), err.getvalue()

    def test_the_export_says_what_it_wrote_and_how_to_restore_it(self) -> None:
        _, err = self._backup()
        assert "PERSONAL BACKUP" in err
        assert "--restore-private" in err

    def test_a_plain_export_says_none_of_that(self) -> None:
        # Anti-vacuous control: the notice is tied to the flag, not printed on every export.
        err = StringIO()
        call_command("config_setting", "export", "--output", str(self.tmp_path / "plain.toml"), stderr=err)
        assert "PERSONAL BACKUP" not in err.getvalue()

    def test_an_ordinary_import_refuses_it_and_names_the_flag(self) -> None:
        path, _ = self._backup()
        err = StringIO()
        with pytest.raises(SystemExit):
            call_command("config_setting", "import", "--input", str(path), stdout=StringIO(), stderr=err)
        assert "pass --restore-private" in err.getvalue()

    def test_the_flag_restores_the_private_rows(self) -> None:
        path, _ = self._backup()
        ConfigSetting.objects.all().delete()
        self._import(path, "--restore-private")
        for key, value in self.PRIVATE_ROWS.items():
            assert ConfigSetting.objects.get_effective(key) == value
        assert ConfigSetting.objects.get_effective("merge_wip") == 4

    def test_the_restore_never_echoes_a_private_value_to_stdout(self) -> None:
        # A CLI an agent runs writes its stdout into transcripts, so the restore names the
        # KEYS it wrote and withholds their values — the same convention the export follows.
        path, _ = self._backup()
        ConfigSetting.objects.all().delete()
        out, _ = self._import(path, "--restore-private")
        for fragment in self._private_value_fragments():
            assert fragment not in out, f"private value {fragment!r} leaked to stdout"
        for key in self.PRIVATE_ROWS:
            assert f"{key} = <withheld: private>" in out, f"the operator was not told {key} was restored"

    def test_the_dry_run_preview_never_echoes_a_private_value_either(self) -> None:
        path, _ = self._backup()
        ConfigSetting.objects.all().delete()
        out, _ = self._import(path, "--restore-private", "--dry-run")
        for fragment in self._private_value_fragments():
            assert fragment not in out, f"private value {fragment!r} leaked to the preview"
        for key in self.PRIVATE_ROWS:
            assert f"{key} = <withheld: private>" in out, f"the preview did not name {key}"

    def test_a_non_private_row_still_renders_its_value(self) -> None:
        # Anti-vacuous control: blanking EVERY value would pass the leak tests trivially.
        path, _ = self._backup()
        ConfigSetting.objects.all().delete()
        out, _ = self._import(path, "--restore-private")
        assert re.search(r"merge_wip = 4\b", out), out

    def test_the_flag_is_reported_ignored_on_a_file_that_is_not_a_backup(self) -> None:
        path = self.tmp_path / "shared.toml"
        path.write_text("[teatree]\nmerge_wip = 4\n", encoding="utf-8")
        _, err = self._import(path, "--restore-private")
        assert "--restore-private ignored" in err


class TestConfigSettingSeed(TestCase):
    """`config_setting seed` — the provenance-aware DEPLOY seed (#3435).

    Distinct from `set`: it skips a value equal to the code default, preserves an
    operator override, and stamps provenance the doctor autofix reads.
    """

    def _seed(self, key: str, value: str) -> str:
        out = StringIO()
        call_command("config_setting", "seed", key, value, stdout=out)
        return out.getvalue()

    def test_seed_below_default_creates_row(self) -> None:
        # provision_ram_ceiling_percent code default is 85; 75 differs, so it seeds.
        text = self._seed("provision_ram_ceiling_percent", "75")
        assert ConfigSetting.objects.get_effective("provision_ram_ceiling_percent") == 75
        assert "created" in text
        row = ConfigSetting.objects.get(key="provision_ram_ceiling_percent")
        assert row.seeded_by == "entrypoint"
        assert row.seed_value == 75

    def test_seed_equal_to_code_default_writes_nothing(self) -> None:
        # provision_max_concurrency code default is 0; seeding 0 is a documented no-op.
        text = self._seed("provision_max_concurrency", "0")
        assert ConfigSetting.objects.filter(key="provision_max_concurrency").exists() is False
        assert "skipped-equals-default" in text

    def test_seed_preserves_operator_override(self) -> None:
        call_command("config_setting", "set", "provision_ram_ceiling_percent", "90")
        self._seed("provision_ram_ceiling_percent", "75")
        assert ConfigSetting.objects.get_effective("provision_ram_ceiling_percent") == 90

    def test_seed_refuses_unknown_key(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "seed", "not_a_setting", "1")

    def test_seed_refuses_invalid_json(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "seed", "provision_ram_ceiling_percent", "not-json")
