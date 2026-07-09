"""Default-off zero-behaviour-change for the pane layer (#1838 PR#7b).

The whole feature ships DEFAULT-OFF: ``teams_enabled`` defaults False, so the
maker claim path and the pane reaper are no-ops, and the pane FSM / reaper /
guardrails are referenced by NOTHING in the live path EXCEPT the one sanctioned
consumer (the idle-pane reaper scanner the pane-reaper mini-loop dispatches —
itself gated ``None``-when-off). These tests pin the "zero behaviour change when
disabled" invariant at the seams a regression would surface: the config default,
the no-spawn-when-off behaviour, and the import graph (no UNSANCTIONED live-path
module reaches the pane modules).
"""

import ast
import uuid
from pathlib import Path

import pytest
from django.test import TestCase

import teatree
from teatree.config import load_config
from teatree.config.settings import UserSettings
from teatree.core.models import ConfigSetting, Session, Task, Ticket
from teatree.teams.pane_spawn import claim_maker_pane
from teatree.teams.panes import AgentTeamsDisabledError, PaneState, TeammatePane
from teatree.teams.roles import TeamRole, team_claim_slot

_SRC_ROOT = Path(teatree.__file__).resolve().parent

_LIVE_PATH_PACKAGES = ("loop", "loops", "agents")
_LIVE_PATH_MODULES = ("core/managers.py", "core/loop_lease_manager.py", "cli/loop.py")

# Every pane-layer module that must stay out of the live path except via the
# sanctioned consumer.
_PANE_MODULES = (
    "teatree.teams.panes",
    "teatree.teams.pane_reaper",
    "teatree.teams.pane_display",
    "teatree.teams.guardrails",
)
# The ONE sanctioned live-path consumer (#1838 PR#7b): the idle-pane reaper
# scanner the pane-reaper mini-loop dispatches. It may import a pane module
# (``pane_reaper``); nothing else in the live path may.
_SANCTIONED_PANE_CONSUMERS = ("loop/scanners/pane_reaper.py",)


def _live_path_files() -> list[Path]:
    files: list[Path] = []
    for pkg in _LIVE_PATH_PACKAGES:
        files.extend((_SRC_ROOT / pkg).rglob("*.py"))
    files.extend(_SRC_ROOT / rel for rel in _LIVE_PATH_MODULES)
    sanctioned = {_SRC_ROOT / rel for rel in _SANCTIONED_PANE_CONSUMERS}
    return [p for p in files if p not in sanctioned]


def _imports_any_pane_module(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(alias.name in _PANE_MODULES for alias in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module in _PANE_MODULES or any(module.startswith(f"{m}.") for m in _PANE_MODULES):
                return True
    return False


class TestPaneLayerDefaultsOff:
    def test_teams_enabled_defaults_off(self) -> None:
        assert load_config().user.teams_enabled is False

    def test_pane_budget_defaults_are_inert(self) -> None:
        settings = load_config().user
        # Conservative defaults: one pane, 30-minute idle threshold.
        assert settings.teams_max_panes == 1
        assert settings.teams_idle_minutes == 30

    def test_display_defaults_off(self) -> None:
        from teatree.config.enums import TeamsDisplay  # noqa: PLC0415

        # Default-OFF presentation: the in-process SDK path stands unchanged.
        assert load_config().user.teams_display is TeamsDisplay.NONE

    def test_no_unsanctioned_live_path_module_imports_a_pane_module(self) -> None:
        offenders = [str(p.relative_to(_SRC_ROOT)) for p in _live_path_files() if _imports_any_pane_module(p)]
        assert not offenders, f"an unsanctioned live-path module wires a pane module in: {offenders}"

    def test_the_scan_actually_covers_files(self) -> None:
        assert len(_live_path_files()) > 5


class TestNothingSpawnsWhenDisabled(TestCase):
    """The behavioural default-off invariant: nothing claims/spawns when off."""

    def _pending_task(self) -> Task:
        ticket = Ticket.objects.create(overlay="", issue_url=f"https://example.com/issues/{uuid.uuid4().hex}")
        session = Session.objects.create(ticket=ticket, agent_id="a")
        return Task.objects.create(ticket=ticket, session=session, status=Task.Status.PENDING)

    def test_maker_claim_path_is_a_no_op_when_disabled(self) -> None:
        self._pending_task()
        disabled = UserSettings(teams_enabled=False)
        pane = claim_maker_pane(role=TeamRole.CORE_MAKER, settings=disabled, session_id="s1")
        assert pane is None
        assert not Task.objects.filter(claimed_by__startswith="team:").exists()

    def test_pane_spawn_refuses_when_teams_disabled(self) -> None:
        # The low-level spawn seam must ENFORCE the OFF setting (fail-closed):
        # spawning a teammate while ``teams_enabled`` resolves to false raises and
        # claims NOTHING, so the setting is the single source of truth even when a
        # caller reaches the primitive directly (bypassing ``claim_maker_pane``).
        ConfigSetting.objects.set_value("teams_enabled", value=False)
        task = self._pending_task()
        with pytest.raises(AgentTeamsDisabledError):
            TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        task.refresh_from_db()
        assert task.status == Task.Status.PENDING
        assert task.claimed_by == ""
        assert not Task.objects.filter(claimed_by__startswith="team:").exists()

    def test_pane_spawn_is_allowed_when_teams_enabled(self) -> None:
        # The must-spawn side: with the setting ON, the same seam claims the
        # ``team:<role>`` slot and returns the ACTIVE pane.
        ConfigSetting.objects.set_value("teams_enabled", value=True)
        task = self._pending_task()
        pane = TeammatePane.spawn(task, role=TeamRole.CORE_MAKER)
        assert pane.state == PaneState.ACTIVE
        task.refresh_from_db()
        assert task.status == Task.Status.CLAIMED
        assert task.claimed_by == team_claim_slot(TeamRole.CORE_MAKER)
