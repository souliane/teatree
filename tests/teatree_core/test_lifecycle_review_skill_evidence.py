"""Gate C: the `reviewing` phase needs T3_REVIEW_SKILL execution evidence.

The hole this closes: ``lifecycle visit-phase <id> reviewing`` can be
satisfied by an ad-hoc self-review. When a project configures a review skill
(``T3_REVIEW_SKILL`` / ``review_skill``), recording the ``reviewing``
attestation must require durable evidence the skill actually ran — a
``review_skill_run`` artifact on ``ticket.extra`` (skill name + ISO
timestamp). With no review skill configured the gate is a NO-OP (opt-in
default preserved).

The configured skill is pinned per test via ``configured_review_skill`` rather
than the host machine's config, so the suite is deterministic regardless of it.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from unittest.mock import patch

import pytest
from django.core.management import call_command
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.gates.review_skill_gate import (
    PER_PR_REVIEW_SKILL,
    accepted_per_pr_review_skills,
    configured_review_skill,
    configured_review_skill_alternates,
    recorded_review_skill,
)
from teatree.core.management.commands.lifecycle import ReviewSkillEvidenceError
from teatree.core.models import Session, Ticket


@contextmanager
def _configured_review_skill(skill: str, *alternates: str) -> Iterator[None]:
    with (
        patch("teatree.core.gates.review_skill_gate.configured_review_skill", return_value=skill),
        patch(
            "teatree.core.gates.review_skill_gate.configured_review_skill_alternates",
            return_value=alternates,
        ),
    ):
        yield


@contextmanager
def _repo_is_overlay_own(*, is_own: bool) -> Iterator[None]:
    with patch("teatree.core.gates.review_skill_gate.ticket_repo_is_overlay_own", return_value=is_own):
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
            _configured_review_skill("custom-per-pr-review"),
            pytest.raises(ReviewSkillEvidenceError, match="custom-per-pr-review"),
        ):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        session.refresh_from_db()
        assert "reviewing" not in (session.visited_phases or [])

    def test_allowed_with_evidence_present(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run("custom-per-pr-review")
        with _configured_review_skill("custom-per-pr-review"):
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
            _configured_review_skill("custom-per-pr-review"),
            pytest.raises(ReviewSkillEvidenceError, match="custom-per-pr-review"),
        ):
            self._visit_reviewing(ticket)


class TestAlternateReviewSkillsSatisfyTheGate(TestCase):
    """A configured alternate is a SUBSTITUTE reviewer, never a bypass.

    An overlay that runs one deep-review skill by default but accepts a
    second (a different model's reviewer) must be able to record either and
    pass. What stays refused is a run of NEITHER — the gate keeps its teeth.
    """

    def _ticket_ready_for_review(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        return ticket

    def _visit_reviewing(self, ticket: Ticket) -> None:
        call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer")

    def test_evidence_for_an_alternate_skill_passes(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run("codex-review")
        with _configured_review_skill("elite-review", "codex-review"):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases

    def test_evidence_for_the_primary_still_passes(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run("elite-review")
        with _configured_review_skill("elite-review", "codex-review"):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases

    def test_an_unconfigured_skill_is_still_refused(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run("some-other-skill")
        with (
            _configured_review_skill("elite-review", "codex-review"),
            pytest.raises(ReviewSkillEvidenceError) as raised,
        ):
            self._visit_reviewing(ticket)
        assert "elite-review" in str(raised.value)
        assert "codex-review" in str(raised.value)

    def test_alternates_alone_never_arm_the_gate(self) -> None:
        ticket = self._ticket_ready_for_review()
        with _configured_review_skill("", "codex-review"):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases


class TestAcceptedReviewSkillResolution(TestCase):
    def test_alternates_join_the_configured_primary(self) -> None:
        with _configured_review_skill("elite-review", "codex-review"):
            assert accepted_per_pr_review_skills() == frozenset({"elite-review", "codex-review"})

    def test_no_configured_skill_accepts_nothing(self) -> None:
        with _configured_review_skill("", "codex-review"):
            assert accepted_per_pr_review_skills() == frozenset()

    def test_alternates_are_read_from_effective_settings_stripped_and_deduped(self) -> None:
        with patch(
            "teatree.core.gates.review_skill_gate.get_effective_settings",
            return_value=UserSettings(review_skill_alternates=[" codex-review ", "codex-review", "  "]),
        ):
            assert configured_review_skill_alternates() == ("codex-review",)


class TestReviewSkillGateRepoScoping(TestCase):
    """A ticket reached only through the overlay's broader workspace-repo routing is exempt (#2895).

    ``ticket_repo_is_overlay_own`` itself is exercised end-to-end (real
    overlay + constructed ``issue_url``) in ``tests/test_overlay_loader.py``;
    here it's patched directly so this file keeps testing only the gate's
    wiring, matching ``_configured_review_skill``'s style above.
    """

    def _ticket_ready_for_review(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        return ticket

    def _visit_reviewing(self, ticket: Ticket) -> None:
        call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer")

    def test_routed_through_ticket_skips_evidence_requirement(self) -> None:
        ticket = self._ticket_ready_for_review()
        with _configured_review_skill("custom-per-pr-review"), _repo_is_overlay_own(is_own=False):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases

    def test_own_repo_ticket_still_requires_evidence(self) -> None:
        """Regression guard: the narrowing must not leak into genuine same-repo tickets."""
        ticket = self._ticket_ready_for_review()
        with (
            _configured_review_skill("custom-per-pr-review"),
            _repo_is_overlay_own(is_own=True),
            pytest.raises(ReviewSkillEvidenceError, match="custom-per-pr-review"),
        ):
            self._visit_reviewing(ticket)
        session = ticket.sessions.first()
        assert session is not None
        session.refresh_from_db()
        assert "reviewing" not in (session.visited_phases or [])


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


class TestPerPrReviewTierScoping(TestCase):
    """souliane/teatree#3530 — a per-PR ship is not accountable for the periodic sweep."""

    @contextmanager
    def _tiers(self, *, review: str, architectural: str = "ac-reviewing-codebase") -> Iterator[None]:
        with (
            _configured_review_skill(review),
            patch(
                "teatree.core.gates.review_skill_gate.get_effective_settings",
                return_value=UserSettings(architectural_review_skill=architectural),
            ),
        ):
            yield

    def _ticket_ready_for_review(self) -> Ticket:
        ticket = Ticket.objects.create(overlay="t3-teatree", state=Ticket.State.TESTED)
        Session.objects.create(ticket=ticket, agent_id="maker:coding")
        return ticket

    def test_periodic_architectural_skill_resolves_to_the_per_pr_tier(self) -> None:
        with self._tiers(review="ac-reviewing-codebase"):
            assert accepted_per_pr_review_skills() == frozenset({PER_PR_REVIEW_SKILL})

    def test_a_distinct_per_pr_skill_is_left_alone(self) -> None:
        with self._tiers(review="custom-per-pr-review"):
            assert accepted_per_pr_review_skills() == frozenset({"custom-per-pr-review"})

    def test_unset_review_skill_stays_a_noop(self) -> None:
        with self._tiers(review=""):
            assert accepted_per_pr_review_skills() == frozenset()

    def test_gate_still_blocks_without_per_pr_tier_evidence(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run("ac-reviewing-codebase")
        with (
            self._tiers(review="ac-reviewing-codebase"),
            _repo_is_overlay_own(is_own=True),
            pytest.raises(ReviewSkillEvidenceError, match=PER_PR_REVIEW_SKILL),
        ):
            call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer")

    def test_gate_passes_on_per_pr_tier_evidence(self) -> None:
        ticket = self._ticket_ready_for_review()
        ticket.record_review_skill_run(PER_PR_REVIEW_SKILL)
        with self._tiers(review="ac-reviewing-codebase"):
            call_command("lifecycle", "visit-phase", str(ticket.pk), "reviewing", agent_id="cold-reviewer")
        session = ticket.sessions.first()
        assert session is not None
        assert "reviewing" in session.visited_phases
