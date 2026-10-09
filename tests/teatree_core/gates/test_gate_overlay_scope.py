"""Review-skill gate resolves against the ticket overlay."""

from types import SimpleNamespace
from unittest.mock import patch

from teatree.core.gates import review_skill_gate
from teatree.core.gates.review_skill_gate import configured_review_skill, configured_review_skill_alternates


class TestReviewSkillGateThreadsTicketOverlay:
    """The seventh opt-in gate: ``review_skill`` resolves through the TICKET's overlay.

    Reading the AMBIENT settings let a DIFFERENT overlay's configuration decide
    whether the gate was armed — an overlay that never configured ``review_skill``
    disarmed it for a ticket whose own overlay did, and the reviewing attestation
    was recorded with no proof the deep review ran.
    """

    def test_accepted_skills_thread_the_explicit_overlay(self) -> None:
        captured: dict[str, object] = {}

        def _configured(overlay: str | None = None) -> str:
            captured["overlay"] = overlay
            return "deep-review"

        with (
            patch.object(review_skill_gate, "configured_review_skill", _configured),
            patch.object(review_skill_gate, "configured_review_skill_alternates", lambda _o=None: ()),
            patch.object(
                review_skill_gate,
                "get_effective_settings",
                lambda _o=None: SimpleNamespace(architectural_review_skill="arch-review"),
            ),
        ):
            assert review_skill_gate.accepted_per_pr_review_skills("acme-overlay") == frozenset({"deep-review"})
        assert captured["overlay"] == "acme-overlay"

    def test_the_settings_reader_receives_the_overlay(self) -> None:
        # The real readers, bound at import: the autouse conftest fixture replaces
        # the module attribute ``configured_review_skill`` for every other test.
        captured: list[str | None] = []

        def _settings(overlay: str | None = None) -> SimpleNamespace:
            captured.append(overlay)
            return SimpleNamespace(review_skill=" deep-review ", review_skill_alternates=[" alt "])

        with patch.object(review_skill_gate, "get_effective_settings", _settings):
            assert configured_review_skill("acme-overlay") == "deep-review"
            assert configured_review_skill_alternates("acme-overlay") == ("alt",)
        assert captured == ["acme-overlay", "acme-overlay"]

    def test_check_threads_the_tickets_overlay(self) -> None:
        ticket = SimpleNamespace(overlay="acme", pk=1, extra={})
        captured: dict[str, object] = {}

        def _accepted(overlay: str | None = None) -> frozenset[str]:
            captured["overlay"] = overlay
            return frozenset()

        with patch.object(review_skill_gate, "accepted_per_pr_review_skills", _accepted):
            review_skill_gate.check_review_skill_evidence(ticket)
        assert captured["overlay"] == "acme"

    def test_blank_ticket_overlay_becomes_none(self) -> None:
        ticket = SimpleNamespace(overlay="", pk=1, extra={})
        captured: dict[str, object] = {}

        def _accepted(overlay: str | None = None) -> frozenset[str]:
            captured["overlay"] = overlay
            return frozenset()

        with patch.object(review_skill_gate, "accepted_per_pr_review_skills", _accepted):
            review_skill_gate.check_review_skill_evidence(ticket)
        assert captured["overlay"] is None
