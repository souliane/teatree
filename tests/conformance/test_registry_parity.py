"""Producer/consumer registry-parity conformance — the anti-drift framework (#1).

The dominant integration-failure family in the autonomy layer is *paired-registry
drift*: one side of a producer→consumer seam gains a member while the other does
not, so work is produced that nothing consumes (or vice versa) and the dispatch
is silently dropped. The #1 blocker was exactly this — ``dispatch_*`` produced
~6 agent zones (codex review, red-card, red-MR fix, e2e-fix, answerer,
skill-drift) that ``persistence._ZONE_HANDLERS`` had no consumer for, so they
were dropped AND their idempotency markers were burned first.

This module is the reusable structural fix. ``assert_registry_covers`` enumerates
one side of a seam and asserts coverage on the other; each seam is its OWN test
function so later fix-PRs (DIS-B/D/E, SIG-4, MW-A/B) add lanes append-only —
a new registry pair is a new ``TestXParity`` class + a call to the shared helper,
never a rewrite. Every lane carries an anti-vacuity floor so emptying an
enumeration cannot turn a lane vacuous-green.
"""

import inspect
from collections.abc import Iterable
from pathlib import Path
from unittest.mock import patch

import pytest
from django.db.models import Q

from teatree.agents.sdk_tool_map import CAPABILITY_TO_SDK_TOOLS
from teatree.core.management.commands import loop_dispatch
from teatree.core.managers import TaskQuerySet
from teatree.core.modelkit.phase_tools import _TOOLS_BY_PHASE, tools_for_phase
from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE, normalize_phase
from teatree.core.models import Task
from teatree.loop.dispatch_tables import AGENT_ZONES, PERSISTED_AT_SOURCE_ZONES
from teatree.loop.job_identity import PER_OVERLAY_DOMAINS
from teatree.loop.persistence import _HANDLER_TARGET_PHASES, _ZONE_HANDLERS
from teatree.loop.phases import orchestrate
from teatree.loops.registry import iter_loops
from teatree.loops.seed import DEFAULT_LOOPS


def assert_registry_covers(
    *,
    producers: Iterable[object],
    consumers: Iterable[object],
    label: str,
    allowlist: Iterable[object] = (),
) -> None:
    """Assert every *producer* has a *consumer* (or is explicitly allowlisted).

    The single reusable primitive every parity lane below calls. An allowlisted
    producer is a deliberate no-consumer case (documented at the call site); an
    un-allowlisted uncovered producer is registry drift and fails loud.
    """
    uncovered = set(producers) - set(consumers) - set(allowlist)
    assert not uncovered, f"{label}: producer(s) with no consumer (registry drift): {sorted(map(str, uncovered))}"


