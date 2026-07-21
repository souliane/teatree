"""Maker-only pane consumer — the SDK spawn helper + the maker claim path (#1838 PR#7b).

The LIVE-spawn layer that CONSUMES PR#7a's safety machinery while staying
DEFAULT-OFF (``teams_enabled = false``) and maker-only. Two seams:

``build_pane_options`` is the SDK-portable spawn-options helper. A "pane" is a
long-lived, detached Claude SDK session keyed by its ``team:<role>`` claim,
reachable through the Agent-SDK ``ClaudeAgentOptions`` param surface — NEVER a
``claude -p`` CLI flag. It MIRRORS
:func:`teatree.agents.headless._build_options` field-for-field, reusing the same
shared primitives so the two paths can never drift: the appended ``claude_code``
preset system prompt (``SystemPromptPreset`` ``append``, never REPLACE), the
:func:`teatree.agents.model_tiering.resolve_spawn_model` tier + per-skill-floor
merge, the worktree ``cwd`` / ``add_dirs``
(:func:`teatree.agents.headless._resolve_task_cwd`), the shared
``_PERMISSION_MODE`` / ``_MAX_TURNS``, and
``resume = _get_resume_session_id(task)`` so a pane re-attaches its session
across claims. The HARD REVIEWER prohibition lives here: the helper RAISES for
the REVIEWER role (or any ``team:reviewer`` slot). Reviewer panes are never
spawnable — reviewing stays on the fresh-spawn-per-head_sha AutoReviewDispatch
path, untouched.

``claim_maker_pane`` is the maker claim path. When ``teams_enabled`` AND under
the ``teams_max_panes`` cap, a CORE_MAKER / OVERLAY_MAKER pane claims a
``team:<role>`` unit using its overlay-seam claim filter
(:attr:`TeamRole.task_claim_filter`). The claim goes through
:func:`teatree.teams.guardrails.assert_pane_claim_allowed` (a pane can never
claim t3-master) after the :func:`teatree.teams.guardrails.live_owner_blocks_pane`
pre-work check (SKIP during another session's live loop). DEFAULT-OFF: nothing
runs when ``teams_enabled`` is false.
"""

from claude_agent_sdk import ClaudeAgentOptions
from claude_agent_sdk.types import SystemPromptPreset

from teatree.agents._headless_options import _MAX_TURNS, _PERMISSION_MODE, _get_resume_session_id, _resolve_task_cwd
from teatree.agents.model_tiering import resolve_spawn_model
from teatree.agents.prompt import build_system_context, build_task_prompt
from teatree.agents.skill_bundle import resolve_skill_bundle
from teatree.config.settings import UserSettings
from teatree.core.cost import tier_rank
from teatree.core.models import Task
from teatree.core.models.ticket_worktree_checks import dispatch_worktree_path
from teatree.llm.credentials import reject_ambient_base_url_redirect
from teatree.skill_support.loading import SkillLoadingPolicy
from teatree.teams.guardrails import assert_pane_claim_allowed, live_owner_blocks_pane
from teatree.teams.panes import TeammatePane
from teatree.teams.roles import TEAM_CLAIM_PREFIX, TeamRole, team_claim_slot

#: The phase a maker pane runs (its long-lived session executes coding work).
#: Drives the per-phase model tier in :func:`resolve_spawn_model`; ``coding`` is
#: a judgment phase (absent from the tier map) so the pane inherits the user's
#: default model unless a per-skill floor raises it.
_MAKER_PANE_PHASE = "coding"

#: A maker pane runs a long-lived, multi-task session, so it MUST spawn on at
#: least the reasoning tier: a sub-opus mate auto-compacts mid-task and loses its
#: working context. This is the floor :func:`_floor_teammate_model` enforces.
_TEAMMATE_MODEL_FLOOR = "opus"


def _floor_teammate_model(model: str | None) -> str:
    """Raise an inherited / below-opus maker-pane spawn model up to the opus floor.

    :func:`resolve_spawn_model` returns ``None`` (or an empty inherit sentinel)
    when the ``coding`` phase inherits the user's default and no skill floor
    applies, and can resolve to sonnet/haiku under a cheap-tier phase pin — either
    of which would spawn a maker pane on a model that auto-compacts mid-task. A
    plain most-capable-wins merge would NOT close the inherit hole
    (``tier_rank(None) == tier_rank("opus")``), so this pins
    :data:`_TEAMMATE_MODEL_FLOOR` whenever the resolved model is absent or ranks
    below it, and leaves an at-or-above-opus model untouched — the floor only
    ever raises, never lowers.
    """
    if not model or tier_rank(model) < tier_rank(_TEAMMATE_MODEL_FLOOR):
        return _TEAMMATE_MODEL_FLOOR
    return model


