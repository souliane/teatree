"""Gate C: the `reviewing` phase needs T3_REVIEW_SKILL execution evidence.

The hole this closes: ``lifecycle visit-phase <id> reviewing`` can be
satisfied by an ad-hoc self-review. When a project configures a review skill
(``T3_REVIEW_SKILL`` / ``review_skill``), recording the ``reviewing``
attestation must require durable evidence the skill actually ran — a
``review_skill_run`` artifact on ``ticket.extra`` (skill name + ISO
timestamp). With no review skill configured the gate is a NO-OP (opt-in
default preserved).

The configured skill is pinned per test via ``configured_review_skill`` rather
than the host ``~/.teatree.toml``, so the suite is deterministic regardless of
the running machine's config.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates.review_skill_gate import configured_review_skill, recorded_review_skill
from teatree.core.management.commands.lifecycle import ReviewSkillEvidenceError
from teatree.core.models import Session, Ticket

pytestmark = pytest.mark.django_db


@contextmanager
def _configured_review_skill(skill: str) -> Iterator[None]:
    with patch("teatree.core.gates.review_skill_gate.configured_review_skill", return_value=skill):
        yield


class TestReviewingRequiresReviewSkillEvidence(TestCase):
    def _ticket_ready_for_review(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        return ticket

    def _visit_reviewing(self, ticket: Ticket) -> None:
        call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer")

    def test_refused_without_evidence_when_review_skill_configured(self) -> None:
        ticket = self._ticket_ready_for_review()
        with (
            _configured_review_skill("ac-reviewing-codebase"),
            pytest.raises(ReviewSkillEvidenceError, match="ac-reviewing-codebase"),
        ):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        session.refresh_from_db()
        assert "reviewing" not in (session.visited_phases or [])

    def test_allowed_with_evidence_present(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run("ac-reviewing-codebase")
        with _configured_review_skill("ac-reviewing-codebase"):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases

    def test_noop_when_review_skill_unset(self) -> None:
        ticket = self._ticket_ready_for_review()
        with _configured_review_skill(""):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases

    def test_evidence_for_a_different_skill_is_refused(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run("some-other-skill")
        with (
            _configured_review_skill("ac-reviewing-codebase"),
            pytest.raises(ReviewSkillEvidenceError, match="ac-reviewing-codebase"),
        ):
            self._visit_reviewing(ticket)


class TestRecordReviewSkillRun(TestCase):
    def test_stores_skill_name_and_iso_timestamp_on_extra(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        ticket.record_review_skill_run("ac-reviewing-codebase")
        ticket.refresh_from_db()
        run = ticket.extra["review_skill_run"]
        assert run["skill"] == "ac-reviewing-codebase"
        assert run["at"].endswith("+00:00") or run["at"].endswith("Z")

    def test_record_review_skill_run_command_stamps_extra(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        call_command("lifecycle", "record-review-skill-run", str(ticket.pk), "ac-reviewing-codebase")
        ticket.refresh_from_db()
        assert ticket.extra["review_skill_run"]["skill"] == "ac-reviewing-codebase"


class TestReviewSkillResolvers(TestCase):
    def test_configured_review_skill_reads_effective_settings(self) -> None:
        with patch(
            "teatree.core.gates.review_skill_gate.get_effective_settings",
            return_value=UserSettings(review_skill="  ac-reviewing-codebase  "),
        ):
            assert configured_review_skill() == "ac-reviewing-codebase"

    def test_recorded_review_skill_empty_without_evidence(self) -> None:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        assert recorded_review_skill(ticket) == ""