class TestDispatchZoneExecutorParity:
    """LANE 1 — every ``dispatch_*`` agent zone has a persistence executor.

    ``AGENT_ZONES`` (the producer SSOT) must be exactly the union of the
    ``_ZONE_HANDLERS`` consumers and the ``PERSISTED_AT_SOURCE_ZONES`` no-ops.
    A new dispatch producer with no persistence consumer fails here — the #1
    blocker's silent-drop can no longer ship green.
    """

    def test_every_agent_zone_is_handled_or_persisted_at_source(self) -> None:
        assert_registry_covers(
            producers=AGENT_ZONES,
            consumers=set(_ZONE_HANDLERS) | set(PERSISTED_AT_SOURCE_ZONES),
            label="AGENT_ZONES -> persistence executor contract",
        )

    def test_no_handler_or_persisted_zone_is_an_orphan(self) -> None:
        # The reverse direction: a handler (or persisted-at-source zone) that no
        # ``dispatch_*`` path can actually produce is dead consumer surface.
        orphans = (set(_ZONE_HANDLERS) | set(PERSISTED_AT_SOURCE_ZONES)) - set(AGENT_ZONES)
        assert not orphans, f"consumer zones with no producer: {sorted(orphans)}"

    def test_handler_target_phases_are_dispatchable(self) -> None:
        # Every (role, phase) a handler writes MUST be a SUBAGENT_BY_PHASE key,
        # else the persisted row is one no claimer can pick up.
        orphan = _HANDLER_TARGET_PHASES - set(SUBAGENT_BY_PHASE)
        assert not orphan, f"handler target (role, phase) with no dispatchable agent: {sorted(orphan)}"

    def test_persisted_at_source_zones_are_the_subagent_values(self) -> None:
        # The pending_task re-emission set IS the SUBAGENT_BY_PHASE value set —
        # the two must not drift.
        assert set(PERSISTED_AT_SOURCE_ZONES) == set(SUBAGENT_BY_PHASE.values())

    def test_cardinality_floors_anti_vacuity(self) -> None:
        # A refactor that empties an enumeration must not make the lanes above
        # vacuously green. These floors are safely below the real cardinalities.
        assert len(AGENT_ZONES) >= 10, AGENT_ZONES
        assert len(_ZONE_HANDLERS) >= 6, _ZONE_HANDLERS
        assert len(PERSISTED_AT_SOURCE_ZONES) >= 10, PERSISTED_AT_SOURCE_ZONES
        assert len(_HANDLER_TARGET_PHASES) >= 6, _HANDLER_TARGET_PHASES

    def test_revived_dark_zones_are_now_handled(self) -> None:
        # The #1 blocker's specific dark zones: each must now be a real handler
        # consumer (not merely persisted-at-source), because each is produced by
        # a NON-pending-task path (AGENT_BY_KIND / MECHANICAL / conditional).
        for zone in ("t3:debug", "t3:e2e", "t3:coder", "t3:answerer", "codex:review", "codex:adversarial-review"):
            assert zone in _ZONE_HANDLERS, f"dark zone {zone!r} still has no persistence handler"


class TestDispatchableFilterSsotParity:
    """LANE 2 — the ONE ``Task.dispatchable_q`` SSOT gates every dispatch site (#6).

    The #2218 recurrence class: the dispatchable filter re-hand-rolled per
    consumer, so a fix to one copy (the #2217 external-delivery exclusion) never
    reached the other — the live ``claim-next``/``pending-spawn`` double-dispatched
    onto leased tickets while ``orchestrate`` correctly excluded them. Now every
    consumer builds ON ``Task.dispatchable_q``: ``orchestrate`` returns it
    verbatim, ``claim-next`` ANDs the INTERACTIVE narrowing, ``pending-spawn``
    shares ``claim-next``'s helper, and the admit-budget gate counts through the
    un-narrowed SSOT. A consumer that stops referencing the symbol fails here.
    """

    _SENTINEL = Q(pk__in=[-98765])
    _INTERACTIVE = Q(execution_target=Task.ExecutionTarget.INTERACTIVE)

    def test_orchestrate_filter_delegates_to_the_ssot(self) -> None:
        with patch.object(Task, "dispatchable_q", return_value=self._SENTINEL):
            assert orchestrate._dispatchable_filter() == self._SENTINEL

    def test_claim_filter_is_the_ssot_narrowed_to_interactive(self) -> None:
        with patch.object(Task, "dispatchable_q", return_value=self._SENTINEL):
            assert loop_dispatch._dispatchable_q() == self._SENTINEL & self._INTERACTIVE

    def test_budget_gate_counts_through_the_un_narrowed_ssot(self) -> None:
        # The boost budget is computed (orchestrate) over the SSOT WITHOUT the
        # execution_target narrowing, so a HEADLESS in-flight claim consumes it;
        # the live gate must count with the SAME set — the un-narrowed SSOT, never
        # ``_dispatchable_q()`` — or it overshoots N with headless workers running.
        with (
            patch.object(Task, "dispatchable_q", return_value=self._SENTINEL),
            patch.object(TaskQuerySet, "in_flight_claimed_count", return_value=0) as count,
            patch.object(loop_dispatch, "read_admit_budget", return_value=5),
        ):
            loop_dispatch._admit_budget_exhausted()
        count.assert_called_once_with(self._SENTINEL)

    def test_pending_spawn_shares_the_claim_filter(self) -> None:
        # Structural: the in-session preview MUST filter through the same
        # ``_dispatchable_q()`` the atomic claim uses, so it cannot drift back to
        # a role/phase-only filter that ignores the external-delivery exclusion.
        source = inspect.getsource(loop_dispatch.Command.pending_spawn)
        assert "_dispatchable_q()" in source

    def test_ssot_is_referenced_by_all_three_live_consumers(self) -> None:
        # The parity claim made explicit: orchestrate, claim-next, and the budget
        # gate each name ``dispatchable_q`` in their own source (pending-spawn is
        # covered above via ``_dispatchable_q``), so no consumer can re-hand-roll
        # the filter and silently diverge.
        consumers = (
            orchestrate._dispatchable_filter,
            loop_dispatch._dispatchable_q,
            loop_dispatch._admit_budget_exhausted,
        )
        for fn in consumers:
            assert "dispatchable_q" in inspect.getsource(fn), fn.__qualname__


