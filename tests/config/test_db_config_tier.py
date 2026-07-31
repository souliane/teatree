# test-path: cross-cutting
"""DB-home settings in the effective-settings resolution chain (#1775 partition).

A DB-home field's SOLE source is the ``ConfigSetting`` store (global + overlay
rows) plus the ``T3_*`` env layer (which still wins). An empty table resolves the
dataclass default. Per DB-home field:

    env -> ConfigSetting (overlay then global) -> dataclass default

Pilot setting: ``orchestrate_claim_enabled`` (a boolean opt-in gate, default
``False``) so an EMPTY table is a provable no-op and the precedence is observable.

Integration-first: real ``ConfigSetting`` rows against the real DB, the active
overlay set via ``T3_OVERLAY_NAME``.
"""

import os
from unittest import mock

import pytest
from django.core.exceptions import AppRegistryNotReady
from django.db.utils import OperationalError, ProgrammingError
from django.test import TestCase

from teatree.config import get_effective_settings
from teatree.config.override_reader import load_global_rows, load_overlay_rows
from teatree.config.resolution import env_setting_overrides, read_setting_layers
from teatree.core.models import ConfigSetting


class TestDbConfigTier(TestCase):
    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ORCHESTRATE_CLAIM_ENABLED", raising=False)
        self.monkeypatch = monkeypatch

    def test_empty_table_is_a_no_op(self) -> None:
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().orchestrate_claim_enabled is False

    def test_db_is_the_sole_source_for_a_db_home_field(self) -> None:
        assert get_effective_settings().orchestrate_claim_enabled is False
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True)
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_db_row_is_the_sole_overlay_source(self) -> None:
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is False
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_env_wins_over_db_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        self.monkeypatch.setenv("T3_ORCHESTRATE_CLAIM_ENABLED", "true")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_db_row_for_non_overridable_key_is_ignored(self) -> None:
        # The pilot is scoped to OVERLAY_OVERRIDABLE_SETTINGS so an unknown /
        # non-overridable key never silently mutates the resolved settings.
        ConfigSetting.objects.set_value("not_a_real_setting", "boom")
        assert get_effective_settings().orchestrate_claim_enabled is False

    def test_db_row_value_is_coerced_via_registry_parser(self) -> None:
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", "5")
        assert get_effective_settings().issue_implementer_max_concurrent == 5

    def test_clear_restores_dataclass_default(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True)
        assert get_effective_settings().orchestrate_claim_enabled is True
        ConfigSetting.objects.clear("orchestrate_claim_enabled")
        assert get_effective_settings().orchestrate_claim_enabled is False

    def test_bool_row_false_resolves_false(self) -> None:
        # #258 blocker 2: a stored real-bool ``False`` for an opt-in safety
        # setting must resolve to Python ``False`` — never truthy-coerced on.
        ConfigSetting.objects.set_value("allow_destructive_disk", value=False)
        assert get_effective_settings().allow_destructive_disk is False

    def test_quoted_bool_string_row_does_not_silently_enable(self) -> None:
        # #258 blocker 2 at the READ tier: a row storing the JSON STRING ``"false"``
        # must NOT silently enable the opt-in setting via ``bool("false") == True``.
        ConfigSetting.objects.set_value("allow_destructive_disk", "false")
        with pytest.raises(ValueError, match="allow_destructive_disk"):
            get_effective_settings()

    def test_bool_row_for_int_setting_is_rejected_loud(self) -> None:
        # #258 round 2: a row storing JSON ``true`` for an int-typed setting is
        # raised LOUD with the offending key, never coerced via ``int(True) == 1``.
        ConfigSetting.objects.set_value("issue_implementer_max_concurrent", value=True)
        with pytest.raises(ValueError, match="issue_implementer_max_concurrent"):
            get_effective_settings()

    def test_scalar_row_for_list_setting_is_rejected_loud(self) -> None:
        # #258 round 2: a scalar row for a list-typed setting is raised LOUD, never
        # silently degraded to ``[]`` (which would mask a corrupt override).
        ConfigSetting.objects.set_value("excluded_skills", value=True)
        with pytest.raises(ValueError, match="excluded_skills"):
            get_effective_settings()

    def test_list_row_resolves_canonical_list(self) -> None:
        ConfigSetting.objects.set_value("excluded_skills", ["foo", "bar"])
        assert get_effective_settings().excluded_skills == ["foo", "bar"]


