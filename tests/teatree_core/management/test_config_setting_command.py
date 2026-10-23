"""``t3 <overlay> config-setting`` admin path for the DB override tier (#1775).

The management command is the sanctioned way to set/clear a ``ConfigSetting``
row (the ORM-touching admin path). Integration-first via ``call_command``
against the real DB; the value is parsed as JSON so a bool kill-switch, a
string, an int, or a list all round-trip into the override store.
"""

import json
import re
import tomllib
from collections.abc import Generator
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from typing import ClassVar
from unittest import mock

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.config.cold_defaults import DEFAULTS_TOML, flatten_settings_table
from teatree.config.enums import Mode
from teatree.config.feature_flags import FEATURE_FLAGS, FeatureFlag, FlagStage
from teatree.config.resolution import get_effective_setting
from teatree.config.seed_defaults import shipped_seed_table
from teatree.config.setting_groups import UNGROUPED_PATH
from teatree.config.setting_taxonomy import taxonomy
from teatree.core.config_interchange.migration import export_db_to_toml
from teatree.core.config_interchange.secret_guard import is_private_backup
from teatree.core.models import ConfigSetting, Loop
from teatree.core.models import Mode as Preset
from teatree.hooks.banned_term_registry import load_registry
from teatree.hooks.term_match import line_matches
from tests._send_gate import TEST_TERM_REGISTRY, TEST_TERM_REGISTRY_JSON

LEAK = TEST_TERM_REGISTRY["leak"][0]
OV = TEST_TERM_REGISTRY["overlay"][0]
SCAN = tuple(term for terms in TEST_TERM_REGISTRY.values() for term in terms)
CARRIER = f"{LEAK} desk"
#: A runtime-built non-ASCII placeholder — no real word, never a configured term.
NON_ASCII_TERM = "qx" + "\u00fc" + "mbel-ledger"


def _carries_no_term(*streams: str, terms: tuple[str, ...] = SCAN) -> bool:
    return not any(line_matches(stream, terms) for stream in streams)


def _teatree(document: dict[str, object]) -> dict[str, object]:
    """A dump's ``[teatree]`` table, flattened back to the flat key namespace."""
    return flatten_settings_table(document.get("teatree", {}))


@contextmanager
def _fixture_flag() -> Generator[None]:
    flag = FeatureFlag(field="contribute", stage=FlagStage.DARK, tracking_issue="#4189", summary="fixture")
    with mock.patch.dict(FEATURE_FLAGS, {"contribute": flag}):
        taxonomy.cache_clear()
        try:
            yield
        finally:
            taxonomy.cache_clear()


class TestConfigSettingSet(TestCase):
    def test_set_bool_creates_row(self) -> None:
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true")
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board") is True

    def test_set_is_upsert(self) -> None:
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true")
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "false")
        assert ConfigSetting.objects.filter(key="admit_colleague_prs_to_board").count() == 1
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board") is False

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

    def test_set_rejects_invalid_json(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "set", "admit_colleague_prs_to_board", "not-json")
        assert ConfigSetting.objects.filter(key="admit_colleague_prs_to_board").exists() is False

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
            call_command("config_setting", "set", "autoload", '"false"')
        assert ConfigSetting.objects.filter(key="autoload").exists() is False

    def test_set_accepts_real_json_bool_false(self) -> None:
        # The GREEN side of blocker 2: a real JSON boolean ``false`` resolves
        # to Python ``False`` and the opt-in setting stays disabled.
        call_command("config_setting", "set", "autoload", "false")
        assert ConfigSetting.objects.get_effective("autoload") is False

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

    def test_set_header_map_round_trips_into_the_effective_settings(self) -> None:
        headers = '{"X-OrcaRouter-Include-Cost": "true", "X-OrcaRouter-Session-Id": "t3-{session}"}'
        call_command("config_setting", "set", "openai_compatible_extra_headers", headers)
        assert get_effective_settings().openai_compatible_extra_headers == {
            "X-OrcaRouter-Include-Cost": "true",
            "X-OrcaRouter-Session-Id": "t3-{session}",
        }

    def test_set_rejects_a_header_map_that_is_not_all_strings(self) -> None:
        for bad in ('{"X-OrcaRouter-Include-Cost": true}', '["X-OrcaRouter-Include-Cost"]', '"true"'):
            with self.subTest(value=bad), pytest.raises(SystemExit):
                call_command("config_setting", "set", "openai_compatible_extra_headers", bad)
        assert ConfigSetting.objects.filter(key="openai_compatible_extra_headers").exists() is False

    def test_set_refuses_an_overlay_scope_its_reader_never_reads(self) -> None:
        stderr = StringIO()
        with pytest.raises(SystemExit) as exc_info:
            call_command("config_setting", "set", "agent_tier_models", '{"cheap": "m"}', overlay="x", stderr=stderr)
        assert exc_info.value.code == 2
        assert "Set it globally instead" in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key="agent_tier_models", scope="x").exists()

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
        ConfigSetting.objects.set_value("admit_colleague_prs_to_board", value=True)
        call_command("config_setting", "clear", "admit_colleague_prs_to_board")
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board") is None

    def test_clear_absent_key_exits_nonzero(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "clear", "never_set")


class TestConfigSettingList(TestCase):
    def test_list_shows_rows(self) -> None:
        ConfigSetting.objects.set_value("admit_colleague_prs_to_board", value=True)
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        assert "admit_colleague_prs_to_board" in out.getvalue()

    def test_list_empty_is_clean(self) -> None:
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        assert "no" in out.getvalue().lower()

    def test_list_groups_rows_under_the_same_nested_hierarchy(self) -> None:
        ConfigSetting.objects.set_value("expected_required_contexts", value=["test (3.13)"])
        ConfigSetting.objects.set_value("autoload", value=True)
        out = StringIO()
        call_command("config_setting", "list", stdout=out)
        rendered = out.getvalue()
        assert "Gates" in rendered
        assert "Quality" in rendered
        assert "Merge & done" in rendered
        assert rendered.index("Merge & done") < rendered.index("expected_required_contexts")
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

    def test_a_former_setting_row_is_marked_unknown_with_its_remedy(self) -> None:
        ConfigSetting.objects.create(key="issue_implementer_require_label", value=True, scope="")
        line = self._row_line("issue_implementer_require_label")
        assert "unknown" in line
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
        ConfigSetting.objects.set_value("admit_colleague_prs_to_board", value=True)
        line = self._row_line("admit_colleague_prs_to_board")
        assert "unknown" not in line
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