class TestLoopRegistryCoverageParity:
    """LANE 3 — every per-overlay Domain / MiniLoop has its consumer + seed row (#22, #23).

    The #22/#23 family: opt-in domains/scanners with no consuming MiniLoop (dead
    on the live per-loop fan-out) and MiniLoops with no seed row (the fan-out can
    never admit them). Every ``PER_OVERLAY_DOMAINS`` member is consumed by some
    ``iter_loops()`` MiniLoop, and the registry and ``DEFAULT_LOOPS`` seed cover
    each other. The runtime + single-overlay-builder lanes live in
    ``tests/teatree_loop/test_loop_registry_coverage.py``.
    """

    @staticmethod
    def _domains_consumed_by_miniloops() -> set[object]:
        consumed: set[object] = set()
        for loop in iter_loops():
            source = inspect.getsource(loop.build_jobs)
            consumed.update(domain for domain in PER_OVERLAY_DOMAINS if f"Domain.{domain.name}" in source)
        return consumed

    def test_every_per_overlay_domain_has_a_consuming_miniloop(self) -> None:
        assert_registry_covers(
            producers=PER_OVERLAY_DOMAINS,
            consumers=self._domains_consumed_by_miniloops(),
            label="PER_OVERLAY_DOMAINS -> consuming MiniLoop",
        )

    def test_every_registry_miniloop_is_seeded(self) -> None:
        assert_registry_covers(
            producers={loop.name for loop in iter_loops()},
            consumers={spec.name for spec in DEFAULT_LOOPS},
            label="registry MiniLoop -> DEFAULT_LOOPS seed row",
        )

    def test_every_seed_row_has_a_registry_miniloop(self) -> None:
        assert_registry_covers(
            producers={spec.name for spec in DEFAULT_LOOPS},
            consumers={loop.name for loop in iter_loops()},
            label="DEFAULT_LOOPS seed row -> registry MiniLoop",
        )

    def test_cardinality_floors_anti_vacuity(self) -> None:
        assert len(PER_OVERLAY_DOMAINS) >= 10, PER_OVERLAY_DOMAINS
        assert len(tuple(iter_loops())) >= 18


class TestRegistryParityFrameworkFiresRed:
    """Anti-vacuity: prove ``assert_registry_covers`` actually catches drift.

    A conformance gate that can never fail is worthless. A synthetic producer
    with no consumer must raise; an allowlisted one must pass.
    """

    def test_uncovered_producer_raises(self) -> None:
        with pytest.raises(AssertionError):
            assert_registry_covers(
                producers={"t3:reviewer", "t3:SYNTHETIC-UNCONSUMED"},
                consumers={"t3:reviewer"},
                label="self-test",
            )

    def test_allowlisted_producer_passes(self) -> None:
        assert_registry_covers(
            producers={"t3:reviewer", "t3:DELIBERATE-NO-CONSUMER"},
            consumers={"t3:reviewer"},
            label="self-test",
            allowlist={"t3:DELIBERATE-NO-CONSUMER"},
        )