class ReviewerPaneProhibitedError(RuntimeError):
    """A reviewer pane was requested — reviewer panes are never spawnable.

    Reviewing stays on the fresh-spawn-per-head_sha AutoReviewDispatch path; a
    long-lived REVIEWER pane would let one stale session re-review every head
    SHA, defeating the maker≠checker boundary. The prohibition is code-level
    (this raise) and pinned by a fitness test.
    """


class PaneBudgetExceededError(RuntimeError):
    """A maker pane was requested above the ``teams_max_panes`` cap."""


def _reject_reviewer(role: TeamRole) -> None:
    """Raise unless *role* is a spawnable maker role (the HARD REVIEWER prohibition).

    Checks BOTH the role identity AND its canonical ``team:reviewer`` slot, so a
    caller can never slip a reviewer pane through by passing a bare/qualified
    slug — the fully-qualified slot is the canonical key.
    """
    if role is TeamRole.REVIEWER or team_claim_slot(role) == team_claim_slot(TeamRole.REVIEWER):
        msg = (
            "Reviewer panes are never spawnable — reviewing stays on the "
            "fresh-spawn-per-head_sha AutoReviewDispatch path."
        )
        raise ReviewerPaneProhibitedError(msg)


def build_pane_options(task: Task, *, role: TeamRole) -> ClaudeAgentOptions:
    """Build the SDK ``ClaudeAgentOptions`` for a maker pane (#1838 PR#7b).

    The single spawn-options seam. RAISES :class:`ReviewerPaneProhibitedError`
    for the REVIEWER role before building anything. For a maker role it MIRRORS
    :func:`teatree.agents.headless._build_options` field-for-field — the appended
    ``claude_code`` preset, the floor-merged spawn model (raised to the opus
    team-mate floor by :func:`_floor_teammate_model`), the worktree ``cwd`` /
    ``add_dirs``, the shared permission mode / max-turns, and
    ``resume = _get_resume_session_id(task)`` so the pane re-attaches its session
    across claims.

    SDK-portable: every choice is a ``ClaudeAgentOptions`` param, never a
    ``claude -p`` CLI flag — a detached long-lived pane is expressed entirely
    through the Agent-SDK param surface.
    """
    _reject_reviewer(role)
    # A pane pins no credential, so its child inherits the ambient auth state AND an
    # ambient base-URL redirect. Refused here for the same reason the headless
    # ambient path refuses it: a long-lived autonomous maker is the last place a
    # silently redirected plan-authenticated session should be able to open.
    reject_ambient_base_url_redirect()
    skills = _resolve_pane_skills(task)
    system_context = _build_pane_system_context(task, skills=skills)
    cwd = _resolve_task_cwd(task)
    add_dirs = [cwd] if cwd else []
    resume_session_id = _get_resume_session_id(task)
    # ``Task.session`` is a non-null FK, so the agent-id read needs no guard (the
    # headless path's ``if task.session_id`` is defensive over a column that is
    # never null). The resume id wins when present; the session agent-id is the
    # escalation fallback that feeds resolve_spawn_model's honesty-escalation gate.
    escalation_session_id = resume_session_id or task.session.agent_id
    return ClaudeAgentOptions(
        # APPEND to the claude_code preset, never REPLACE it (the headless path's
        # rationale): a plain-str system_prompt would drop the preset.
        # ``exclude_dynamic_sections`` keeps the cached prefix stable across a
        # long-lived pane's turns in one worktree, where git status churns most.
        system_prompt=SystemPromptPreset(
            type="preset",
            preset="claude_code",
            append=system_context,
            exclude_dynamic_sections=True,
        ),
        model=_floor_teammate_model(
            resolve_spawn_model(
                _MAKER_PANE_PHASE,
                skills=skills,
                session_id=escalation_session_id or None,
                task_id=int(task.pk),
            )
        ),
        cwd=cwd,
        add_dirs=add_dirs,
        permission_mode=_PERMISSION_MODE,
        max_turns=_MAX_TURNS,
        resume=resume_session_id or None,
    )