class TestUnknownKeyRefusal(TestCase):
    """Former setting names follow the same refusal as any unknown name."""

    def _refusal(self, subcommand: str, key: str) -> str:
        err = StringIO()
        args = ["config_setting", subcommand, key] + ([] if subcommand == "get" else ["true"])
        with pytest.raises(SystemExit):
            call_command(*args, stderr=err, stdout=StringIO())
        return err.getvalue()

    def test_unknown_names_are_refused_identically(self) -> None:
        for key in ("issue_implementer_require_label", "not_a_real_setting"):
            for subcommand in ("get", "set", "seed"):
                with self.subTest(key=key, subcommand=subcommand):
                    assert self._refusal(subcommand, key) == self._refusal(subcommand, "not_a_real_setting").replace(
                        "not_a_real_setting", key
                    )
                    assert not ConfigSetting.objects.filter(key=key).exists()

    def test_retired_and_renamed_keys_name_their_remedy(self) -> None:
        assert "no readers" in self._refusal("set", "branch_prefix")
        assert "renamed to wip" in self._refusal("get", "speed")


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


class TestConfigSettingGovernanceTrailer(TestCase):
    """Set/get of a governed key names every class it holds; a plain setting names none."""

    def test_set_of_a_flag_key_prints_its_stage_and_tracking_issue(self) -> None:
        out = StringIO()
        with _fixture_flag():
            call_command("config_setting", "set", "contribute", "false", stdout=out)
        rendered = out.getvalue()
        assert "feature-flag" in rendered
        assert "stage=dark" in rendered
        assert "tracking" in rendered

    def test_set_of_a_durable_setting_has_no_trailer(self) -> None:
        out = StringIO()
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "3", stdout=out)
        assert "feature-flag" not in out.getvalue()

    def test_get_of_a_flag_key_prints_the_trailer(self) -> None:
        out = StringIO()
        with _fixture_flag():
            call_command("config_setting", "get", "contribute", stdout=out)
        assert "feature-flag" in out.getvalue()

    def test_set_of_a_gate_key_announces_the_gate_a_flag_registry_never_named(self) -> None:
        out = StringIO()
        call_command("config_setting", "set", "out_of_band_merge_gate_enabled", "false", stdout=out)
        assert "gate" in out.getvalue()


class TestConfigSettingFlagsAudit(TestCase):
    """``config_setting flags`` is the read-only dead-toggle audit report."""

    def test_flags_lists_every_registered_flag_with_its_stage(self) -> None:
        out = StringIO()
        with _fixture_flag():
            call_command("config_setting", "flags", stdout=out)
        rendered = out.getvalue()
        assert "contribute" in rendered
        assert "stage=dark" in rendered

    def test_flags_is_read_only_creates_no_rows(self) -> None:
        call_command("config_setting", "flags", stdout=StringIO())
        assert ConfigSetting.objects.count() == 0


class TestConfigSettingOverlayScope(TestCase):
    """``--overlay`` scoping on set / clear / get / list (per-overlay + global)."""

    def test_set_with_overlay_writes_overlay_scoped_row(self) -> None:
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true", "--overlay", "ov")
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board", scope="ov") is True
        # The global scope is untouched by an overlay-scoped write.
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board") is None

    def test_set_global_and_overlay_coexist_via_cli(self) -> None:
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "false")
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true", "--overlay", "ov")
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board") is False
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board", scope="ov") is True

    def test_clear_with_overlay_is_scope_isolated(self) -> None:
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "false")
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true", "--overlay", "ov")
        call_command("config_setting", "clear", "admit_colleague_prs_to_board", "--overlay", "ov")
        # The overlay row is gone; the global row survives.
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board", scope="ov") is None
        assert ConfigSetting.objects.get_effective("admit_colleague_prs_to_board") is False

    def test_clear_overlay_absent_row_exits_nonzero(self) -> None:
        # A global row exists, but clearing the overlay scope (no row there) is loud.
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true")
        with pytest.raises(SystemExit):
            call_command("config_setting", "clear", "admit_colleague_prs_to_board", "--overlay", "ov")

    def test_get_with_overlay_reports_db_source(self) -> None:
        call_command("config_setting", "set", "issue_implementer_max_concurrent", "7", "--overlay", "ov")
        out = StringIO()
        call_command("config_setting", "get", "issue_implementer_max_concurrent", "--overlay", "ov", stdout=out)
        rendered = out.getvalue().lower()
        assert "7" in rendered
        assert "db" in rendered
        assert "ov" in rendered

    def test_get_resolves_an_alias_row_and_names_its_decider(self) -> None:
        ConfigSetting.objects.set_value(
            "issue_implementer_max_concurrent", 7, scope="teatree", authorized_by="operator"
        )
        out = StringIO()

        call_command("config_setting", "get", "issue_implementer_max_concurrent", "--overlay", "t3-teatree", stdout=out)

        assert "issue_implementer_max_concurrent = 7" in out.getvalue()
        assert "[source: db, overlay 'teatree']" in out.getvalue()
        assert "[decided by operator]" in out.getvalue()

    def test_get_resolves_global_row_for_an_overlay(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", 7)
        out = StringIO()

        call_command("config_setting", "get", "issue_implementer_max_concurrent", "--overlay", "t3-teatree", stdout=out)

        assert "issue_implementer_max_concurrent = 7" in out.getvalue()
        assert "[source: db, global]" in out.getvalue()

    def test_list_names_each_rows_scope(self) -> None:
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true")
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
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)

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
        call_command("config_setting", "set", "admit_colleague_prs_to_board", "true")
        target = self.tmp_path / "dump.toml"
        call_command("config_setting", "export", "--output", str(target))
        doc = tomllib.loads(target.read_text(encoding="utf-8"))
        assert _teatree(doc)["admit_colleague_prs_to_board"] is True

    def test_export_overlay_scopes_the_dump(self) -> None:
        call_command("config_setting", "set", "mode", '"auto"')  # global
        call_command("config_setting", "set", "mode", '"interactive"', "--overlay", "myproj")
        out = StringIO()
        call_command("config_setting", "export", "--overlay", "myproj", stdout=out)
        doc = tomllib.loads(out.getvalue())
        assert doc["overlays"]["myproj"]["mode"] == "interactive"
        # The global scope is excluded when a single overlay is requested.
        assert "teatree" not in doc

    def test_banned_content_flag_requires_private_backup(self) -> None:
        out, err = StringIO(), StringIO()
        with pytest.raises(SystemExit):
            call_command("config_setting", "export", "--include-banned-term-values", stdout=out, stderr=err)
        assert out.getvalue() == ""
        assert "--include-private" in err.getvalue()