class TestPhaseToolsTotalityParity:
    """LANE 4 — every dispatchable phase has an EXPLICIT least-privilege entry (#10).

    ``bughunt`` (and, after DIS-A, ``debugging`` / ``codex_reviewing`` /
    ``codex_adversarial_reviewing``) were dispatchable through
    ``SUBAGENT_BY_PHASE`` yet absent from ``_TOOLS_BY_PHASE``, so
    ``tools_for_phase`` silently resolved them to the read-only fallback — a
    bughunter whose brief tells it to reproduce findings could not run the
    shell. Every ``SUBAGENT_BY_PHASE`` phase must carry an explicit
    ``_TOOLS_BY_PHASE`` key; the read-only fallback stays as defense-in-depth
    for a genuinely unregistered phase, never as the silent resolution for a
    dispatchable one — the #10 recurrence dies in CI.
    """

    @staticmethod
    def _dispatchable_phases() -> set[str]:
        return {normalize_phase(phase) for _role, phase in SUBAGENT_BY_PHASE}

    def test_every_dispatchable_phase_has_an_explicit_tool_entry(self) -> None:
        assert_registry_covers(
            producers=self._dispatchable_phases(),
            consumers=set(_TOOLS_BY_PHASE),
            label="SUBAGENT_BY_PHASE phase -> explicit _TOOLS_BY_PHASE entry",
        )

    def test_bughunt_can_reproduce_findings_but_not_write(self) -> None:
        tools = tools_for_phase("bughunt")
        assert {"shell", "dispatch_subtask", "read_file"} <= tools
        assert "write_file" not in tools
        assert "edit_file" not in tools

    def test_cardinality_floor_anti_vacuity(self) -> None:
        assert len(self._dispatchable_phases()) >= 12, self._dispatchable_phases()
        assert len(_TOOLS_BY_PHASE) >= 12, set(_TOOLS_BY_PHASE)


#: teatree capability name <- each ``claude_sdk`` built-in tool an agent md may list.
_SDK_TOOL_TO_CAPABILITY: dict[str, str] = {
    sdk_name: capability for capability, sdk_names in CAPABILITY_TO_SDK_TOOLS.items() for sdk_name in sdk_names
}
_AGENTS_DIR = Path(__file__).resolve().parents[2] / "agents"
#: The shell-denied reactive phases whose headless agents hand work back through
#: the result envelope (DIS-B, #9). Their briefs must not promise the shell the
#: lane strips, or the article-ideas / approved-reply silent-drop class returns.
_ENVELOPE_SHELL_DENIED_PHASES = ("scanning_news", "answering")


def _agent_allowlist_tools(agent_file_stem: str) -> frozenset[str] | None:
    """Teatree capabilities an ``agents/<stem>.md`` ``tools:`` allowlist grants.

    ``None`` for a brief using the ``disallowedTools:`` denylist convention (a
    different mechanism) or missing frontmatter — this lane checks only positive
    allowlist briefs, where a listed tool is a direct promise of that capability.
    """
    path = _AGENTS_DIR / f"{agent_file_stem}.md"
    if not path.is_file():
        return None
    in_tools = False
    listed: list[str] = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if number > 1 and stripped == "---":
            break
        if not raw.startswith((" ", "\t")) and ":" in stripped:
            in_tools = stripped.split(":", 1)[0].strip() == "tools"
            continue
        if in_tools and stripped.startswith("- "):
            listed.append(stripped.removeprefix("- ").strip())
    if not listed:
        return None
    return frozenset(_SDK_TOOL_TO_CAPABILITY[name] for name in listed if name in _SDK_TOOL_TO_CAPABILITY)


def _agent_for_phase(target_phase: str) -> str:
    for (_role, phase), agent in SUBAGENT_BY_PHASE.items():
        if phase == target_phase:
            return agent
    return ""


def _phases_for_agent(subagent: str) -> set[str]:
    """Every phase the loop dispatches to *subagent* (reverse of ``SUBAGENT_BY_PHASE``)."""
    return {phase for (_role, phase), agent in SUBAGENT_BY_PHASE.items() if agent == subagent}


