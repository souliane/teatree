# test-path: cross-cutting
"""The divergence gate: a shipped default may differ from its in-code default ONLY if approved.

``defaults.toml`` is hand-editable and the resolver reads it, so an edit MOVES an
effective default. That must stay a reviewed decision rather than a side effect — which
is what the recorded approval in ``defaults_approvals.toml`` is. This suite drives the
gate from both directions on fixture files: an unrecorded divergence FAILS, the same
divergence with a matching approval PASSES, and a safety-posture / dark-flag key FAILS
either way.

The last test is the CI guard itself, run against the committed pair.
"""

from pathlib import Path

import pytest

from teatree.config import cold_defaults
from teatree.config.defaults_approvals import (
    APPROVALS_TOML,
    ApprovedDivergence,
    audit_shipped_defaults,
    read_approvals,
    render_approvals,
    resolved_shipped_value,
    shipped_divergences,
)
from teatree.config.defaults_snapshot import pinned_fail_closed_keys
from teatree.config.settings import UserSettings

_DIVERGENT_KEY = "session_stale_after_hours"
_SAFETY_KEY = "on_behalf_post_mode"
_DARK_KEY = "outer_loop_enabled"


@pytest.fixture
def shipped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Point the divergence reader at a fixture ``defaults.toml`` the test writes."""

    def write(body: str) -> Path:
        toml = tmp_path / "defaults.toml"
        toml.write_text(f"[teatree]\n{body}", encoding="utf-8")
        monkeypatch.setattr(cold_defaults, "DEFAULTS_TOML", toml)
        return toml

    return write


def _approval(key: str, value: object) -> ApprovedDivergence:
    return ApprovedDivergence(
        key=key,
        value=value,
        approver="U-owner",
        question_id=7,
        recorded_at="2026-07-27T09:00:00+00:00",
    )


class TestDivergenceDetection:
    def test_a_hand_edited_value_is_reported_as_a_divergence(self, shipped) -> None:
        shipped(f"{_DIVERGENT_KEY} = 24\n")
        code = getattr(UserSettings(), _DIVERGENT_KEY)
        assert code != 24
        divergences = shipped_divergences()
        assert list(divergences) == [_DIVERGENT_KEY]
        assert (divergences[_DIVERGENT_KEY].shipped, divergences[_DIVERGENT_KEY].in_code) == (24, code)

    def test_a_value_equal_to_the_in_code_default_is_no_divergence(self, shipped) -> None:
        shipped(f"{_DIVERGENT_KEY} = {getattr(UserSettings(), _DIVERGENT_KEY)}\n")
        assert shipped_divergences() == {}

    def test_a_structured_sub_table_resolves_through_its_own_parser(self, shipped) -> None:
        # `speak` is stored as a sub-table, so it resolves through `speak_from_subtable`
        # rather than `effective_default` — comparing the stored dict would never match the
        # dataclass's `SpeakConfig` and would report a permanent false divergence.
        shipped('[teatree.speak]\nlocal = "all"\n')
        code = UserSettings()
        assert resolved_shipped_value("speak", code).local == "all"
        assert resolved_shipped_value("speak", code) != code.speak

    def test_an_enum_valued_key_at_its_default_is_no_divergence(self, shipped) -> None:
        # The shipped form is a string; the dataclass default is a StrEnum. Comparing the
        # RESOLVED value (not the raw string) is what keeps this from a false divergence.
        shipped(f'{_SAFETY_KEY} = "draft_or_ask"\n')
        assert shipped_divergences() == {}


class TestGateBothDirections:
    def test_an_unapproved_divergence_fails_the_gate(self, shipped) -> None:
        shipped(f"{_DIVERGENT_KEY} = 24\n")
        audit = audit_shipped_defaults(approvals={})
        assert not audit.ok
        assert [d.key for d in audit.unapproved] == [_DIVERGENT_KEY]

    def test_the_same_divergence_passes_once_approved(self, shipped) -> None:
        shipped(f"{_DIVERGENT_KEY} = 24\n")
        audit = audit_shipped_defaults(approvals={_DIVERGENT_KEY: _approval(_DIVERGENT_KEY, 24)})
        assert audit.ok
        assert audit.unapproved == ()

    def test_an_approval_for_a_different_value_does_not_authorize_the_shipped_one(self, shipped) -> None:
        shipped(f"{_DIVERGENT_KEY} = 24\n")
        audit = audit_shipped_defaults(approvals={_DIVERGENT_KEY: _approval(_DIVERGENT_KEY, 36)})
        assert not audit.ok
        assert [d.key for d in audit.mismatched] == [_DIVERGENT_KEY]

    def test_an_approval_with_no_divergence_left_is_stale_and_fails(self, shipped) -> None:
        shipped(f"{_DIVERGENT_KEY} = {getattr(UserSettings(), _DIVERGENT_KEY)}\n")
        audit = audit_shipped_defaults(approvals={_DIVERGENT_KEY: _approval(_DIVERGENT_KEY, 24)})
        assert not audit.ok
        assert audit.stale == (_DIVERGENT_KEY,)


class TestSafetyPostureAndDarkFlagsCannotMoveEvenWithApproval:
    def test_the_two_exemplars_are_genuinely_pinned_keys(self) -> None:
        # Anti-vacuity: a flag that graduates out of DARK (or a key dropped from
        # SAFETY_POSTURE_KEYS) would make the cases below prove nothing about pinning.
        assert {_SAFETY_KEY, _DARK_KEY} <= pinned_fail_closed_keys()

    @pytest.mark.parametrize(
        ("key", "body"),
        [(_SAFETY_KEY, f'{_SAFETY_KEY} = "immediate"\n'), (_DARK_KEY, f"{_DARK_KEY} = true\n")],
    )
    def test_a_pinned_key_divergence_fails_even_when_approved(self, shipped, key: str, body: str) -> None:
        shipped(body)
        approved_value = shipped_divergences()[key].shipped
        audit = audit_shipped_defaults(approvals={key: _approval(key, approved_value)})
        assert not audit.ok
        assert [d.key for d in audit.forbidden] == [key]
        assert audit.unapproved == ()
        # Reported once, as FORBIDDEN — the entry is not doubled as a stale approval.
        assert audit.stale == ()


class TestLedgerRoundTrip:
    def test_render_then_read_reproduces_the_entries(self, tmp_path: Path) -> None:
        ledger = tmp_path / "defaults_approvals.toml"
        entries = {_DIVERGENT_KEY: _approval(_DIVERGENT_KEY, 24)}
        ledger.write_text(render_approvals(entries), encoding="utf-8")
        assert read_approvals(ledger) == entries

    def test_an_empty_ledger_reads_as_no_approvals(self, tmp_path: Path) -> None:
        ledger = tmp_path / "defaults_approvals.toml"
        ledger.write_text(render_approvals({}), encoding="utf-8")
        assert read_approvals(ledger) == {}

    def test_a_missing_ledger_reads_as_no_approvals(self, tmp_path: Path) -> None:
        assert read_approvals(tmp_path / "absent.toml") == {}

    def test_the_committed_ledger_is_a_fixed_point_of_the_renderer(self) -> None:
        # A re-render must be byte-stable, so an --apply run never churns the file with a
        # whitespace-only diff the end-of-file-fixer would then have to undo.
        assert render_approvals(read_approvals(APPROVALS_TOML)) == APPROVALS_TOML.read_text(encoding="utf-8")


def test_the_committed_defaults_file_carries_no_unrecorded_divergence() -> None:
    """THE CI guard: every shipped value either equals its in-code default or is approved.

    This replaces the two pins that forbade divergence outright. Divergence is now
    allowed — it just has to be a recorded, reviewed decision in
    ``defaults_approvals.toml``, and a safety-posture / dark-flag key can never diverge
    at all.
    """
    audit = audit_shipped_defaults()
    assert audit.ok, audit.report()


def test_the_committed_ledger_parses() -> None:
    assert isinstance(read_approvals(APPROVALS_TOML), dict)