class TestConfigSettingExportFilters(TestCase):
    """The two export filters over the CLI — both off by default, both together = the file shape."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)

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
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)

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

    def test_import_rejects_a_former_setting_name(self) -> None:
        path = self._write_toml('[teatree]\nspeed = "slow"\n')
        err = StringIO()
        with pytest.raises(SystemExit):
            call_command("config_setting", "import", "--input", str(path), stdout=StringIO(), stderr=err)
        assert "rejected speed" in err.getvalue()
        assert ConfigSetting.objects.get_effective("wip") is None

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
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)

    #: One synthetic row per KEY-classified withhold class — a one-class fixture passes against
    #: a one-class fix. The fourth class (a banned term matched on the VALUE) is unit-covered in
    #: ``test_config_migration`` instead: it needs live scan terms, and this command resolves
    #: those through a cold reader that cannot see a test transaction's rows.
    PRIVATE_ROWS: ClassVar[dict[str, str | list[str] | dict[str, list[str]]]] = {
        "banned_term_registry": {"leak": list(TEST_TERM_REGISTRY["leak"]), "prose_collider": []},  # private-key
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
            if isinstance(value, dict):
                fragments.extend(term for terms in value.values() for term in terms)
            else:
                fragments.extend(value if isinstance(value, list) else [value])
        return fragments

    def _backup(self) -> tuple[Path, str]:
        path = self.tmp_path / "backup.toml"
        err = StringIO()
        call_command(
            "config_setting",
            "export",
            "--include-private",
            "--include-banned-term-values",
            "--output",
            str(path),
            stderr=err,
        )
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


class TestGetNamesWhoDecided(TestCase):
    """A recorded decision nothing shows is a column, not an audit.

    Every write stamps `written_by`, and the operator asking "why is this off" was given
    the value and no answer — so the decision sat in a table with no reader.
    """

    def _get(self, key: str) -> str:
        out = StringIO()
        call_command("config_setting", "get", key, stdout=out)
        return out.getvalue()

    def test_a_stored_row_names_its_decider(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 3, authorized_by="the-owner")

        assert "[decided by the-owner]" in self._get("merge_wip")

    def test_a_value_that_is_not_stored_names_nobody(self) -> None:
        # A code default was decided by the release, not by a person on this box.
        assert "decided by" not in self._get("merge_wip")

    def test_an_invalid_other_row_does_not_break_single_key_diagnostics(self) -> None:
        ConfigSetting.objects.create(key="merge_wip", scope="", value="garbage")
        ConfigSetting.objects.set_value("agent_max_turns", 7)
        assert "agent_max_turns = 7" in self._get("agent_max_turns")

    def test_an_invalid_other_row_does_not_break_an_unset_key_diagnostic(self) -> None:
        ConfigSetting.objects.create(key="merge_wip", scope="", value="garbage")
        assert "agent_max_turns = " in self._get("agent_max_turns")

    def test_single_key_fallback_keeps_the_autonomy_dependent_mode(self) -> None:
        ConfigSetting.objects.set_value("autonomy", "babysit")
        ConfigSetting.objects.set_value("mode", "interactive")
        assert get_effective_setting("mode") is Mode.INTERACTIVE


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
        # provision_ram_ceiling_percent ships 75; 65 differs, so it seeds.
        text = self._seed("provision_ram_ceiling_percent", "65")
        assert ConfigSetting.objects.get_effective("provision_ram_ceiling_percent") == 65
        assert "created" in text
        row = ConfigSetting.objects.get(key="provision_ram_ceiling_percent")
        assert row.seeded_by == "entrypoint"
        assert row.seed_value == 65

    def test_seed_equal_to_code_default_writes_nothing(self) -> None:
        # provision_max_concurrency code default is 0; seeding 0 is a documented no-op.
        text = self._seed("provision_max_concurrency", "0")
        assert ConfigSetting.objects.filter(key="provision_max_concurrency").exists() is False
        assert "skipped-equals-default" in text

    def test_seed_preserves_operator_override(self) -> None:
        # 65 differs from the shipped 75, so the seed WOULD write — the override is what
        # stops it, not the equals-default skip.
        call_command("config_setting", "set", "provision_ram_ceiling_percent", "90")
        self._seed("provision_ram_ceiling_percent", "65")
        assert ConfigSetting.objects.get_effective("provision_ram_ceiling_percent") == 90

    def test_seed_refuses_unknown_key(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "seed", "not_a_setting", "1")

    def test_seed_refuses_invalid_json(self) -> None:
        with pytest.raises(SystemExit):
            call_command("config_setting", "seed", "provision_ram_ceiling_percent", "not-json")

    def test_seed_refuses_an_overlay_scope_its_reader_never_reads(self) -> None:
        stderr = StringIO()
        with pytest.raises(SystemExit) as exc_info:
            call_command("config_setting", "seed", "agent_tier_models", '{"cheap": "m"}', overlay="x", stderr=stderr)
        assert exc_info.value.code == 2
        assert "Set it globally instead" in stderr.getvalue()
        assert not ConfigSetting.objects.filter(key="agent_tier_models", scope="x").exists()


class TestCredentialPassKeySetting(TestCase):
    def test_set_stores_the_entry_a_known_credential_reads_on_this_venue(self) -> None:
        call_command("config_setting", "set", "notion_token_pass_key", '"venue/notion"', "--overlay", "t3-teatree")

        assert ConfigSetting.objects.get_effective("notion_token_pass_key", scope="t3-teatree") == "venue/notion"

    def test_set_refuses_a_credential_no_overlay_reads(self) -> None:
        with pytest.raises(SystemExit) as caught:
            call_command("config_setting", "set", "notoin_token_pass_key", '"venue/notion"')

        assert caught.value.code == 2
        assert not ConfigSetting.objects.filter(key="notoin_token_pass_key").exists()

    def test_set_refuses_a_value_that_is_not_an_entry_name(self) -> None:
        for entry in ('""', '"two words"', "3"):
            with self.subTest(entry=entry), pytest.raises(SystemExit):
                call_command("config_setting", "set", "notion_token_pass_key", entry)

        assert not ConfigSetting.objects.filter(key="notion_token_pass_key").exists()

    def test_get_names_the_declared_default_when_no_row_exists(self) -> None:
        out = StringIO()

        call_command("config_setting", "get", "gitlab_token_pass_key", "--overlay", "t3-teatree", stdout=out)

        assert "gitlab_token_pass_key = 'gitlab/pat'  [source: overlay declared default]" in out.getvalue()

    def test_get_names_the_db_scope_a_row_resolved_from(self) -> None:
        ConfigSetting.objects.set_value("gitlab_token_pass_key", "venue/pat")
        out = StringIO()

        call_command("config_setting", "get", "gitlab_token_pass_key", "--overlay", "t3-teatree", stdout=out)

        assert "gitlab_token_pass_key = 'venue/pat'  [source: db, global scope]" in out.getvalue()


class _TermGuardCase(TestCase):
    """Drives ``config_setting`` the way an operator does, each stream captured on its own."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.tmp_path = tmp_path
        self.monkeypatch = monkeypatch
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.setenv("TEATREE_TERM_REGISTRY", TEST_TERM_REGISTRY_JSON)

    def _run(self, *args: str, command: str = "config_setting") -> tuple[int, str, str]:
        out, err = StringIO(), StringIO()
        try:
            call_command(command, *args, stdout=out, stderr=err)
        except SystemExit as exc:
            return int(exc.code or 0), out.getvalue(), err.getvalue()
        return 0, out.getvalue(), err.getvalue()

    def _set(self, key: str, value: object, *flags: str) -> None:
        assert self._run("set", key, json.dumps(value), *flags)[0] == 0

    def _file(self, name: str, text: str) -> Path:
        path = self.tmp_path / name
        path.write_text(text, encoding="utf-8")
        return path

    def _no_terms(self, registry: str = "") -> None:
        if registry:
            self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", registry)
        else:
            self.monkeypatch.delenv("TEATREE_TERM_REGISTRY")
            assert load_registry() is None


