"""The runtime-read names that still carry the legacy word keep resolving, each written out literally.

Live sessions hold escape tokens under these ids, the control DB holds rows under them, and
the plugin loads the two hook modules by file name. A rename that misses one orphans the
stored data or breaks a hook at import, so each name below is a literal: whoever renames
one has to edit this file on purpose and say why.
"""

import importlib
import sys
from pathlib import Path

import pytest
from django.test import TestCase

import hooks.scripts.hook_router as router
from hooks.scripts.gate_ledger import DECLARED_GATES
from teatree.eval.discovery import FIXTURES_DIR, discover_core_specs
from teatree.eval.presets import baseline_preset
from teatree.hooks.gate_escape import KNOWN_GATES, EscapePolicy, find_escape, policy_for
from teatree.loop.dispatch import dispatch
from teatree.loop.persistence import persist_agent_actions
from teatree.loop.scanners.base import ScanSignal

_REPO = Path(__file__).resolve().parents[2]
GATE_IDS = ("orchestrator_bash", "orchestrator_delegation_gate", "orchestrator_foreground_dispatch")
HOOK_MODULES = ("orchestrator_delegation_gate", "orchestrator_investigation_gate")
HANDLERS = (
    "handle_enforce_orchestrator_boundary",
    "handle_enforce_orchestrator_investigation_boundary",
    "handle_orchestrator_turn_budget_nudge",
)
ZONE_LABEL = "t3:orchestrator"
EVAL_IDS = (
    "orchestrator_boundary_long_work_to_background_agent",
    "orchestrator_boundary_no_foreground_edit_in_main_agent",
    "orchestrator_collects_result_not_polls_subagent",
    "orchestrator_delegates_investigation",
    "orchestrator_delegates_refactor",
    "orchestrator_delegates_test_writing",
    "orchestrator_embeds_skills_in_subagent_brief",
    "orchestrator_escalates_blocked_subagent_result_not_swallows",
)


@pytest.mark.parametrize("gate", GATE_IDS)
def test_the_gate_id_is_declared_in_both_ledgers_with_an_audited_escape(gate: str) -> None:
    assert gate in DECLARED_GATES
    assert gate in KNOWN_GATES
    assert policy_for(gate) is EscapePolicy.AUDITED


@pytest.mark.parametrize("gate", GATE_IDS)
def test_a_legacy_escape_token_is_accepted_only_under_its_own_id(gate: str) -> None:
    payload = {"command": f"uv run pytest [gate-escape:{gate}: reviewing the hook]"}

    assert find_escape(payload, gate) == "reviewing the hook"
    assert [other for other in GATE_IDS if find_escape(payload, other)] == [gate]


@pytest.mark.parametrize("module", HOOK_MODULES)
def test_the_hook_script_keeps_its_file_name_and_both_import_identities(module: str) -> None:
    assert (_REPO / "hooks" / "scripts" / f"{module}.py").is_file()

    loaded = importlib.import_module(f"hooks.scripts.{module}")

    assert sys.modules[module] is loaded


@pytest.mark.parametrize("handler", HANDLERS)
def test_the_handler_keeps_its_name_and_stays_on_the_pretooluse_chain(handler: str) -> None:
    assert vars(router)[handler] in router._HANDLERS["PreToolUse"]


@pytest.mark.parametrize("eval_id", EVAL_IDS)
def test_the_eval_scenario_id_is_still_pinned_discovered_and_replayable(eval_id: str) -> None:
    assert eval_id in baseline_preset().scenario_tiers
    assert eval_id in {spec.name for spec in discover_core_specs()}
    assert (FIXTURES_DIR / f"{eval_id}_pass.stream.jsonl").is_file()


class TestTheZoneLabel(TestCase):
    def test_an_admitted_issue_still_reaches_the_zone_and_schedules_planning(self) -> None:
        signal = ScanSignal(
            kind="issue_intake.admitted",
            summary="Claimed for auto-implement",
            payload={"url": "https://example.com/owner/repo/issues/7", "auto_start": True, "overlay": "acme"},
        )

        agent_actions = [action for action in dispatch([signal]) if action.kind == "agent"]
        created = persist_agent_actions(agent_actions)

        assert [action.zone for action in agent_actions] == [ZONE_LABEL]
        assert [task.phase for task in created] == ["planning"]

    def test_a_red_card_still_reaches_the_zone_and_stamps_its_row(self) -> None:
        signal = ScanSignal(
            kind="red_card.signal",
            summary="RED CARD from U1",
            payload={"row_id": 5, "signal_kind": "red_circle", "user_id": "U1", "overlay": "acme"},
        )

        agent_actions = [action for action in dispatch([signal]) if action.kind == "agent"]
        created = persist_agent_actions(agent_actions)

        assert [action.zone for action in agent_actions] == [ZONE_LABEL]
        assert [task.ticket.extra["red_card_signal_id"] for task in created] == [5]
