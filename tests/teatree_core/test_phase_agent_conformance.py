"""Golden phase→agent conformance for the per-phase loop dispatch.

The anti-regression guard for the per-phase FSM dispatch that #559/#633
shadowed: every author lifecycle phase must route to its OWN phase agent,
never to a single chaining orchestrator. The table is the contract — adding
``planning → t3:planner`` later is a one-line edit here and in
``SUBAGENT_BY_PHASE``.
"""

import json
from io import StringIO
from pathlib import Path

from django.core.management import call_command
from django.test import TestCase

from teatree.core.management.commands import loop_dispatch as loop_dispatch_cmd
from teatree.core.models import Session, Task, Ticket
from teatree.core.phases import CHAINING_ORCHESTRATOR, SUBAGENT_BY_PHASE, subagent_for_phase
from teatree.loop.dispatch import dispatch
from teatree.loop.scanners.base import ScanSignal

#: The golden table: every author lifecycle phase → its dedicated agent.
#: A new lifecycle phase is added with one row (e.g. ``"planning": "t3:planner"``).
EXPECTED_AUTHOR_AGENT: dict[str, str] = {
    "coding": "t3:coder",
    "testing": "t3:tester",
    "reviewing": "t3:reviewer",
    "shipping": "t3:shipper",
}

#: Repo-root ``agents/`` directory — the canonical sub-agent definitions the
#: ``Agent`` tool resolves a ``t3:<name>`` value against. ``plugins/t3/agents``
#: is the same directory reached through the ``plugins/t3 -> ..`` setup symlink.
AGENTS_DIR: Path = Path(__file__).resolve().parents[2] / "agents"


def _agent_name(subagent: str) -> str:
    """Strip the ``t3:`` namespace prefix from a ``SUBAGENT_BY_PHASE`` value.

    Every value is namespaced (asserted by
    ``test_every_mapped_subagent_uses_the_t3_namespace``); ``removeprefix``
    is a no-op for any future un-namespaced value rather than raising.
    """
    return subagent.removeprefix("t3:")


class TestSubagentForPhaseConformance(TestCase):
    """The canonical ``subagent_for_phase`` map — the single source of truth."""

    def test_every_author_phase_routes_to_its_own_agent(self) -> None:
        for phase, expected in EXPECTED_AUTHOR_AGENT.items():
            assert subagent_for_phase(Ticket.Role.AUTHOR, phase) == expected, (
                f"author phase {phase!r} routed to {subagent_for_phase(Ticket.Role.AUTHOR, phase)!r}, "
                f"expected {expected!r}"
            )

    def test_no_author_phase_routes_to_chaining_orchestrator(self) -> None:
        offenders = {
            phase: agent
            for (role, phase), agent in SUBAGENT_BY_PHASE.items()
            if role == Ticket.Role.AUTHOR and agent == CHAINING_ORCHESTRATOR
        }
        assert offenders == {}, f"author phases must not chain through the orchestrator: {offenders}"

    def test_reviewer_role_reviewing_still_routes_to_reviewer(self) -> None:
        assert subagent_for_phase(Ticket.Role.REVIEWER, "reviewing") == "t3:reviewer"

    def test_short_verb_spelling_resolves_same_as_canonical(self) -> None:
        assert subagent_for_phase(Ticket.Role.AUTHOR, "code") == "t3:coder"
        assert subagent_for_phase(Ticket.Role.AUTHOR, "ship") == "t3:shipper"


class TestLoopDispatchCommandConformance(TestCase):
    """``_SUBAGENT_BY_PHASE`` (the pending-spawn map) mirrors the canonical map."""

    def test_command_map_is_the_canonical_map(self) -> None:
        assert loop_dispatch_cmd._SUBAGENT_BY_PHASE is SUBAGENT_BY_PHASE

    def test_every_author_phase_has_a_non_orchestrator_subagent(self) -> None:
        for phase, expected in EXPECTED_AUTHOR_AGENT.items():
            ticket = Ticket.objects.create(
                overlay="acme",
                issue_url=f"https://example.com/issues/{phase}",
                role=Ticket.Role.AUTHOR,
            )
            session = Session.objects.create(ticket=ticket, agent_id=phase)
            Task.objects.create(ticket=ticket, session=session, phase=phase)
            stdout = StringIO()
            call_command("loop_dispatch", "pending-spawn", "--json", stdout=stdout)
            payload = json.loads(stdout.getvalue())
            entry = next(e for e in payload if e["phase"] == phase)
            assert entry["subagent"] == expected
            assert entry["subagent"] != CHAINING_ORCHESTRATOR


