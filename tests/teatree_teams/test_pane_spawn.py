"""Maker-only pane consumer — SDK spawn helper + the maker claim path (#1838 PR#7b).

The LIVE-spawn layer that consumes PR#7a's safety machinery, staying DEFAULT-OFF
(``teams_enabled = false``) and maker-only. ``build_pane_options`` mirrors
``teatree.agents.headless._build_options`` exactly (preset-append
``system_prompt``, ``resolve_spawn_model`` floor merge, cwd / ``add_dirs``,
``resume = _get_resume_session_id(task)``) so a pane is a long-lived SDK session
that re-attaches across claims. The hard REVIEWER prohibition: the helper RAISES
for ``REVIEWER`` (or any ``team:reviewer`` slot) — reviewer panes are never
spawnable. ``claim_maker_pane`` lets a CORE_MAKER / OVERLAY_MAKER pane claim a
``team:<role>`` unit through ``assert_pane_claim_allowed`` (never t3-master) and
the ``live_owner_blocks_pane`` pre-work check, under the ``teams_max_panes`` cap,
using the role's overlay-seam claim filter. Nothing runs when the feature is off.
"""

import uuid
from dataclasses import replace
from unittest.mock import patch

import pytest
from django.test import TestCase

from teatree.config.settings import UserSettings
from teatree.core.loop_lease_manager import T3_MASTER_SLOT
from teatree.core.models import Session, Task, Ticket
from teatree.teams.guardrails import LoopOwnerCollisionError
from teatree.teams.pane_spawn import (
    PaneBudgetExceededError,
    ReviewerPaneProhibitedError,
    build_pane_options,
    claim_maker_pane,
)
from teatree.teams.panes import PaneState, TeammatePane
from teatree.teams.roles import TeamRole, team_claim_slot

_ENABLED = UserSettings(teams_enabled=True, teams_max_panes=1)


def _ticket(*, overlay: str = "") -> Ticket:
    return Ticket.objects.create(overlay=overlay, issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")


def _pending_task(*, overlay: str = "") -> Task:
    ticket = _ticket(overlay=overlay)
    session = Session.objects.create(ticket=ticket, agent_id="a")
    return Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)


class TestReviewerPaneProhibition(TestCase):
    """Reviewer panes are NEVER spawnable — a code-level raise (pinned by a fitness test)."""

    def test_build_pane_options_raises_for_reviewer_role(self) -> None:
        task = _pending_task()
        with pytest.raises(ReviewerPaneProhibitedError):
            build_pane_options(task, role=TeamRole.REVIEWER)

    def test_claim_maker_pane_raises_for_reviewer_role(self) -> None:
        with pytest.raises(ReviewerPaneProhibitedError):
            claim_maker_pane(role=TeamRole.REVIEWER, settings=_ENABLED, session_id="s1")


class TestBuildPaneOptions(TestCase):
    """The spawn helper mirrors ``_build_options`` exactly."""

    def test_options_mirror_build_options(self) -> None:
        task = _pending_task()
        with (
            patch("teatree.teams.pane_spawn._get_resume_session_id", return_value="resume-uuid") as resume,
            patch("teatree.teams.pane_spawn.resolve_spawn_model", return_value="opus") as model,
        ):
            options = build_pane_options(task, role=TeamRole.CORE_MAKER)

        # resume= threads the pane's resumable session so it re-attaches across claims.
        resume.assert_called_once_with(task)
        assert options.resume == "resume-uuid"
        # An at-or-above-opus resolved model is threaded through unchanged; the
        # team-mate floor is a no-op here (its raising behaviour is pinned below).
        assert options.model == "opus"
        assert model.called
        # APPEND to the claude_code preset, never REPLACE it. SystemPromptPreset
        # is a TypedDict (a dict at runtime); narrow off the str | None union
        # before subscripting, then assert the structural shape.
        preset = options.system_prompt
        assert isinstance(preset, dict)
        assert preset["type"] == "preset"
        assert preset["preset"] == "claude_code"
        assert preset.get("append")

    def test_resume_none_when_no_resumable_session(self) -> None:
        task = _pending_task()
        with patch("teatree.teams.pane_spawn._get_resume_session_id", return_value=""):
            options = build_pane_options(task, role=TeamRole.OVERLAY_MAKER)
        assert options.resume is None


class TestTeammateModelFloor(TestCase):
    """A maker pane is never spawned below opus — a sub-opus mate auto-compacts mid-task.

    ``build_pane_options`` runs the ``coding`` phase, which inherits the user's
    default and can be pinned to a cheap tier — either of which would put a
    long-lived team-mate pane on a model that auto-compacts mid-task and loses its
    working context. The opus floor closes both holes: an inherited (``None``) or
    below-opus resolution is raised to opus; an at-or-above-opus model is kept.
    """

    def _pane_model(self, resolved: str | None) -> str | None:
        task = _pending_task()
        with patch("teatree.teams.pane_spawn.resolve_spawn_model", return_value=resolved):
            return build_pane_options(task, role=TeamRole.CORE_MAKER).model

    def test_sonnet_resolution_is_floored_to_opus(self) -> None:
        assert self._pane_model("sonnet") == "opus"

    def test_haiku_resolution_is_floored_to_opus(self) -> None:
        assert self._pane_model("haiku") == "opus"

    def test_inherited_none_resolution_is_pinned_to_opus(self) -> None:
        # The inherit hole: tier_rank(None) == tier_rank("opus"), so a plain
        # most-capable-wins floor would leave None untouched and the pane would
        # inherit a possibly-sub-opus user default. The floor must pin opus.
        assert self._pane_model(None) == "opus"

    def test_empty_resolution_is_pinned_to_opus(self) -> None:
        # resolve_spawn_model can return "" (an inherit sentinel); the floor
        # treats it like None and pins opus, so the pane never spawns model-less.
        assert self._pane_model("") == "opus"

    def test_opus_resolution_passes_through(self) -> None:
        assert self._pane_model("opus") == "opus"

    def test_more_capable_model_is_not_downgraded(self) -> None:
        # Fable ranks above opus (the most-honest tier) and does not auto-compact;
        # the floor only raises, never lowers, so it is kept.
        assert self._pane_model("claude-fable-5") == "claude-fable-5"