class TestEveryUnknownKeySaysWhy(_TermGuardCase):
    ANSWERS: ClassVar[dict[str, tuple[str, ...]]] = {
        "speed": ("renamed to wip",),
        "branch_prefix": ("retired:", "#2731"),
        "not_a_setting_probe": ("not a declared setting",),
    }

    def test_set_and_import_name_the_reason(self) -> None:
        for key, fragments in self.ANSWERS.items():
            path = self._file("f.toml", f'[teatree]\n{key} = "x"\n')
            for args in (("set", key, '"x"'), ("import", "--input", str(path))):
                with self.subTest(key=key, surface=args[0]):
                    code, _, err = self._run(*args)
                    assert code == 2
                    assert all(fragment in err for fragment in fragments), err
                    assert "git " not in err
                    assert not ConfigSetting.objects.filter(key=key).exists()


class TestRefusalsNameTheClassNeverTheValue(_TermGuardCase):
    def test_an_export_names_a_term_row_by_key_and_class(self) -> None:
        self._set("issue_implementer_label", CARRIER)
        self._set("merge_wip", 4)
        code, out, err = self._run("export")
        assert code == 0
        assert "withheld issue_implementer_label  [global]  (banned-term)" in err
        assert _carries_no_term(out, err)
        assert _teatree(tomllib.loads(out))["merge_wip"] == 4

    def test_an_import_refusal_names_the_key_and_class(self) -> None:
        path = self._file("f.toml", f'[teatree]\nissue_implementer_label = "{CARRIER}"\n')
        code, out, err = self._run("import", "--input", str(path))
        assert code == 2
        assert "rejected issue_implementer_label  [global]  (secret (banned-term))" in err
        assert _carries_no_term(out, err)
        assert "[source: env/default]" in self._run("get", "issue_implementer_label")[1]

    def test_a_banned_term_value_rejected_as_invalid_on_restore_drops_the_parser_message(self) -> None:
        backup = "[backup]\ninclude_private = true\n[teatree]\n"
        code, out, err = self._run(
            "import", "--input", str(self._file("f.toml", f'{backup}mode = "{LEAK}"\n')), "--restore-private"
        )
        assert code == 2
        assert "rejected mode" in err
        assert "invalid (banned-term value; message withheld)" in err
        assert not line_matches(out, SCAN)
        assert not line_matches(err, SCAN)
        control = self._file("g.toml", f'{backup}mode = "not-a-mode"\n')
        assert "not-a-mode" in self._run("import", "--input", str(control), "--restore-private")[2]

    def test_an_invalid_message_quoting_a_term_is_withheld(self) -> None:
        self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [Mode.AUTO.value]}))
        code, out, err = self._run("import", "--input", str(self._file("f.toml", '[teatree]\nmode = "not-a-mode"\n')))
        assert code == 2
        assert "rejected mode  [global]  (invalid (banned-term; message withheld))" in err
        assert _carries_no_term(out, err, terms=(Mode.AUTO.value,))

    def test_a_non_ascii_term_is_withheld_by_class_on_export(self) -> None:
        self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [NON_ASCII_TERM]}))
        self._set("issue_implementer_label", f"{NON_ASCII_TERM} desk")
        self._set("merge_wip", 4)
        code, out, err = self._run("export")
        assert code == 0
        assert "withheld issue_implementer_label  [global]  (banned-term)" in err
        assert _carries_no_term(out, err, terms=(NON_ASCII_TERM,))
        assert _teatree(tomllib.loads(out))["merge_wip"] == 4


