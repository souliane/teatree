import logging
from typing import ClassVar, cast

from django.db import models, transaction
from django.utils import timezone

from teatree.core.managers import SessionManager
from teatree.core.models.errors import QualityGateError
from teatree.core.models.ticket import Ticket

logger = logging.getLogger(__name__)

# Phases where maker≠checker is enforced: the agent that coded
# must not be the same agent that reviews.
_CONFLICTING_PHASE_PAIRS: list[tuple[str, str]] = [
    ("coding", "reviewing"),
]


class Session(models.Model):
    overlay = models.CharField(max_length=255)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="sessions")
    visited_phases = models.JSONField(default=list, blank=True)
    phase_visits = models.JSONField(default=dict, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    agent_id = models.CharField(max_length=255, blank=True)
    repos_modified = models.JSONField(default=list, blank=True)
    repos_tested = models.JSONField(default=list, blank=True)

    objects = SessionManager()

    class Meta:
        db_table = "teatree_session"

    _REQUIRED_PHASES: ClassVar[dict[str, list[str]]] = {
        "reviewing": ["testing"],
        "shipping": ["testing", "reviewing", "retro"],
        "requesting_review": ["shipping"],
    }

    def __str__(self) -> str:
        return str(self.agent_id or f"session-{self.pk}")

    def recording_identity(self, explicit: str = "") -> str:
        """A guaranteed-non-empty attribution identity for a phase visit.

        #755: ``visit_phase`` only stamps ``phase_visits`` when the
        identity is truthy, so a blank ``Session.agent_id`` (the
        ``Session.objects.create(ticket=…)`` fallback / coordinator or
        non-FSM-minted sessions) silently dropped maker attribution and
        made ``_check_maker_checker`` unverifiable. Resolution order:
        explicit caller identity → the session's own ``agent_id`` →
        a deterministic non-empty per-session fallback. Never ``""``.
        """
        return explicit.strip() or self.agent_id.strip() or f"session-{self.pk}"

    def visit_phase(self, phase: str, *, agent_id: str = "") -> None:
        """Record a phase visit atomically (#755).

        The read-modify-write of the ``visited_phases`` / ``phase_visits``
        JSON columns is wrapped in ``transaction.atomic()`` with the row
        ``select_for_update``-locked and **re-read from the locked row**
        (not the possibly-stale in-memory instance). A concurrent writer
        on the same ``Session`` pk — the live maker ``loop`` session and
        an independent reviewer both recording on the same row — would
        otherwise lose-update: each saved its own stale view and the last
        writer clobbered the other's phase (the #748 / `/t3:review`
        Safety-6 unlocked read-modify-write class). The lock serialises
        the two writers so both phases survive.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            visited = list(locked.visited_phases or [])
            visits = dict(locked.phase_visits or {})

            if phase not in visited:
                visited.append(phase)
            if agent_id and phase not in visits:
                visits[phase] = {
                    "agent_id": agent_id,
                    "timestamp": timezone.now().isoformat(),
                }

            self.visited_phases = visited
            self.phase_visits = visits
            type(self).objects.filter(pk=self.pk).update(
                visited_phases=visited,
                phase_visits=visits,
            )

    def has_visited(self, phase: str) -> bool:
        return phase in self._visited_phases()

    def check_gate(self, target_phase: str, *, force: bool = False) -> None:
        """Check this session's own phase records against the gate."""
        if force:
            return
        self._check_phases(target_phase, self._visited_phases(), self._phase_visits())

    def check_gate_across_ticket(self, target_phase: str) -> None:
        """Check the gate against the UNION of all the ticket's sessions.

        FSM-advancing ``visit-phase`` forks a fresh session by design
        (bias-free maker≠checker), so the required phases for a ticket are
        legitimately scattered across its lifecycle sessions. The single
        source of truth is therefore the union, not the latest session.
        ``_check_maker_checker`` runs over the merged ``phase_visits`` so
        a same-agent conflicting pair is still caught even when the two
        phases were recorded on different sessions — integrity preserved.
        """
        visited, visits = self.ticket.aggregate_phase_records()
        self._check_phases(target_phase, visited, visits)

    def _check_phases(self, target_phase: str, visited: list[str], visits: dict[str, dict[str, str]]) -> None:
        missing = [phase for phase in self._REQUIRED_PHASES.get(target_phase, []) if phase not in visited]
        if missing:
            joined = ", ".join(missing)
            msg = f"{target_phase} requires: {joined}"
            raise QualityGateError(msg)

        self._check_maker_checker(visited, visits)

    def mark_repo_modified(self, repo: str) -> None:
        repos = cast("list[str]", self.repos_modified or [])
        if repo not in repos:
            self.repos_modified = [*repos, repo]
            self.save(update_fields=["repos_modified"])

    def mark_repo_tested(self, repo: str) -> None:
        repos = cast("list[str]", self.repos_tested or [])
        if repo not in repos:
            self.repos_tested = [*repos, repo]
            self.save(update_fields=["repos_tested"])

    def untested_repos(self) -> list[str]:
        modified = set(cast("list[str]", self.repos_modified or []))
        tested = set(cast("list[str]", self.repos_tested or []))
        return sorted(modified - tested)

    def begin_manual_handoff(self) -> None:
        self.ended_at = timezone.now()
        self.save(update_fields=["ended_at"])

    @staticmethod
    def _check_maker_checker(visited: list[str], visits: dict[str, dict[str, str]]) -> None:
        """Enforce maker≠checker, FAIL-CLOSED on absent attribution (#755).

        Pre-#755 this ``continue``-d past any pair lacking ``phase_visits``
        entries, so a blank-``agent_id`` recording path (the
        ``Session.objects.create(ticket=…)`` fallback / coordinator
        sessions) made the gate vacuously pass — the safety check could
        not observe the attribution it must enforce. Now: if BOTH
        conflicting phases are claimed done (in ``visited``) but either
        lacks a non-empty recorded identity in ``visits``, the gate
        REFUSES — work asserted complete with unverifiable maker
        attribution must not pass automated review.
        """
        for phase_a, phase_b in _CONFLICTING_PHASE_PAIRS:
            both_claimed_done = phase_a in visited and phase_b in visited
            agent_a = visits.get(phase_a, {}).get("agent_id", "").strip()
            agent_b = visits.get(phase_b, {}).get("agent_id", "").strip()
            if both_claimed_done and not (agent_a and agent_b):
                msg = (
                    f"Maker≠checker unverifiable: {phase_a} and {phase_b} are both "
                    f"recorded as visited but lack per-phase agent attribution "
                    f"(agent_id missing/blank). The gate fails closed — re-record "
                    f"the phases with an explicit recording identity."
                )
                raise QualityGateError(msg)
            if agent_a and agent_b and agent_a == agent_b:
                msg = (
                    f"Maker≠checker violation: {phase_a} and {phase_b} "
                    f"were both visited by the same agent ({agent_a}). "
                    f"A different agent must perform {phase_b}."
                )
                raise QualityGateError(msg)

    def _visited_phases(self) -> list[str]:
        return cast("list[str]", self.visited_phases or [])

    def _phase_visits(self) -> dict[str, dict[str, str]]:
        return cast("dict[str, dict[str, str]]", self.phase_visits or {})