class TestPerOverlayDbScope(TestCase):
    """Per-overlay scope in the DB override tier — global then overlay (later wins).

    A global ``ConfigSetting`` row (``scope=""``) applies to every overlay; an
    overlay-scoped row applies to that overlay alone and beats the global DB row.
    The active overlay is set via ``T3_OVERLAY_NAME``.
    """

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)
        monkeypatch.delenv("T3_ORCHESTRATE_CLAIM_ENABLED", raising=False)
        self.monkeypatch = monkeypatch

    def test_overlay_scoped_db_row_beats_global_db_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_scoped_db_row_ignored_for_a_different_overlay(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is False
        assert get_effective_settings("another").orchestrate_claim_enabled is False

    def test_global_db_row_applies_when_overlay_has_no_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True)
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_scoped_row_resolves_through_named_overlay_path(self) -> None:
        # The loop's per-overlay scanners call get_effective_settings(overlay_name);
        # that path must read the overlay's DB scope too (no env applied there).
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False)
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="my-overlay")
        assert get_effective_settings("my-overlay").orchestrate_claim_enabled is True

    def test_env_still_wins_over_overlay_scoped_db_row(self) -> None:
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=False, scope="my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        self.monkeypatch.setenv("T3_ORCHESTRATE_CLAIM_ENABLED", "true")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_overlay_scope_matches_canonical_alias(self) -> None:
        # A row stored under the t3- entry-point spelling resolves for the short
        # alias active overlay (and vice versa).
        ConfigSetting.objects.set_value("orchestrate_claim_enabled", value=True, scope="t3-my-overlay")
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert get_effective_settings().orchestrate_claim_enabled is True

    def test_empty_overlay_scope_is_still_a_no_op(self) -> None:
        self.monkeypatch.setenv("T3_OVERLAY_NAME", "my-overlay")
        assert ConfigSetting.objects.count() == 0
        assert get_effective_settings().orchestrate_claim_enabled is False


class TestOverrideReadSignalsOnRealFailure(TestCase):
    """The DB override read never SILENTLY empties the tier on a REAL error (P1-B).

    ``load_global_rows`` / ``load_overlay_rows`` return ``({}, False)`` — an EMPTY tier that
    was read cleanly — only for genuine bootstrap states (missing table, DB not ready). A
    real read fault returns ``({}, True)``: the rows are equally empty, but the second
    element says teatree could not determine what the tier held, which is what stops the
    ``autonomy`` / ``require_human_approval_to_merge`` gates resolving to shipped defaults
    (#3873). The loud ERROR log + traceback is retained on top, for operator monitoring.
    """

    @pytest.fixture(autouse=True)
    def _clear_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_bootstrap_missing_table_error_is_a_silent_no_op(self) -> None:
        # A pre-migration / missing-table read while the app registry is NOT ready is the
        # legitimate bootstrap no-op: {} and NO error log. `_app_registry_ready` is patched
        # False to model the genuine bootstrap state (this TestCase has Django set up).
        with (
            mock.patch("teatree.config.override_reader._app_registry_ready", return_value=False),
            mock.patch.object(
                ConfigSetting.objects, "overrides_for_scope", side_effect=OperationalError("no such table")
            ),
            self.assertNoLogs("teatree.config", level="ERROR"),
        ):
            assert load_global_rows() == ({}, False)

    def test_app_registry_not_ready_error_is_always_silent(self) -> None:
        # AppRegistryNotReady is an unambiguous bootstrap state — silent regardless of the
        # readiness predicate (it is the very signal the predicate reads as not-ready).
        with (
            mock.patch.object(
                ConfigSetting.objects, "overrides_for_scope", side_effect=AppRegistryNotReady("apps not ready")
            ),
            self.assertNoLogs("teatree.config", level="ERROR"),
        ):
            assert load_global_rows() == ({}, False)

    def test_runtime_operational_error_is_logged_loud_not_silent(self) -> None:
        # THE fix: an OperationalError raised while the app registry IS ready (a locked DB,
        # a lock timeout, a mid-session drop) is a RUNTIME fault, not a bootstrap no-op — it
        # still degrades to {} but MUST be signalled loud, else the operator's DB-override
        # tier (autonomy, per-overlay mode, worker_quiescing) silently reverts to defaults.
        with (
            mock.patch("teatree.config.override_reader._app_registry_ready", return_value=True),
            mock.patch.object(
                ConfigSetting.objects, "overrides_for_scope", side_effect=OperationalError("database is locked")
            ),
            self.assertLogs("teatree.config", level="ERROR") as captured,
        ):
            # ``({}, True)`` — empty rows AND "this scope could not be read" (#3873). The
            # loud log is retained; what changed is that the caller can now tell this apart
            # from a healthy read of an empty tier.
            assert load_global_rows() == ({}, True)
        assert any("FAILED unexpectedly" in r.getMessage() for r in captured.records)

    def test_runtime_programming_error_is_logged_loud_not_silent(self) -> None:
        # ProgrammingError shares the runtime-vs-bootstrap distinction with OperationalError.
        with (
            mock.patch("teatree.config.override_reader._app_registry_ready", return_value=True),
            mock.patch.object(
                ConfigSetting.objects, "overrides_for_scope", side_effect=ProgrammingError("relation gone")
            ),
            self.assertLogs("teatree.config", level="ERROR") as captured,
        ):
            # ``({}, True)`` — empty rows AND "this scope could not be read" (#3873). The
            # loud log is retained; what changed is that the caller can now tell this apart
            # from a healthy read of an empty tier.
            assert load_global_rows() == ({}, True)
        assert any("FAILED unexpectedly" in r.getMessage() for r in captured.records)

    def test_runtime_operational_error_on_overlay_read_is_logged_loud(self) -> None:
        # The per-overlay reader shares the runtime-vs-bootstrap distinction.
        with (
            mock.patch("teatree.config.override_reader._app_registry_ready", return_value=True),
            mock.patch.object(ConfigSetting.objects, "exclude", side_effect=OperationalError("database is locked")),
            self.assertLogs("teatree.config", level="ERROR") as captured,
        ):
            assert load_overlay_rows("my-overlay") == ({}, True)
        assert any("FAILED unexpectedly" in r.getMessage() for r in captured.records)

    def test_real_read_error_is_logged_loud_not_silent(self) -> None:
        # A genuine read bug (not a bootstrap state) degrades to {} but is SIGNALLED loud —
        # never a silent empty override tier that fails OPEN on the safety gates.
        with (
            mock.patch.object(ConfigSetting.objects, "overrides_for_scope", side_effect=RuntimeError("corrupt read")),
            self.assertLogs("teatree.config", level="ERROR") as captured,
        ):
            # ``({}, True)`` — empty rows AND "this scope could not be read" (#3873). The
            # loud log is retained; what changed is that the caller can now tell this apart
            # from a healthy read of an empty tier.
            assert load_global_rows() == ({}, True)
        assert any("FAILED unexpectedly" in r.getMessage() for r in captured.records)

    def test_real_read_error_signals_through_effective_settings(self) -> None:
        # End to end: a real read failure surfaces loud (ERROR) rather than silently
        # resolving the safety gates to their fail-open defaults with no trace.
        with (
            mock.patch.object(ConfigSetting.objects, "overrides_for_scope", side_effect=RuntimeError("corrupt read")),
            self.assertLogs("teatree.config", level="ERROR") as captured,
        ):
            get_effective_settings()
        assert any("FAILED unexpectedly" in r.getMessage() for r in captured.records)

    def test_overlay_read_real_error_is_logged_loud(self) -> None:
        # The per-overlay reader shares the same signal-on-real-failure contract.
        with (
            mock.patch.object(ConfigSetting.objects, "exclude", side_effect=RuntimeError("corrupt read")),
            self.assertLogs("teatree.config", level="ERROR") as captured,
        ):
            assert load_overlay_rows("my-overlay") == ({}, True)
        assert any("FAILED unexpectedly" in r.getMessage() for r in captured.records)