class TestMakerClaimOverlaySeamRouting(TestCase):
    """CORE claims overlay=='' only, OVERLAY claims overlay!='' only — disjoint."""

    def test_core_maker_claims_only_core_units(self) -> None:
        core = _pending_task(overlay="")
        _pending_task(overlay="some-overlay")
        pane = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=_ENABLED, session_id="s1")
        assert pane is not None
        assert pane.claim_slot == team_claim_slot(TeamRole.CORE_MAKER)
        core.refresh_from_db()
        assert core.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)

    def test_overlay_maker_claims_only_overlay_units(self) -> None:
        _pending_task(overlay="")
        ov = _pending_task(overlay="some-overlay")
        pane = claim_maker_pane(role=TeamRole.OVERLAY_MAKER, settings=_ENABLED, session_id="s1")
        assert pane is not None
        ov.refresh_from_db()
        assert ov.claimed_by == team_claim_slot(TeamRole.OVERLAY_MAKER)

    def test_core_maker_does_not_claim_an_overlay_unit(self) -> None:
        _pending_task(overlay="some-overlay")
        pane = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=_ENABLED, session_id="s1")
        assert pane is None

    def test_overlay_maker_does_not_claim_a_core_unit(self) -> None:
        _pending_task(overlay="")
        pane = claim_maker_pane(role=TeamRole.OVERLAY_MAKER, settings=_ENABLED, session_id="s1")
        assert pane is None


class TestBudgetCap(TestCase):
    """Refuse to spawn pane N+1 above ``teams_max_panes``."""

    def test_refuses_pane_above_the_cap(self) -> None:
        _pending_task(overlay="")
        _pending_task(overlay="")
        first = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=_ENABLED, session_id="s1")
        assert first is not None
        with pytest.raises(PaneBudgetExceededError):
            claim_maker_pane(role=TeamRole.CORE_MAKER, settings=_ENABLED, session_id="s1")

    def test_cap_counts_only_live_team_claims(self) -> None:
        settings = replace(_ENABLED, teams_max_panes=2)
        for _ in range(3):
            _pending_task(overlay="")
        first = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=settings, session_id="s1")
        second = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=settings, session_id="s1")
        assert first is not None
        assert second is not None
        with pytest.raises(PaneBudgetExceededError):
            claim_maker_pane(role=TeamRole.CORE_MAKER, settings=settings, session_id="s1")


class TestNeverClaimsLoopOwner(TestCase):
    """A pane claims ONLY ``team:<role>`` — enforced via ``assert_pane_claim_allowed``."""

    def test_a_pane_can_never_claim_the_loop_owner_slot(self) -> None:
        # The guard is the boundary: a non-team slot raises before any claim.
        from teatree.teams.guardrails import assert_pane_claim_allowed  # noqa: PLC0415

        with pytest.raises(LoopOwnerCollisionError):
            assert_pane_claim_allowed(T3_MASTER_SLOT)

    def test_claimed_pane_slot_is_in_the_team_namespace(self) -> None:
        _pending_task(overlay="")
        pane = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=_ENABLED, session_id="s1")
        assert pane is not None
        assert pane.claim_slot.startswith("team:")
        assert pane.claim_slot != T3_MASTER_SLOT


class TestLiveForeignOwnerSkips(TestCase):
    """During ANOTHER session's live loop, the maker claim path SKIPS (no claim)."""

    def test_skips_when_a_live_foreign_owner_drives_the_loop(self) -> None:
        _pending_task(overlay="")
        with patch("teatree.teams.pane_spawn.live_owner_blocks_pane", return_value=True):
            pane = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=_ENABLED, session_id="s1")
        assert pane is None
        assert not Task.objects.filter(claimed_by__startswith="team:").exists()


class TestDefaultOff(TestCase):
    """``teams_enabled = false`` ⇒ no maker claim path runs, no pane spawns."""

    def test_disabled_settings_never_claim(self) -> None:
        _pending_task(overlay="")
        disabled = UserSettings(teams_enabled=False, teams_max_panes=1)
        pane = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=disabled, session_id="s1")
        assert pane is None
        assert not Task.objects.filter(claimed_by__startswith="team:").exists()

    def test_disabled_settings_skip_the_live_owner_check_entirely(self) -> None:
        _pending_task(overlay="")
        disabled = UserSettings(teams_enabled=False, teams_max_panes=1)
        with patch("teatree.teams.pane_spawn.live_owner_blocks_pane") as owner_check:
            claim_maker_pane(role=TeamRole.CORE_MAKER, settings=disabled, session_id="s1")
        owner_check.assert_not_called()


class TestClaimedPaneIsActive(TestCase):
    """A claimed maker pane reports the ACTIVE lifecycle state (PR#7a FSM)."""

    def test_claimed_pane_is_active(self) -> None:
        _pending_task(overlay="")
        pane = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=_ENABLED, session_id="s1")
        assert pane is not None
        assert isinstance(pane, TeammatePane)
        assert pane.refreshed_state() == PaneState.ACTIVE