class TestAStoreWithNoTerms(_TermGuardCase):
    def _assert_export_refused(self) -> None:
        self._set("merge_wip", 4)
        target = self.tmp_path / "out.toml"
        code, out, err = self._run("export", "--output", str(target))
        assert code == 2
        assert "banned_term_registry" in err
        assert "TEATREE_TERM_REGISTRY" in err
        assert "--include-private --include-banned-term-values" in err
        assert not target.exists()
        assert out == ""

    def test_export_refuses_an_unset_registry_and_names_the_escape(self) -> None:
        self._no_terms()
        self._assert_export_refused()

    def test_export_refuses_a_registry_holding_only_allow_terms(self) -> None:
        self._no_terms(json.dumps({"allow": ["placeholder-allow-entry"]}))
        self._assert_export_refused()

    def test_a_scanned_export_still_writes_its_file(self) -> None:
        self._set("merge_wip", 4)
        target = self.tmp_path / "out.toml"
        assert self._run("export", "--output", str(target))[0] == 0
        assert _teatree(tomllib.loads(target.read_text(encoding="utf-8")))["merge_wip"] == 4

    def test_import_proceeds_unscanned_with_one_notice_and_withheld_values(self) -> None:
        self._no_terms()
        path = self._file("f.toml", "[teatree]\nmerge_wip = 4\n")
        for flags in (("--dry-run",), ()):
            code, out, err = self._run("import", "--input", str(path), *flags)
            assert code == 0
            [notice] = [line for line in err.splitlines() if "not content-scanned" in line]
            assert "banned_term_registry" in notice
            assert "merge_wip = <withheld: private>" in out
        assert ConfigSetting.objects.get_effective("merge_wip") == 4

    def test_a_scanned_import_shows_the_value_and_no_notice(self) -> None:
        code, out, err = self._run("import", "--input", str(self._file("f.toml", "[teatree]\nmerge_wip = 4\n")))
        assert code == 0
        assert "not content-scanned" not in err
        assert "merge_wip = 4" in out

    def test_the_full_personal_backup_is_explicit_and_restores_without_terms(self) -> None:
        self._no_terms()
        self._set("merge_wip", 4)
        self._set("slack_user_id", "synthetic-user-ref")
        code, _, err = self._run("export", "--include-private")
        assert (code, "--include-banned-term-values" in err) == (2, True)
        alone = self.tmp_path / "g.toml"
        code, _, err = self._run("export", "--include-banned-term-values", "--output", str(alone))
        assert (code, "--include-private" in err, alone.exists()) == (2, True, False)
        code, out, err = self._run("export", "--include-private", "--include-banned-term-values")
        assert (code, "--output" in err, out) == (2, True, "")
        backup = self.tmp_path / "f.toml"
        assert self._run("export", "--include-private", "--include-banned-term-values", "--output", str(backup))[0] == 0
        assert is_private_backup(tomllib.loads(backup.read_text(encoding="utf-8")))
        self._run("clear", "merge_wip")
        self._run("clear", "slack_user_id")
        code, out, _ = self._run("import", "--input", str(backup), "--restore-private")
        assert code == 0
        assert ConfigSetting.objects.get_effective("merge_wip") == 4
        assert ConfigSetting.objects.get_effective("slack_user_id") == "synthetic-user-ref"
        assert "merge_wip = <withheld: private>" in out
        assert "slack_user_id = <withheld: private>" in out
        assert "synthetic-user-ref" not in out

    def test_term_values_never_print_to_stdout_even_with_terms(self) -> None:
        self._set("issue_implementer_label", CARRIER)
        code, out, err = self._run("export", "--include-private", "--include-banned-term-values")
        assert (code, out) == (2, "")
        assert "--output" in err


class TestPrivateExportWithholdsRowsNotTheWholeFile(_TermGuardCase):
    def setUp(self) -> None:
        call_command("seed_loops", stdout=StringIO())
        self.shipped_mode = min(shipped_seed_table("modes"))

    def _arrange(self) -> None:
        self._set("issue_implementer_label", CARRIER)
        self._set("issue_implementer_label", CARRIER, "--overlay", "probe")
        self._set("slack_user_id", "synthetic-user-ref")
        self._set("banned_term_registry", TEST_TERM_REGISTRY)
        self._run("create", "probe", "--description", f"{LEAK} preset", command="loop_preset")
        self._run("edit", self.shipped_mode, "--description", f"{LEAK} preset", command="loop_preset")

    def test_every_shape_withholds_the_term_rows_and_still_writes_a_marked_file(self) -> None:
        self._arrange()
        for shape in ((), ("--overlay", "probe"), ("--default-keys-only", "--include-defaults")):
            target = self.tmp_path / "f.toml"
            code, out, err = self._run("export", "--include-private", *shape, "--output", str(target))
            text = target.read_text(encoding="utf-8")
            assert code == 0, err
            assert _carries_no_term(text, out, err)
            assert is_private_backup(tomllib.loads(text))
            assert "(banned-term)" in err
            assert "--include-banned-term-values" in err
        defaults = tomllib.loads(text)
        assert _teatree(defaults)["issue_implementer_label"] == ""

    def test_the_full_shape_keeps_the_key_classes_and_names_each_withheld_row(self) -> None:
        self._arrange()
        target = self.tmp_path / "f.toml"
        _, _, err = self._run("export", "--include-private", "--output", str(target))
        assert "synthetic-user-ref" in target.read_text(encoding="utf-8")
        for key in ("issue_implementer_label", "banned_term_registry", "description"):
            assert f"withheld {key} " in err

    def test_the_second_flag_carries_the_term_rows(self) -> None:
        self._arrange()
        target = self.tmp_path / "f2.toml"
        assert self._run("export", "--include-private", "--include-banned-term-values", "--output", str(target))[0] == 0
        assert line_matches(target.read_text(encoding="utf-8"), SCAN)

    def test_a_stored_term_registry_is_withheld_when_the_terms_come_from_the_environment(self) -> None:
        self._set("banned_term_registry", {"leak": ["stored-only-placeholder"]})
        for flags, carried in (((), False), (("--include-banned-term-values",), True)):
            with self.subTest(flags=flags):
                target = self.tmp_path / "f.toml"
                code, _, err = self._run("export", "--include-private", *flags, "--output", str(target))
                assert code == 0, err
                assert ("stored-only-placeholder" in target.read_text(encoding="utf-8")) is carried
                assert ("withheld banned_term_registry  [global]  (banned-term)" in err) is not carried


