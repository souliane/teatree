"""What a stored ``ConfigSetting`` row is, when no live setting owns it (#3862)."""

from importlib import import_module
from pathlib import Path

import pytest

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.retired_settings import REMOVED_SETTING_KEYS, RENAMED_SETTING_KEYS
from teatree.config.stored_row_health import INTERNAL_STATE_KEYS, internal_state_key, stored_row_note


class TestStoredRowNote:
    """A key no live declaration owns never renders bare."""

    def test_every_live_key_is_silent(self) -> None:
        # Totality, not a sample: one live key gaining a note would put a spurious
        # "not in effect" beside a setting that IS in effect.
        noted = sorted(key for key in ALL_KNOWN_CONFIG_SETTINGS if stored_row_note(key))
        assert not noted

    def test_a_removed_key_is_named_dead_with_the_clear_remedy(self) -> None:
        key = next(iter(sorted(REMOVED_SETTING_KEYS)))
        note = stored_row_note(key)
        assert "retired" in note
        assert "config_setting clear" in note
        assert key in note

    def test_a_renamed_alias_says_where_its_value_goes_not_that_it_is_dead(self) -> None:
        # A renamed key's stored value still resolves — onto the replacement field —
        # so marking it "not in effect" would be a lie the operator acts on.
        key = next(iter(sorted(RENAMED_SETTING_KEYS)))
        note = stored_row_note(key)
        assert RENAMED_SETTING_KEYS[key] in note
        assert "not in effect" not in note

    def test_an_unrecorded_key_is_still_marked(self) -> None:
        # The class fix, not the one-key fix: a removal nobody recorded in
        # RETIRED_SETTINGS must not render as an ordinary setting either.
        note = stored_row_note("a_key_no_registry_and_no_retirement_carries")
        assert "not a declared setting" in note
        assert "config_setting clear" in note

    def test_the_negative_bucket_claims_undeclaredness_not_absence_of_readers(self) -> None:
        # The classifier reads registries; "nothing reads it" is a claim about the call
        # graph it never consults, and #3867 printed it beside three keys with live
        # consumers. Only what a registry lookup can support may be said.
        assert "no live consumer" not in stored_row_note("a_key_no_registry_and_no_retirement_carries")

    def test_the_retired_intake_gate_lands_in_the_retired_bucket_not_the_unknown_one(self) -> None:
        # Asserting only that the note is non-empty passes for EVERY bucket, so it
        # never noticed a key classified as unknown-orphan rather than retired.
        note = stored_row_note("issue_implementer_require_label")
        assert "retired — not in effect" in note
        assert "config_setting clear issue_implementer_require_label" in note
        assert "not a declared setting" not in note


class TestInternalStateKeysAreNotCalledDead:
    """A deliberate non-setting row has an owner, not a corpse.

    A ``ConfigSetting`` row is not always a setting: ``loop_preset_transition_stamp``
    is runtime state ``teatree.loops.preset_transitions`` rewrites every pass, absent
    from the key registries BY DESIGN. Telling the operator to clear it is worse than
    silence — the next pass reads the missing stamp as a mode switch and posts a
    spurious Slack line. So the marker needs this third bucket, or the fix trades one
    misleading surface for another.
    """

    STAMP = "loop_preset_transition_stamp"

    def test_the_stamp_row_is_not_reported_dead_nor_offered_the_clear_remedy(self) -> None:
        note = stored_row_note(self.STAMP)
        assert "not a declared setting" not in note
        assert "config_setting clear" not in note

    def test_the_stamp_row_names_the_module_that_owns_it(self) -> None:
        assert "teatree.loops.preset_transitions" in stored_row_note(self.STAMP)

    def test_lookup_returns_none_for_a_key_that_is_not_state(self) -> None:
        assert internal_state_key(self.STAMP) is not None
        assert internal_state_key("issue_implementer_enabled") is None

    def test_no_registered_key_is_also_a_live_setting(self) -> None:
        registered = {entry.key for entry in INTERNAL_STATE_KEYS}
        assert not registered & ALL_KNOWN_CONFIG_SETTINGS.keys()

    @pytest.mark.parametrize(
        ("key", "owner"),
        [
            ("approval_dial", "teatree.core.models.approval_dial"),
            ("default_mode", "teatree.core.mode_resolution"),
            ("presence_upgrade_mode", "teatree.core.mode_resolution"),
        ],
    )
    def test_a_security_relevant_live_key_is_never_offered_the_clear_remedy(self, key: str, owner: str) -> None:
        # #3867 printed the destructive remedy beside all three. Following it on
        # `approval_dial` un-graduates every approval class back to ASK; on the two mode
        # keys it drops the operator's ladder back to the compiled fallback.
        note = stored_row_note(key)
        assert "config_setting clear" not in note
        assert "not a declared setting" not in note
        assert owner in note

    def test_every_registered_key_still_appears_in_the_module_it_names(self) -> None:
        # The carve-out must not become the dead config it exists to prevent: an owner
        # that stops using its key would otherwise keep a permanent exemption.
        assert INTERNAL_STATE_KEYS
        for entry in INTERNAL_STATE_KEYS:
            source = Path(str(import_module(entry.owner).__file__)).read_text(encoding="utf-8")
            assert entry.key in source, f"{entry.owner} no longer carries {entry.key!r} — drop the registry entry"
