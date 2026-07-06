"""Unit tests for the canonical ``classify_ticket_kind`` SSOT (#17).

Every ticket-intake site routes its FEATURE/FIX decision through this one
function; these tests pin the precedence order and the deliberately-conservative
title inference (a mis-classified feature would wedge the fix-record DoD gate).
"""

import pytest

from teatree.core.intake.ticket_kind_classification import TicketOrigin, classify_ticket_kind, parse_kind
from teatree.core.models import Ticket


class TestClassifyTicketKind:
    def test_default_is_feature(self) -> None:
        assert classify_ticket_kind() == Ticket.Kind.FEATURE

    def test_plain_feature_title_is_feature(self) -> None:
        assert classify_ticket_kind(title="Add dark mode toggle") == Ticket.Kind.FEATURE

    def test_correction_origin_is_fix(self) -> None:
        assert classify_ticket_kind(origin=TicketOrigin.CORRECTION) == Ticket.Kind.FIX

    def test_bug_label_is_fix(self) -> None:
        assert classify_ticket_kind(labels=["type: bug"]) == Ticket.Kind.FIX

    def test_kind_slash_bug_label_is_fix(self) -> None:
        assert classify_ticket_kind(labels=["kind/bug"]) == Ticket.Kind.FIX

    def test_red_card_label_is_fix(self) -> None:
        # Hyphenated multi-word label collapses to its separator-stripped form.
        assert classify_ticket_kind(labels=["red-card"]) == Ticket.Kind.FIX

    def test_non_fix_labels_stay_feature(self) -> None:
        assert classify_ticket_kind(labels=["enhancement", "documentation"]) == Ticket.Kind.FEATURE

    def test_substring_lookalike_labels_are_not_fix(self) -> None:
        # Token-boundary matching: "debug" ⊃ "bug", "prefix"/"suffix" ⊃ "fix",
        # "defective" ⊃ "defect" — none may flip a feature to FIX.
        for label in ("debug", "prefix", "suffix", "defective", "debugging"):
            assert classify_ticket_kind(labels=[label]) == Ticket.Kind.FEATURE, label

    def test_conventional_fix_prefix_title_is_fix(self) -> None:
        assert classify_ticket_kind(title="fix: crash on empty password") == Ticket.Kind.FIX

    def test_hotfix_title_is_fix(self) -> None:
        assert classify_ticket_kind(title="hotfix login redirect loop") == Ticket.Kind.FIX

    def test_numbered_fix_branch_is_fix(self) -> None:
        # The ``<number>-<slug>`` branch shape resolve.py auto-registers: the
        # first non-numeric token drives the decision.
        assert classify_ticket_kind(title="123-fix-broken-export") == Ticket.Kind.FIX

    def test_slash_fix_branch_is_fix(self) -> None:
        assert classify_ticket_kind(title="fix/login-crash") == Ticket.Kind.FIX

    def test_fix_word_not_leading_stays_feature(self) -> None:
        # Conservative: "fix" buried mid-title must NOT flip a feature to FIX.
        assert classify_ticket_kind(title="Add a button to fix broken links") == Ticket.Kind.FEATURE

    def test_explicit_overrides_feature_inference(self) -> None:
        assert classify_ticket_kind(title="Add dark mode", explicit="fix") == Ticket.Kind.FIX

    def test_explicit_overrides_fix_inference(self) -> None:
        assert classify_ticket_kind(title="fix: crash", labels=["bug"], explicit="feature") == Ticket.Kind.FEATURE


class TestParseKind:
    def test_parses_fix(self) -> None:
        assert parse_kind("fix") == Ticket.Kind.FIX

    def test_parses_feature_case_insensitively(self) -> None:
        assert parse_kind("  Feature ") == Ticket.Kind.FEATURE

    def test_unknown_raises(self) -> None:
        with pytest.raises(ValueError, match="unknown ticket kind"):
            parse_kind("bugfix")