class TestEveryPrivateExportIsMarkedAndRestores(_TermGuardCase):
    SHAPES: ClassVar[tuple[tuple[str, ...], ...]] = (
        (),
        ("--overlay", "probe"),
        ("--default-keys-only", "--include-defaults"),
    )

    def setUp(self) -> None:
        call_command("seed_loops", stdout=StringIO())
        self._run("edit", min(shipped_seed_table("modes")), "--description", f"{LEAK} preset", command="loop_preset")

    def _unstamped(self, shape: tuple[str, ...]) -> dict[str, object]:
        # The same emitter scanning nothing: over a store with no key-class row only the stamp may differ.
        export = export_db_to_toml(
            shape[1] if shape[:1] == ("--overlay",) else None,
            scan_terms=(),
            default_keys_only="--default-keys-only" in shape,
            include_defaults="--include-defaults" in shape,
        )
        return tomllib.loads(export.toml)

    def test_each_shape_is_its_unstamped_export_plus_the_marker(self) -> None:
        self._set("issue_implementer_label", CARRIER)
        self._set("issue_implementer_label", CARRIER, "--overlay", "probe")
        for shape in self.SHAPES:
            with self.subTest(shape=shape):
                target = self.tmp_path / "f.toml"
                flags = ("--include-private", "--include-banned-term-values", *shape, "--output", str(target))
                code, _, err = self._run("export", *flags)
                expected = {**self._unstamped(shape), "backup": {"include_private": True}}
                assert code == 0
                assert "PERSONAL BACKUP" in err
                assert tomllib.loads(target.read_text(encoding="utf-8")) == expected

    def test_each_shape_restores_the_cleared_carrier_and_shows_it_withheld(self) -> None:
        for shape in self.SHAPES:
            scope = shape[:2] if shape[:1] == ("--overlay",) else ()
            with self.subTest(shape=shape):
                self._set("issue_implementer_label", CARRIER, *scope)
                target = self.tmp_path / "f.toml"
                flags = ("--include-private", "--include-banned-term-values", *shape, "--output", str(target))
                assert self._run("export", *flags)[0] == 0
                self._run("clear", "issue_implementer_label", *scope)
                code, out, err = self._run("import", "--input", str(target))
                assert (code, "--restore-private" in err) == (2, True)
                code, out, err = self._run("import", "--input", str(target), "--restore-private")
                stored = self._run("get", "issue_implementer_label", *scope)[1]
                assert code == 0, err
                assert "issue_implementer_label = <withheld: private>" in out
                assert _carries_no_term(out, err)
                assert "[source: db" in stored


class TestASharedExportStaysUnmarked(_TermGuardCase):
    def test_the_defaults_shape_is_the_shipped_file_byte_for_byte(self) -> None:
        code, out, _ = self._run("export", "--default-keys-only", "--include-defaults")
        assert code == 0
        assert out == DEFAULTS_TOML.read_text(encoding="utf-8")

    def test_a_shared_full_export_carries_no_marker(self) -> None:
        self._set("merge_wip", 4)
        code, out, err = self._run("export")
        assert code == 0
        assert not is_private_backup(tomllib.loads(out))
        assert "PERSONAL BACKUP" not in err


class TestATermCarryingNameNeverLeavesTheBox(_TermGuardCase):
    def _arrange(self) -> None:
        self._set("merge_wip", 4, "--overlay", OV)
        self._run("create", f"{LEAK}-probe", command="loop_preset")

    def test_a_shared_export_withholds_the_scope_without_naming_it(self) -> None:
        self._arrange()
        code, out, err = self._run("export")
        assert code == 0
        assert _carries_no_term(out, err)
        assert "under a name that carries a banned term" in err
        assert "(banned-term)" in err

    def test_a_private_export_withholds_it_from_the_file_too(self) -> None:
        self._arrange()
        target = self.tmp_path / "f.toml"
        code, out, err = self._run("export", "--include-private", "--output", str(target))
        assert code == 0
        assert _carries_no_term(target.read_text(encoding="utf-8"), out, err)
        assert "under a name that carries a banned term" in err

    def test_an_import_refuses_the_scope_without_naming_it(self) -> None:
        path = self._file("g.toml", f"[overlays.{OV}]\nmerge_wip = 4\n")
        code, out, err = self._run("import", "--input", str(path))
        assert code == 2
        assert _carries_no_term(out, err)
        assert "rejected 1 row(s)  [under a name that carries a banned term]  (secret (banned-term))" in err
        assert ConfigSetting.objects.get_effective("merge_wip", scope=OV) is None

    def test_a_registry_entry_named_after_a_term_is_withheld_from_every_export(self) -> None:
        self._set("e2e_repos", {f"{LEAK}-repo": {"url": "https://example.invalid/r.git"}, "probe-repo": {"url": ""}})
        self._set("peer_instances", {f"{LEAK}-peer": {"url": "http://127.0.0.1:9"}})
        target = self.tmp_path / "f.toml"
        for flags in ((), ("--include-private", "--output", str(target))):
            with self.subTest(flags=flags):
                code, out, err = self._run("export", *flags)
                text = target.read_text(encoding="utf-8") if flags else out
                assert code == 0, err
                assert _carries_no_term(text, out, err)
                assert "withheld 1 row(s)  [under a name that carries a banned term]" in err
                assert list(tomllib.loads(text)["e2e_repos"]) == ["probe-repo"]

    def test_a_restore_refusal_under_the_scope_does_not_name_it(self) -> None:
        text = f'[backup]\ninclude_private = true\n[overlays.{OV}]\nmerge_wip = "not-an-int"\n'
        code, out, err = self._run("import", "--input", str(self._file("g.toml", text)), "--restore-private")
        assert code == 2
        assert _carries_no_term(out, err)
        assert "rejected merge_wip  [under a name that carries a banned term]  (invalid" in err

    def test_the_term_values_backup_carries_and_restores_the_scope_without_printing_it(self) -> None:
        self._arrange()
        backup = self.tmp_path / "f3.toml"
        assert self._run("export", "--include-private", "--include-banned-term-values", "--output", str(backup))[0] == 0
        assert tomllib.loads(backup.read_text(encoding="utf-8"))["overlays"][OV]["merge_wip"] == 4
        self._run("clear", "merge_wip", "--overlay", OV)
        code, out, err = self._run("import", "--input", str(backup), "--restore-private")
        assert code == 0, err
        assert ConfigSetting.objects.get_effective("merge_wip", scope=OV) == 4
        assert not line_matches(out, SCAN)
        assert "[under a name that carries a banned term]" in out