def _resolve_pane_skills(task: Task) -> list[str]:
    """The skill bundle a maker pane loads — drives the per-skill model floor."""
    from teatree.core.overlay_loader import get_overlay_for_ticket  # noqa: PLC0415 — deferred: call-time import

    overlay = get_overlay_for_ticket(task.ticket)
    return resolve_skill_bundle(
        phase=_MAKER_PANE_PHASE,
        overlay_skill_metadata=overlay.metadata.get_skill_metadata(),
        worktree_path=dispatch_worktree_path(task.ticket),
    )


def _build_pane_system_context(task: Task, *, skills: list[str]) -> str:
    """Build the appended system context for a maker pane (mirrors the headless path)."""
    lifecycle_skill = SkillLoadingPolicy.lifecycle_for_phase(_MAKER_PANE_PHASE)
    return build_system_context(task, skills=skills, lifecycle_skill=lifecycle_skill)


def build_pane_prompt(task: Task, *, role: TeamRole) -> str:
    """Build the task prompt for a maker pane. RAISES for the REVIEWER role."""
    _reject_reviewer(role)
    return build_task_prompt(task, skills=_resolve_pane_skills(task))


def claim_maker_pane(
    *,
    role: TeamRole,
    settings: UserSettings,
    session_id: str,
    lease_seconds: int = 300,
) -> TeammatePane | None:
    """Claim a ``team:<role>`` unit for a maker pane, honouring every guard (#1838 PR#7b).

    The maker claim path. Returns the ACTIVE :class:`TeammatePane` on a successful
    claim, or ``None`` when nothing was claimed (feature off, a live foreign loop
    owner, or no candidate unit). It RAISES :class:`ReviewerPaneProhibitedError`
    for the REVIEWER role (no maker path), and :class:`PaneBudgetExceededError`
    when a live ``team:`` claim already fills the ``teams_max_panes`` cap (pane
    N+1 above the budget is refused).

    Order of checks (each is a SKIP-returning ``None``, not a raise, so a
    disabled/contended loop is a clean no-op). First ``teams_enabled`` off returns
    ``None`` (the live-owner check is never even run — DEFAULT-OFF is byte-identical
    to today). Then :func:`live_owner_blocks_pane` SKIPs during ANOTHER session's
    live loop. Then the ``teams_max_panes`` budget cap raises above the cap. Then
    :func:`assert_pane_claim_allowed` on the ``team:<role>`` slot fails closed for
    a non-team slot (a pane can never claim t3-master / infra). Finally
    ``claim_next_pending``'s CAS is narrowed by the role's overlay-seam filter.

    The claim heartbeats via the existing lease (``Task.renew_lease``) so a dead
    pane is recovered by ``reclaim_orphaned_claims`` / ``reap_stale_claims``.
    """
    _reject_reviewer(role)
    if not settings.teams_enabled:
        return None
    if live_owner_blocks_pane(pane_session_id=session_id):
        return None

    slot = team_claim_slot(role)
    if _live_team_claim_count() >= settings.teams_max_panes:
        msg = (
            f"Refusing to spawn pane above the teams_max_panes cap "
            f"({settings.teams_max_panes}): a live maker pane already fills the budget."
        )
        raise PaneBudgetExceededError(msg)

    assert_pane_claim_allowed(slot)
    task = Task.objects.claim_next_pending(
        claimed_by=slot,
        lease_seconds=lease_seconds,
        extra_filter=role.task_claim_filter,
    )
    if task is None:
        return None
    return TeammatePane(task, role=role)


def _live_team_claim_count() -> int:
    """Count live (CLAIMED, unexpired-lease) maker panes — the budget denominator."""
    from django.utils import timezone  # noqa: PLC0415 — deferred: Django import at call time

    return Task.objects.filter(
        status=Task.Status.CLAIMED,
        claimed_by__startswith=TEAM_CLAIM_PREFIX,
        lease_expires_at__gt=timezone.now(),
    ).count()


__all__ = [
    "PaneBudgetExceededError",
    "ReviewerPaneProhibitedError",
    "build_pane_options",
    "build_pane_prompt",
    "claim_maker_pane",
]
