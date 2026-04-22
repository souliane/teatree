import logging
from typing import ClassVar, cast

from django.db import models
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

    def visit_phase(self, phase: str, *, agent_id: str = "") -> None:
        visited_phases = self._visited_phases()
        if phase not in visited_phases:
            self.visited_phases = [*visited_phases, phase]

        if agent_id:
            visits = self._phase_visits()
            if phase not in visits:
                visits[phase] = {
                    "agent_id": agent_id,
                    "timestamp": timezone.now().isoformat(),
                }
                self.phase_visits = visits

        self.save(update_fields=["visited_phases", "phase_visits"])

    def has_visited(self, phase: str) -> bool:
        return phase in self._visited_phases()

    def check_gate(self, target_phase: str, *, force: bool = False) -> None:
        if force:
            return

        visited_phases = self._visited_phases()
        missing = [phase for phase in self._REQUIRED_PHASES.get(target_phase, []) if phase not in visited_phases]
        if missing:
            joined = ", ".join(missing)
            msg = f"{target_phase} requires: {joined}"
            raise QualityGateError(msg)

        self._check_maker_checker(target_phase)

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

    def _check_maker_checker(self, _target_phase: str) -> None:
        visits = self._phase_visits()
        for phase_a, phase_b in _CONFLICTING_PHASE_PAIRS:
            if phase_a not in visits or phase_b not in visits:
                continue
            agent_a = visits[phase_a].get("agent_id", "")
            agent_b = visits[phase_b].get("agent_id", "")
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