class TestTheSeedImportIsScanned(_TermGuardCase):
    def setUp(self) -> None:
        call_command("seed_loops", stdout=StringIO())
        self.mode = min(shipped_seed_table("modes"))
        self.shipped_description = Preset.objects.get(name=self.mode).description

    def test_a_term_in_a_preset_field_is_refused_by_class(self) -> None:
        shown = self._run("show", self.mode, command="loop_preset")[1]
        path = self._file("f.toml", f'[modes.{self.mode}]\ndescription = "{LEAK} preset"\n')
        code, out, err = self._run("import", "--input", str(path))
        assert code == 2
        assert any("description" in line and "secret (banned-term)" in line for line in err.splitlines())
        assert _carries_no_term(out, err)
        assert self._run("show", self.mode, command="loop_preset")[1] == shown
        assert Preset.objects.get(name=self.mode).description == self.shipped_description

    def test_a_clean_preset_field_still_imports(self) -> None:
        path = self._file("f.toml", f'[modes.{self.mode}]\ndescription = "a tuned preset"\n')
        assert self._run("import", "--input", str(path))[0] == 0
        assert Preset.objects.get(name=self.mode).description == "a tuned preset"

    def test_a_restore_shows_a_term_bearing_seed_field_withheld(self) -> None:
        text = f'[backup]\ninclude_private = true\n[modes.{self.mode}]\ndescription = "{LEAK} preset"\n'
        code, out, _ = self._run("import", "--input", str(self._file("f.toml", text)), "--restore-private", "--dry-run")
        assert code == 0
        assert "would import description = <withheld: private>" in out
        assert not line_matches(out, SCAN)

    def test_an_entry_named_after_a_term_is_refused_without_the_name(self) -> None:
        path = self._file("f.toml", f'[modes.{LEAK}-probe]\ndescription = "a tuned preset"\n')
        code, out, err = self._run("import", "--input", str(path))
        assert code == 2
        assert "rejected 1 row(s)  [under a name that carries a banned term]  (secret (banned-term))" in err
        assert _carries_no_term(out, err)

    def test_an_unscanned_import_shows_a_seed_field_withheld(self) -> None:
        self._no_terms()
        path = self._file("f.toml", f'[modes.{self.mode}]\ndescription = "a tuned preset"\n')
        code, out, _ = self._run("import", "--input", str(path), "--dry-run")
        assert code == 0
        assert "would import description = <withheld: private>" in out
        assert "a tuned preset" not in out


class TestAShippedEntryNamedAfterATerm(_TermGuardCase):
    def setUp(self) -> None:
        call_command("seed_loops", stdout=StringIO())
        self.mode = min(shipped_seed_table("modes"))

    def _arrange(self) -> None:
        self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [self.mode]}))
        self._run("edit", self.mode, "--description", "a tuned preset", command="loop_preset")

    def test_a_shared_export_withholds_it_without_the_name(self) -> None:
        self._arrange()
        code, out, err = self._run("export")
        assert code == 0
        assert "withheld 1 row(s)  [under a name that carries a banned term]  (banned-term)" in err
        assert _carries_no_term(out, err, terms=(self.mode,))

    def test_the_term_values_backup_restores_it_without_the_name(self) -> None:
        self._arrange()
        backup = self.tmp_path / "f.toml"
        assert self._run("export", "--include-private", "--include-banned-term-values", "--output", str(backup))[0] == 0
        assert self.mode in tomllib.loads(backup.read_text(encoding="utf-8"))["modes"]
        self._run("edit", self.mode, "--description", "another preset", command="loop_preset")
        code, out, err = self._run("import", "--input", str(backup), "--restore-private")
        assert code == 0, err
        assert Preset.objects.get(name=self.mode).description == "a tuned preset"
        assert 'imported description = "a tuned preset"  [under a name that carries a banned term]' in out
        assert _carries_no_term(out, err, terms=(self.mode,))

    def _restore(self, body: str, *, marked: bool = True) -> tuple[int, str, str]:
        head = "[backup]\ninclude_private = true\n" if marked else ""
        path = self._file("r.toml", f"{head}[modes.{self.mode}]\n{body}")
        return self._run("import", "--input", str(path), "--restore-private")

    def test_a_restore_refusing_it_drops_the_reason_with_the_name(self) -> None:
        self._arrange()
        code, out, err = self._restore("entries = {}\n")
        assert code == 2
        assert "rejected entries  [under a name that carries a banned term]  (invalid mask (banned-term; message" in err
        assert _carries_no_term(out, err, terms=(self.mode,))

    def test_a_restore_onto_an_unseeded_box_refuses_it_without_the_name(self) -> None:
        self._arrange()
        Preset.objects.filter(name=self.mode).delete()
        code, out, err = self._restore('description = "a tuned preset"\n')
        assert code == 2
        assert "rejected description  [under a name that carries a banned term]  (no modes row yet" in err
        assert _carries_no_term(out, err, terms=(self.mode,))

    def test_a_restore_with_an_unknown_field_refuses_it_without_the_name(self) -> None:
        self._arrange()
        code, out, err = self._restore("not_a_field = 1\n")
        assert code == 2
        assert "rejected not_a_field  [under a name that carries a banned term]  (unknown field)" in err
        assert _carries_no_term(out, err, terms=(self.mode,))

    def test_an_unmarked_file_is_still_refused_without_the_name_under_the_restore_flag(self) -> None:
        self._arrange()
        code, out, err = self._restore('description = "another preset"\n', marked=False)
        assert code == 2
        assert "rejected 1 row(s)  [under a name that carries a banned term]  (secret (banned-term))" in err
        assert Preset.objects.get(name=self.mode).description == "a tuned preset"
        assert _carries_no_term(out, err, terms=(self.mode,))