def _phase_envelope(subagent: str) -> frozenset[str]:
    """Capability envelope for *subagent*: the union of every phase's allowance.

    An agent dispatched for more than one phase (``t3:planner`` → ``planning`` +
    ``directive_interpreting``) declares the SUPERSET its most-privileged phase
    needs; each phase's ``phase_tools`` entry narrows it below at dispatch (Lane A
    injects the per-phase complement). So the coherent floor is that a declared
    capability be usable in AT LEAST ONE phase the agent serves — the union — not
    that every phase allow every declared tool.
    """
    envelope: set[str] = set()
    for phase in _phases_for_agent(subagent):
        envelope |= tools_for_phase(phase)
    return frozenset(envelope)


def _agent_uses_denylist(agent_file_stem: str) -> bool:
    """True when ``agents/<stem>.md`` declares tools via the ``disallowedTools:`` denylist.

    A denylist brief is the complementary convention to the ``tools:`` allowlist:
    the phase policy is its strict ceiling (Lane A applies the per-phase disallow
    on top), so it cannot over-promise a phase-denied tool — the over-promise
    class the envelope lane guards. Recognising it keeps the coverage claim
    honest: every dispatched agent is EITHER allowlist-checked OR denylist-known,
    never a silent third state with no tool declaration at all.
    """
    path = _AGENTS_DIR / f"{agent_file_stem}.md"
    if not path.is_file():
        return False
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        stripped = raw.strip()
        if number > 1 and stripped == "---":
            break
        if not raw.startswith((" ", "\t")) and stripped.split(":", 1)[0].strip() == "disallowedTools":
            return True
    return False


def _dispatched_allowlist_agents() -> dict[str, frozenset[str]]:
    """Every dispatched ``t3:`` agent that uses the ``tools:`` allowlist → its capabilities.

    Skips the ``codex:`` slash-command agents (resolved via the codex CLI, not an
    ``agents/*.md`` brief) and the ``disallowedTools:`` denylist agents (a
    different mechanism — the phase policy is their strict ceiling and can only
    narrow, so a denylist brief cannot over-promise a phase-denied tool).
    """
    declared_by_agent: dict[str, frozenset[str]] = {}
    for subagent in set(SUBAGENT_BY_PHASE.values()):
        if not subagent.startswith("t3:"):
            continue
        declared = _agent_allowlist_tools(subagent.removeprefix("t3:"))
        if declared is None:
            continue
        declared_by_agent[subagent] = declared
    return declared_by_agent


class TestAgentMdPhaseToolParity:
    """LANE 5 — a headless brief never promises a tool its phase denies (#9).

    ``scanning-news.md`` / ``answerer.md`` listed ``Bash`` while their
    ``phase_tools`` entry is shell-denied, so the lane stripped the shell the
    brief promised and a run that tried to use it dropped its work. These
    reactive phases now hand results back through the typed envelope channel
    BECAUSE they cannot shell out; their ``tools:`` allowlist must match the
    SSOT (no shell), or the silent-drop class returns. Scoped to the two
    envelope-channel phases DIS-B owns — the planner/shipper allowlist briefs
    carry separate pre-existing divergences outside this PR's file ownership.
    """

    @pytest.mark.parametrize("phase", _ENVELOPE_SHELL_DENIED_PHASES)
    def test_agent_tools_are_a_subset_of_the_phase_allowance(self, phase: str) -> None:
        agent = _agent_for_phase(phase)
        assert agent, f"no dispatched agent for phase {phase!r}"
        declared = _agent_allowlist_tools(agent.removeprefix("t3:"))
        assert declared is not None, f"{agent} has no tools: allowlist to check"
        allowed = tools_for_phase(phase)
        assert declared <= allowed, f"{agent} promises {sorted(declared - allowed)} denied by phase {phase!r}"
        assert "shell" not in declared, f"{agent} declares the shell but phase {phase!r} is shell-denied"

    def test_both_reactive_agents_are_actually_checked(self) -> None:
        checked = [
            _agent_allowlist_tools(_agent_for_phase(phase).removeprefix("t3:"))
            for phase in _ENVELOPE_SHELL_DENIED_PHASES
        ]
        assert all(tools is not None for tools in checked), checked
        assert len(checked) >= 2


