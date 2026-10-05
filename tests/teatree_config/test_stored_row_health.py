"""What a stored ``ConfigSetting`` row is, when no live setting owns it (#3862)."""

from importlib import import_module
from pathlib import Path

import pytest

from teatree.config import ALL_KNOWN_CONFIG_SETTINGS
from teatree.config.stored_row_health import (
    INTERNAL_STATE_KEYS,
    internal_state_key,
    is_operator_configuration,
    stored_row_kind,
    stored_row_note,
)


class TestStoredRowNote:
    """A key no live declaration owns never renders bare."""

    def test_every_live_key_is_silent(self) -> None:
        # Totality, not a sample: one live key gaining a note would put a spurious
        # "not in effect" beside a setting that IS in effect.
        noted = sorted(key for key in ALL_KNOWN_CONFIG_SETTINGS if stored_row_note(key))
        assert not noted

    def test_an_unknown_key_has_the_clear_remedy(self) -> None:
        key = "issue_implementer_require_label"
        note = stored_row_note(key)
        assert "unknown" in note
        assert "config_setting clear" in note
        assert key in note

    def test_an_unrecorded_key_is_still_marked(self) -> None:
        # A row with no declaration must not render as an ordinary setting.
        note = stored_row_note("a_key_no_registry_and_no_retirement_carries")
        assert "not a declared setting" in note
        assert "config_setting clear" in note

    def test_the_negative_bucket_claims_undeclaredness_not_absence_of_readers(self) -> None:
        # The classifier reads registries; "nothing reads it" is a claim about the call
        # graph it never consults, and #3867 printed it beside three keys with live
        # consumers. Only what a registry lookup can support may be said.
        assert "no live consumer" not in stored_row_note("a_key_no_registry_and_no_retirement_carries")


class TestIsOperatorConfiguration:
    """Which stored rows an interchange format may carry (#4147).

    The export writes a file the import reads back, and the import refuses the WHOLE file
    on a key it has no home for. So "may this row be written out" is exactly "will the
    reader accept it", and that is what this predicate answers.
    """

    def test_the_note_is_the_kind_in_brackets_so_one_classifier_serves_both_surfaces(self) -> None:
        # A row listing brackets it; the export report does not. Two spellings of the same
        # sentence is how the two would drift.
        kind = stored_row_kind("loop_preset_transition_stamp")
        assert kind
        assert not kind.startswith("[")
        assert stored_row_note("loop_preset_transition_stamp") == f"[{kind}]"

    def test_a_live_setting_is_configuration(self) -> None:
        assert is_operator_configuration(next(iter(sorted(ALL_KNOWN_CONFIG_SETTINGS))))

    @pytest.mark.parametrize(
        "key",
        ["host_projection_generation", "loop_preset_transition_stamp", "a_key_no_registry_and_no_retirement_carries"],
    )
    def test_internal_state_and_an_orphan_are_not(self, key: str) -> None:
        assert not is_operator_configuration(key)

    def test_a_former_setting_is_not_configuration(self) -> None:
        assert not is_operator_configuration("speed")


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
        assert internal_state_key("admit_colleague_prs_to_board") is None

    def test_no_registered_key_is_also_a_live_setting(self) -> None:
        registered = {entry.key for entry in INTERNAL_STATE_KEYS}
        assert not registered & ALL_KNOWN_CONFIG_SETTINGS.keys()

    @pytest.mark.parametrize(
        ("key", "owner"),
        [
            ("approval_dial", "teatree.core.models.approval_dial"),
            ("default_mode", "teatree.core.mode_resolution"),
        ],
    )
    def test_a_security_relevant_live_key_is_never_offered_the_clear_remedy(self, key: str, owner: str) -> None:
        # #3867 printed the destructive remedy beside both. Following it on `approval_dial`
        # un-graduates every approval class back to ASK; on `default_mode` it drops the
        # operator's posture back to the compiled fallback.
        note = stored_row_note(key)
        assert "config_setting clear" not in note
        assert "not a declared setting" not in note
        assert owner in note

    def test_the_projection_counter_is_state_a_publisher_ratchets_not_an_orphan(self) -> None:
        # It is written through raw sqlite3, so the classification conformance walk never
        # saw it and every raw-row surface offered `config_setting clear` for a live counter.
        note = stored_row_note("host_projection_generation")
        assert "teatree.config.host_projection" in note
        assert "config_setting clear" not in note

    def test_every_registered_key_still_appears_in_the_module_it_names(self) -> None:
        # The carve-out must not become the dead config it exists to prevent: an owner
        # that stops using its key would otherwise keep a permanent exemption.
        assert INTERNAL_STATE_KEYS
        for entry in INTERNAL_STATE_KEYS:
            source = Path(str(import_module(entry.owner).__file__)).read_text(encoding="utf-8")
            assert entry.key in source, f"{entry.owner} no longer carries {entry.key!r} — drop the registry entry"


class TestCredentialPassKeyRows:
    def test_a_pass_key_row_is_live_configuration_not_an_orphan(self) -> None:
        assert stored_row_note("notion_token_pass_key") == ""