class TestPendingTaskSignalConformance(TestCase):
    """``loop.dispatch`` routes a ``pending_task`` signal phase-aware.

    The ``PendingTasksScanner`` emits one ``pending_task`` per pending row;
    the dispatcher must route it to the phase's own agent, never a single
    chaining orchestrator.
    """

    def _signal(self, phase: str, *, role: str = Ticket.Role.AUTHOR) -> ScanSignal:
        return ScanSignal(
            kind="pending_task",
            summary=f"Task ({phase}) pending",
            payload={"task_id": 1, "phase": phase, "ticket_id": 1, "ticket_role": role},
        )

    def test_every_author_phase_dispatches_to_its_own_agent(self) -> None:
        for phase, expected in EXPECTED_AUTHOR_AGENT.items():
            actions = dispatch([self._signal(phase)])
            agent_actions = [a for a in actions if a.kind == "agent"]
            assert len(agent_actions) == 1, f"phase {phase!r}: expected one agent action, got {agent_actions}"
            assert agent_actions[0].zone == expected, (
                f"phase {phase!r} dispatched to {agent_actions[0].zone!r}, expected {expected!r}"
            )

    def test_no_author_phase_dispatches_to_chaining_orchestrator(self) -> None:
        for phase in EXPECTED_AUTHOR_AGENT:
            actions = dispatch([self._signal(phase)])
            zones = {a.zone for a in actions if a.kind == "agent"}
            assert CHAINING_ORCHESTRATOR not in zones, (
                f"author phase {phase!r} must not route to the chaining orchestrator (zones={zones})"
            )


class TestEverySubagentResolvesToAnAgentDefinition(TestCase):
    """Every ``SUBAGENT_BY_PHASE`` value must resolve to a real agent file.

    The loop dispatches a phase by passing its ``t3:<name>`` value as the
    ``Agent`` tool's ``subagent_type``; the tool resolves that against an
    ``agents/<name>.md`` definition. A phase mapped to a value with no
    matching file errors at spawn time (``Agent type 't3:<name>' not
    found``), so the work unit can never run. This scans the *whole* map —
    not just the four FSM phases — so a phase added to ``SUBAGENT_BY_PHASE``
    without its agent definition fails here, at conformance time.
    """

    def test_agents_dir_exists(self) -> None:
        assert AGENTS_DIR.is_dir(), f"agents directory not found at {AGENTS_DIR}"

    def test_every_mapped_subagent_has_an_agent_definition(self) -> None:
        missing = {
            (role, phase): subagent
            for (role, phase), subagent in SUBAGENT_BY_PHASE.items()
            if not (AGENTS_DIR / f"{_agent_name(subagent)}.md").is_file()
        }
        assert missing == {}, (
            f"phases mapped to a sub-agent with no agents/<name>.md definition: {missing}. "
            f"Spawning these via the Agent tool errors \"Agent type '<value>' not found\"."
        )

    def test_every_mapped_subagent_uses_the_t3_namespace(self) -> None:
        offenders = {
            (role, phase): subagent
            for (role, phase), subagent in SUBAGENT_BY_PHASE.items()
            if not subagent.startswith("t3:")
        }
        assert offenders == {}, f"sub-agent values must be namespaced 't3:<name>': {offenders}"

    def test_agent_definition_name_matches_its_filename(self) -> None:
        for (role, phase), subagent in SUBAGENT_BY_PHASE.items():
            name = _agent_name(subagent)
            agent_file = AGENTS_DIR / f"{name}.md"
            text = agent_file.read_text(encoding="utf-8")
            assert f"name: {name}\n" in text, (
                f"{agent_file} frontmatter 'name:' must equal {name!r} so the Agent tool "
                f"resolves {subagent!r} dispatched for ({role}, {phase})"
            )