class TestAMaskRefusalNeverQuotesATerm(_TermGuardCase):
    def setUp(self) -> None:
        call_command("seed_loops", stdout=StringIO())
        self.mode = min(shipped_seed_table("modes"))
        self.loops = sorted(Loop.objects.values_list("name", flat=True))

    def _import_mask(self, entries: dict[str, bool], *flags: str, marked: bool = False) -> tuple[int, str, str]:
        rows = "".join(f'"{loop}" = {str(on).lower()}\n' for loop, on in entries.items())
        head = "[backup]\ninclude_private = true\n" if marked else ""
        path = self._file("m.toml", f"{head}[modes.{self.mode}.entries]\n{rows}")
        return self._run("import", "--input", str(path), *flags)

    def test_a_restored_mask_keyed_by_a_term_of_any_class_is_refused_without_it(self) -> None:
        for term_class in ("leak", "overlay"):
            with self.subTest(term_class=term_class):
                entries = {**dict.fromkeys(self.loops, False), f"{TEST_TERM_REGISTRY[term_class][0]}-loop": True}
                code, out, err = self._import_mask(entries, "--restore-private", marked=True)
                assert code == 2
                assert "(invalid mask (banned-term; message withheld))" in err
                assert _carries_no_term(out, err)

    def test_a_mask_leaving_out_a_loop_named_after_a_term_is_refused_without_it(self) -> None:
        term = self.loops[0]
        self.monkeypatch.setenv("TEATREE_TERM_REGISTRY", json.dumps({"leak": [term]}))
        code, out, err = self._import_mask(dict.fromkeys(self.loops[1:], False))
        assert code == 2
        assert "(invalid mask (banned-term; message withheld))" in err
        assert _carries_no_term(out, err, terms=(term,))

    def test_an_unscanned_mask_refusal_withholds_the_keys_it_quotes(self) -> None:
        self._no_terms()
        code, _, err = self._import_mask({**dict.fromkeys(self.loops, False), "stale-probe-loop": True}, "--dry-run")
        assert code == 2
        assert "(invalid mask (not content-scanned; message withheld))" in err
        assert "stale-probe-loop" not in err

    def test_a_clean_mask_refusal_still_names_what_is_wrong(self) -> None:
        code, _, err = self._import_mask(dict.fromkeys(self.loops[1:], False))
        assert code == 2
        assert f"no opinion on {self.loops[0]}" in err


class TestTheTermValuesBackupNamesOnlyWhatAScanClears(_TermGuardCase):
    def _arrange(self) -> None:
        self._run("create", f"{LEAK}-probe", command="loop_preset")
        self._run("create", "probe-preset", command="loop_preset")
        ConfigSetting.objects.set_value("loop_preset_transition_stamp", "maintenance", scope=OV)

    def _backup(self) -> tuple[int, str, str]:
        target = self.tmp_path / "f.toml"
        return self._run("export", "--include-private", "--include-banned-term-values", "--output", str(target))

    def _omitted_lines(self, err: str) -> list[str]:
        return [line for line in err.splitlines() if line.startswith("  omitted ")]

    def test_a_clean_name_is_named_and_a_term_carrying_one_is_not(self) -> None:
        self._arrange()
        code, out, err = self._backup()
        assert code == 0
        assert not line_matches(out, SCAN)
        assert not line_matches(err, SCAN)
        assert any("modes.probe-preset" in line and "not shipped" in line for line in self._omitted_lines(err))
        assert any("under a name that carries a banned term" in line for line in self._omitted_lines(err))

    def test_with_no_term_to_scan_no_omitted_row_is_named(self) -> None:
        self._arrange()
        self._no_terms()
        code, out, err = self._backup()
        lines = self._omitted_lines(err)
        assert code == 0
        assert not line_matches(out, SCAN)
        assert not line_matches(err, SCAN)
        assert lines
        assert all("[names not shown: no term to scan them]" in line for line in lines), lines


class TestABackupFromABoxWithACustomPresetRestores(_TermGuardCase):
    def test_the_unshipped_preset_is_omitted_by_name_and_the_rest_restores(self) -> None:
        call_command("seed_loops", stdout=StringIO())
        self._run("create", "probe-preset", command="loop_preset")
        self._set("merge_wip", 4)
        backup = self.tmp_path / "f.toml"
        code, _, err = self._run("export", "--include-private", "--include-banned-term-values", "--output", str(backup))
        assert code == 0
        assert any("modes.probe-preset" in line and "not shipped" in line for line in err.splitlines())
        self._run("clear", "merge_wip")
        code, _, err = self._run("import", "--input", str(backup), "--restore-private")
        assert code == 0, err
        assert ConfigSetting.objects.get_effective("merge_wip") == 4