class TestTheTierSeamsAreTheOneReader(TestCase):
    """``read_setting_layers`` / ``env_setting_overrides`` are public for ONE reason.

    ``get_effective_settings`` FOLDS the tiers into a ``UserSettings``; ``config.provenance``
    WALKS the same two seams to say which tier supplied a value. A second reader on either
    side would be a second resolution path, and the value shown and the tier credited for it
    could then disagree — so each seam's own answer is pinned here, together with the fold
    agreeing with it.
    """

    def test_the_two_db_scopes_come_back_separately(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        ConfigSetting.objects.set_value("merge_wip", 7, scope="demo")
        global_rows, overlay_rows = read_setting_layers("demo").db_rows
        assert (global_rows["merge_wip"], overlay_rows["merge_wip"]) == (4, 7)

    def test_an_unnamed_overlay_sees_no_overlay_rows(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 7, scope="demo")
        _global_rows, overlay_rows = read_setting_layers("").db_rows
        assert "merge_wip" not in overlay_rows

    def test_the_shipped_file_tier_is_carried_beside_the_db_scopes(self) -> None:
        assert "merge_wip" in read_setting_layers("").toml_rows

    def test_the_fold_serves_what_the_layers_say_wins(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        global_rows, _overlay_rows = read_setting_layers("").db_rows
        assert get_effective_settings().merge_wip == global_rows["merge_wip"]

    def test_an_unset_env_contributes_nothing(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            assert "merge_wip" not in env_setting_overrides()

    def test_a_t3_variable_is_read_under_its_flat_key_and_beats_a_stored_row(self) -> None:
        ConfigSetting.objects.set_value("merge_wip", 4)
        with mock.patch.dict(os.environ, {"T3_MERGE_WIP": "9"}):
            assert env_setting_overrides()["merge_wip"] == get_effective_settings().merge_wip == 9
