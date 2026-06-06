import logging
from typing import ClassVar, cast

from django.db import models, transaction
from django.utils import timezone

from teatree.core.managers import SessionManager
from teatree.core.models.errors import QualityGateError
from teatree.core.models.ticket import Ticket
from teatree.core.phases import CANONICAL_PHASES, normalize_phase

logger = logging.getLogger(__name__)


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

    # #837: retro is an orchestrator-level periodic synthesis, NOT a
    # per-ticket sub-agent step. The shipping gate therefore no longer
    # requires a per-ticket ``retro`` visit (the bureaucracy that pushed
    # retro to the least-effective level). ``retro`` remains a recordable
    # phase for audit; it is simply no longer GATED here.
    _REQUIRED_PHASES: ClassVar[dict[str, list[str]]] = {
        "reviewing": ["testing"],
        "shipping": ["testing", "reviewing"],
        "requesting_review": ["shipping"],
    }

    # #782: ``_REQUIRED_PHASES`` is a second hand-maintained canonical-phase
    # vocabulary, divorced from ``phases.py``. Validate every key and required
    # phase against the canonical set at import so a typo or a future skill
    # spelling that drifts from ``phases.CANONICAL_PHASES`` fails fast here
    # instead of silently making the gate compare against a stale token.
    _GATE_PHASES: ClassVar[frozenset[str]] = frozenset(_REQUIRED_PHASES) | {
        phase for required in _REQUIRED_PHASES.values() for phase in required
    }
    if not _GATE_PHASES <= CANONICAL_PHASES:
        _drift = ", ".join(sorted(_GATE_PHASES - CANONICAL_PHASES))
        _msg = f"_REQUIRED_PHASES drifted from phases.CANONICAL_PHASES: {_drift}"
        raise ImportError(_msg)

    def __str__(self) -> str:
        return str(self.agent_id or f"session-{self.pk}")

    def recording_identity(self, explicit: str = "") -> str:
        """A guaranteed-non-empty attribution identity for a phase visit.

        ``phase_visits`` is an audit trail of who recorded each phase.
        ``visit_phase`` only stamps it when the identity is truthy, so a
        blank ``Session.agent_id`` (the ``Session.objects.create(ticket=…)``
        fallback / coordinator or non-FSM-minted sessions) would otherwise
        leave the audit record empty. Resolution order: explicit caller
        identity → the session's own ``agent_id`` → a deterministic
        non-empty per-session fallback. Never ``""``.
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

        ``phase`` is normalized to its canonical token **here, at the
        write boundary** (#782). The canonical-phase invariant
        ("``visited_phases`` holds canonical tokens, the spelling
        ``_REQUIRED_PHASES`` is keyed by") is owned by this method, not
        by caller convention. Before #782 it held only because
        ``lifecycle.py``/``task.py`` happened to ``normalize_phase``
        before calling; any other caller passing a raw ``review``
        silently corrupted the gate (``_check_phases`` then sees
        ``"reviewing" not in ["review"]`` and falsely blocks shipping —
        the #779 symptom, the structural root of the #759/#767/#770
        whack-a-mole). Enforcing it once at the boundary makes the
        invariant structural; callers no longer need to remember.
        """
        canonical = normalize_phase(phase)
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            visited = list(locked.visited_phases or [])
            visits = dict(locked.phase_visits or {})

            if canonical not in visited:
                visited.append(canonical)
            if agent_id and canonical not in visits:
                visits[canonical] = {
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
        self._check_phases(target_phase, self._visited_phases())

    def check_gate_across_ticket(self, target_phase: str) -> None:
        """Check the gate against the UNION of all the ticket's sessions.

        FSM-advancing ``visit-phase`` forks a fresh session by design, so
        the required phases for a ticket are legitimately scattered across
        its lifecycle sessions. The single source of truth is therefore
        the union, not the latest session. The gate verifies the required
        phases (``testing``/``reviewing``/``retro``) were recorded for the
        work; independence comes structurally from the ``reviewing`` phase
        being earned by a freshly-spawned cold-review sub-agent that has
        not seen the implementation — the spawn boundary is the guarantee,
        not an ``agent_id`` comparison.
        """
        visited, _visits = self.ticket.aggregate_phase_records()
        self._check_phases(target_phase, visited)

    def _check_phases(self, target_phase: str, visited: list[str]) -> None:
        """Gate ``target_phase`` against the required canonical phases.

        Both sides are normalized at this read boundary (#782):
        ``visited`` may carry legacy raw spellings (rows written before
        #782, or by a path that bypassed :meth:`visit_phase` such as
        ``merge.execution``); ``_REQUIRED_PHASES`` is keyed canonically.
        Normalizing membership here means a legacy ``review`` row still
        satisfies the canonical ``reviewing`` requirement instead of the
        gate falsely blocking shipping forever.
        """
        canonical_visited = {normalize_phase(phase) for phase in visited}
        canonical_target = normalize_phase(target_phase)
        missing = [phase for phase in self._REQUIRED_PHASES.get(canonical_target, []) if phase not in canonical_visited]
        if missing:
            joined = ", ".join(missing)
            msg = f"{target_phase} requires: {joined}"
            raise QualityGateError(msg)

    def mark_repo_modified(self, repo: str) -> None:
        self._append_repo("repos_modified", repo)

    def mark_repo_tested(self, repo: str) -> None:
        self._append_repo("repos_tested", repo)

    def _append_repo(self, field: str, repo: str) -> None:
        """Append ``repo`` to a JSON list column atomically.

        Mirrors the locked-RMW shape of :meth:`visit_phase`: re-read the row
        under ``select_for_update`` so a concurrent append on the same Session
        row is serialised by the DB lock rather than lost.
        """
        with transaction.atomic():
            locked = type(self).objects.select_for_update().get(pk=self.pk)
            repos = cast("list[str]", getattr(locked, field) or [])
            if repo in repos:
                return
            updated = [*repos, repo]
            type(self).objects.filter(pk=self.pk).update(**{field: updated})
            setattr(self, field, updated)

    def untested_repos(self) -> list[str]:
        modified = set(cast("list[str]", self.repos_modified or []))
        tested = set(cast("list[str]", self.repos_tested or []))
        return sorted(modified - tested)

    def begin_manual_handoff(self) -> None:
        self.ended_at = timezone.now()
        self.save(update_fields=["ended_at"])

    def _visited_phases(self) -> list[str]:
        return cast("list[str]", self.visited_phases or [])
