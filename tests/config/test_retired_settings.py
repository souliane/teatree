# test-path: cross-cutting
"""A retired DB-home settings key migrates or fails LOUD — never a silent revert.

souliane/teatree#3527: dropping ``eval_credential`` reverted a configured
operator to the default with no migration and no warning, because
``_coerce_setting_rows`` drops every row whose key is not a live field. Silence is the
bug: an operator who explicitly configured a setting cannot tell a removal apart
from "my value took effect".

:mod:`teatree.config.retired_settings` is the single registry both halves read.
A retired key carrying a ``replacement`` MIGRATES (its stored value resolves onto
the replacement field); one carrying none is REMOVED and resolving it emits a
loud stderr line naming the key, the reason, and the remedy — then falls through
to the default, so a stale row can never lock the operator out.

Integration-first per the Test-Writing Doctrine: real ``ConfigSetting`` rows
resolved through ``get_effective_settings``.
"""

import dataclasses

import pytest
from django.test import TestCase

from teatree.config import UserSettings, Wip, get_effective_settings
from teatree.config.retired_settings import (
    REMOVED_SETTING_KEYS,
    RENAMED_SETTING_KEYS,
    RETIRED_SETTINGS,
    RetiredSetting,
    removed_setting,
    warn_removed_setting,
)
from teatree.core.models import ConfigSetting


def _field_names() -> set[str]:
    return {f.name for f in dataclasses.fields(UserSettings)}


class TestRegistryShape:
    """The registry is the one place a retirement is recorded."""

    def test_every_entry_is_typed(self) -> None:
        assert RETIRED_SETTINGS
        assert all(isinstance(entry, RetiredSetting) for entry in RETIRED_SETTINGS)

    def test_every_entry_carries_a_reason(self) -> None:
        assert all(entry.reason.strip() for entry in RETIRED_SETTINGS)

    def test_no_retired_key_is_still_a_live_field(self) -> None:
        assert not (RENAMED_SETTING_KEYS.keys() | REMOVED_SETTING_KEYS) & _field_names()

    def test_every_replacement_is_a_live_field(self) -> None:
        assert set(RENAMED_SETTING_KEYS.values()) <= _field_names()

    def test_renamed_and_removed_partition_the_registry(self) -> None:
        assert not RENAMED_SETTING_KEYS.keys() & REMOVED_SETTING_KEYS
        assert RENAMED_SETTING_KEYS.keys() | REMOVED_SETTING_KEYS == {e.key for e in RETIRED_SETTINGS}

    def test_removed_setting_looks_up_only_removals(self) -> None:
        removed = next(iter(REMOVED_SETTING_KEYS))
        renamed = next(iter(RENAMED_SETTING_KEYS))
        assert removed_setting(removed) is not None
        assert removed_setting(renamed) is None
        assert removed_setting("openai_compatible_model") is None


class TestRenamedKeyMigrates(TestCase):
    """A row under a renamed key resolves onto its replacement field."""

    @pytest.fixture(autouse=True)
    def _no_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    def test_retired_speed_row_resolves_to_wip(self) -> None:
        ConfigSetting.objects.set_value("speed", "full")
        assert get_effective_settings().wip is Wip.FULL

    def test_retired_credential_entry_row_resolves_onto_the_generic_setting(self) -> None:
        ConfigSetting.objects.set_value("orca_router_pass_path", "provider/factory/api-key")
        assert get_effective_settings().openai_compatible_credential_entry == "provider/factory/api-key"


class TestRemovedKeyFailsLoud(TestCase):
    """A row under a removed key is reported LOUDLY, never silently reverted."""

    @pytest.fixture(autouse=True)
    def _no_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    @pytest.fixture(autouse=True)
    def _capsys(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._captured = capsys

    def test_stored_removed_key_warns_with_key_reason_and_remedy(self) -> None:
        key = next(iter(sorted(REMOVED_SETTING_KEYS)))
        ConfigSetting.objects.set_value(key, "anything")
        get_effective_settings()
        stderr = self._captured.readouterr().err
        assert key in stderr
        assert "config_setting clear" in stderr
        assert removed_setting(key).reason in stderr

    def test_stored_removed_key_still_resolves_settings(self) -> None:
        key = next(iter(sorted(REMOVED_SETTING_KEYS)))
        ConfigSetting.objects.set_value(key, "anything")
        assert isinstance(get_effective_settings(), UserSettings)

    def test_a_live_key_is_silent(self) -> None:
        ConfigSetting.objects.set_value("wip", "full")
        get_effective_settings()
        assert "config_setting clear" not in self._captured.readouterr().err

    def test_warn_removed_setting_names_key_reason_and_remedy(self) -> None:
        # The anti-silent-revert line, called directly: it must be named, reasoned,
        # and actionable in one message.
        entry = removed_setting(next(iter(sorted(REMOVED_SETTING_KEYS))))
        assert entry is not None
        warn_removed_setting(entry)
        stderr = self._captured.readouterr().err
        assert entry.key in stderr
        assert entry.reason in stderr
        assert "config_setting clear" in stderr
        assert "reverted to its default" in stderr


class TestIntakeRequireLabelRetirementIsRecorded(TestCase):
    """souliane/teatree#3862: the require-label narrowing gate's removal is recorded.

    souliane/teatree#3634 folded the two issue-intake scanners into one and replaced
    the per-overlay narrowing gate with the ``decide_intake`` admission table — the
    field and its reader went together, but the RETIREMENT was never recorded. So a
    stored row stayed invisible to the resolver and rendered on the operator's config
    surface as a live gate: diagnosing an intake stall, the row read ``True`` and
    implied a label was required, which the label-independent trusted-author rule
    makes false.
    """

    KEY = "issue_implementer_require_label"

    @pytest.fixture(autouse=True)
    def _no_overlay(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("T3_OVERLAY_NAME", raising=False)

    @pytest.fixture(autouse=True)
    def _capsys(self, capsys: pytest.CaptureFixture[str]) -> None:
        self._captured = capsys

    def test_the_key_is_recorded_as_a_removal_not_a_rename(self) -> None:
        assert self.KEY in REMOVED_SETTING_KEYS
        assert self.KEY not in RENAMED_SETTING_KEYS

    def test_the_reason_points_at_the_live_admission_table(self) -> None:
        entry = removed_setting(self.KEY)
        assert entry is not None
        assert "decide_intake" in entry.reason

    def test_a_stored_row_warns_instead_of_vanishing(self) -> None:
        ConfigSetting.objects.set_value(self.KEY, value=True)
        get_effective_settings()
        stderr = self._captured.readouterr().err
        assert self.KEY in stderr
        assert "config_setting clear" in stderr
