"""Aggregate every declared scenario group into one ordered catalog.

``_AGENT_SECTIONS`` is the token-cost lever applied centrally: a generated
scenario that pins ONE rule of a large multi-rule skill (notably the 77 KB
``skills/rules/SKILL.md``) declares only the ``## `` section it tests, so the
metered runner sends that section as the system prompt instead of the whole file.
The map lives here — one auditable place — rather than scattered across the
catalog declarations. Each section name is verified against the real on-disk
SKILL.md by ``tests/eval_replay/test_scenarios_anti_vacuous.py`` (a typo'd anchor is a
hard RED), and the size win is measured by ``tests/teatree_eval/test_context_budget.py``.
A scenario absent from the map sends the whole file — the safe default.
"""

import dataclasses

from scripts.eval.corpus_gen.catalog import RECURRING
from scripts.eval.corpus_gen.model import Scenario
from scripts.eval.corpus_gen.per_skill import PER_SKILL

# scenario name -> the ``## `` sections of its agent_path SKILL.md it exercises.
_AGENT_SECTIONS: dict[str, tuple[str, ...]] = {
    "background_long_ops_docker_build": ("Background Long Operations (Non-Negotiable)",),
    "background_long_ops_db_migration_replay": ("Background Long Operations (Non-Negotiable)",),
    "background_long_ops_e2e_suite": ("Background Long Operations (Non-Negotiable)",),
    "background_long_ops_large_clone": ("Background Long Operations (Non-Negotiable)",),
    "id_namespace_forge_ref_repo_qualified": ("ID Namespace Disambiguation (Non-Negotiable)",),
    "blocked_subagent_surfaces_structured_block_not_workaround": ("Sub-Agent Limitations",),
    "blocked_subagent_missing_token_surfaces_not_partial_ship": ("Sub-Agent Limitations",),
    "orchestrator_escalates_blocked_subagent_result_not_swallows": ("Sub-Agent Limitations",),
    "orchestrator_delegates_refactor": ("Sub-Agent Limitations", "Background Long Operations (Non-Negotiable)"),
    "orchestrator_delegates_investigation": ("Sub-Agent Limitations", "Background Long Operations (Non-Negotiable)"),
    "orchestrator_delegates_test_writing": ("Sub-Agent Limitations", "Background Long Operations (Non-Negotiable)"),
    "orchestrator_collects_result_not_polls_subagent": (
        "Sub-Agent Limitations",
        "Background Long Operations (Non-Negotiable)",
    ),
    "comm_asks_via_askuserquestion_not_chat": ("Always Use AskUserQuestion for Questions",),
    "banned_term_to_public_repo_is_blocked": (
        "Verify Repo Visibility Before Filing External Issues (Non-Negotiable)",
        "Public-Repo Commit Author Identity (Non-Negotiable)",
    ),
    "banned_term_to_private_repo_is_not_blocked": (
        "Verify Repo Visibility Before Filing External Issues (Non-Negotiable)",
        "Public-Repo Commit Author Identity (Non-Negotiable)",
    ),
    "on_behalf_drafts_and_dms_before_posting": (
        "Ask Before Posting on the User's Behalf (Non-Negotiable)",
        "No AI Signature on Posts Made on the User's Behalf (Non-Negotiable)",
    ),
    "on_behalf_colleague_message_uses_personal_token": ("Ask Before Posting on the User's Behalf (Non-Negotiable)",),
    "on_behalf_notifies_user_after_posting": ("Ask Before Posting on the User's Behalf (Non-Negotiable)",),
    "on_behalf_dm_to_user_uses_overlay_bot": ("Ask Before Posting on the User's Behalf (Non-Negotiable)",),
    "away_ask_no_colleague_reaction_on_merged_mr": ("Ask Before Posting on the User's Behalf (Non-Negotiable)",),
    "approved_colleague_reaction_fires_and_dms_receipt": ("Ask Before Posting on the User's Behalf (Non-Negotiable)",),
    "self_dm_eyes_ack_still_placed_under_ask": ("Ask Before Posting on the User's Behalf (Non-Negotiable)",),
}


def _with_agent_sections(scenario: Scenario) -> Scenario:
    sections = _AGENT_SECTIONS.get(scenario.name)
    if sections is None or scenario.agent_sections:
        return scenario
    _assert_sections_resolve(scenario.name, scenario.agent_path, sections)
    return dataclasses.replace(scenario, agent_sections=sections)


def _assert_sections_resolve(name: str, agent_path: str, sections: tuple[str, ...]) -> None:
    """Fail generation if a mapped section is not in the scenario's own SKILL.md.

    Guards against keying ``_AGENT_SECTIONS`` on a name whose ``agent_path`` does
    not contain that section (the canonical rule lives in a DIFFERENT skill). A
    mismatch here would send an empty rule prompt and make the scenario vacuous.
    """
    from pathlib import Path

    from teatree.eval.context_budget import MissingSectionError, extract_sections

    root = Path(__file__).resolve().parents[3]
    text = (root / agent_path).read_text(encoding="utf-8")
    try:
        extract_sections(text, sections)
    except MissingSectionError as exc:
        msg = f"_AGENT_SECTIONS[{name!r}] does not resolve against {agent_path}: {exc}"
        raise ValueError(msg) from exc


ALL_SCENARIOS: list[Scenario] = [_with_agent_sections(s) for s in (*RECURRING, *PER_SKILL)]


def _assert_unique_names(scenarios: list[Scenario]) -> None:
    seen: set[str] = set()
    for scenario in scenarios:
        if scenario.name in seen:
            msg = f"duplicate scenario name: {scenario.name}"
            raise ValueError(msg)
        seen.add(scenario.name)


_assert_unique_names(ALL_SCENARIOS)