class TestEveryDispatchedAgentMdMatchesPhasePolicy:
    """LANE 5b — NO dispatched ``tools:`` brief promises a tool its phases deny (#89).

    LANE 5 above pinned only the two envelope-channel phases; the planner/shipper
    allowlist briefs carried pre-existing declaration↔SSOT divergences #3012
    deferred as "outside this PR's file ownership": ``planner.md`` declared
    ``Bash`` while ``planning`` was shell-denied, and ``shipper.md`` declared
    ``Write``/``Edit`` while ``shipping`` denies write. This lane closes the whole
    surface — every dispatched ``t3:`` agent using the ``tools:`` allowlist must
    declare only capabilities its phase envelope (:func:`_phase_envelope`) grants,
    so any future declaration-vs-``phase_tools`` divergence fails here in CI, not
    silently at dispatch where Lane A strips the promised-but-denied tool.
    """

    def test_no_dispatched_allowlist_brief_promises_a_phase_denied_tool(self) -> None:
        offenders = {
            subagent: sorted(declared - _phase_envelope(subagent))
            for subagent, declared in _dispatched_allowlist_agents().items()
            if declared - _phase_envelope(subagent)
        }
        assert offenders == {}, (
            f"agent tools: allowlist promises capabilities no phase it serves allows "
            f"(declaration diverges from the phase_tools SSOT): {offenders}"
        )

    def test_planning_phase_grants_shell_for_git_archaeology(self) -> None:
        # The authoritative side for planning is the DECLARATION: an honest plan
        # needs git archaeology (fetch, log, base_sha capture), so the policy is
        # widened to the shell planner.md already declares — not the declaration
        # narrowed. bughunt/shipping are the shell-in-a-read-mostly-phase precedent.
        assert "shell" in tools_for_phase("planning")
        assert "shell" in _agent_allowlist_tools("planner")

    def test_directive_interpreting_stays_read_only_no_shell(self) -> None:
        # Planner serves planning AND directive_interpreting; only planning is
        # widened. The read-only interpreter keeps least privilege — it drafts a
        # sketch and never shells out, so its phase strips the planner's shell.
        assert "shell" not in tools_for_phase("directive_interpreting")

    def test_shipping_phase_authoritative_declaration_drops_write(self) -> None:
        # The authoritative side for shipping is the POLICY: shipping commits,
        # pushes, and opens MRs via the shell — it never edits source, so the
        # declaration is narrowed to match, not the policy widened to grant write.
        declared = _agent_allowlist_tools("shipper")
        assert declared is not None
        assert "write_file" not in declared, "shipper.md must not declare Write — shipping denies write"
        assert "edit_file" not in declared, "shipper.md must not declare Edit — shipping denies write"
        assert {"write_file", "edit_file"} & tools_for_phase("shipping") == set()

    def test_every_dispatched_agent_md_declares_tools_by_one_convention(self) -> None:
        # Exhaustiveness: every dispatched t3: agent is EITHER an allowlist brief
        # (checked by the envelope lane above) OR a recognised disallowedTools
        # denylist brief. A future agent added with no tool declaration at all
        # would silently escape both — this closes that blind spot so "cover ALL
        # agents" stays honest, not a shell.
        allowlist = set(_dispatched_allowlist_agents())
        unclassified = {
            subagent
            for subagent in set(SUBAGENT_BY_PHASE.values())
            if subagent.startswith("t3:")
            and subagent not in allowlist
            and not _agent_uses_denylist(subagent.removeprefix("t3:"))
        }
        assert unclassified == set(), (
            f"dispatched agent(s) with no tools:/disallowedTools: declaration to check: {sorted(unclassified)}"
        )

    def test_allowlist_agent_coverage_floor_anti_vacuity(self) -> None:
        # Emptying SUBAGENT_BY_PHASE or the agents/ dir must not make the lane
        # vacuously green. Nine t3: allowlist agents are dispatched today
        # (answerer, bughunter, coder, debugger, e2e, planner, scanning-news,
        # shipper, tester); the floor sits safely below.
        assert len(_dispatched_allowlist_agents()) >= 8, sorted(_dispatched_allowlist_agents())
