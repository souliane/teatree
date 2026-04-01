from typing import ClassVar, cast

from django.db import models
from django.utils import timezone

from teatree.core.managers import SessionManager
from teatree.core.models.errors import QualityGateError
from teatree.core.models.ticket import Ticket


class Session(models.Model):
    overlay = models.CharField(max_length=255)
    ticket = models.ForeignKey(Ticket, on_delete=models.CASCADE, related_name="sessions")
    visited_phases = models.JSONField(default=list, blank=True)
    started_at = models.DateTimeField(auto_now_add=True)
    ended_at = models.DateTimeField(null=True, blank=True)
    agent_id = models.CharField(max_length=255, blank=True)
    repos_modified = models.JSONField(default=list, blank=True)
    repos_tested = models.JSONField(default=list, blank=True)

    objects = SessionManager()

    _REQUIRED_PHASES: ClassVar[dict[str, list[str]]] = {
        "reviewing": ["testing"],
        "shipping": ["testing", "reviewing"],
        "requesting_review": ["shipping"],
    }

    def __str__(self) -> str:
        return str(self.agent_id or f"session-{self.pk}")

    def visit_phase(self, phase: str) -> None:
        visited_phases = self._visited_phases()
        if phase not in visited_phases:
            self.visited_phases = [*visited_phases, phase]
            self.save(update_fields=["visited_phases"])

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

    def _visited_phases(self) -> list[str]:
        return cast("list[str]", self.visited_phases or [])
