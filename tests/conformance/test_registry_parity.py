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
from unittest.mock import patch

import pytest
from django.db.models import Q

from teatree.core.management.commands import loop_dispatch
from teatree.core.managers import TaskQuerySet
from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE
from teatree.core.models import Task
from teatree.loop.dispatch_tables import AGENT_ZONES, PERSISTED_AT_SOURCE_ZONES
from teatree.loop.persistence import _HANDLER_TARGET_PHASES, _ZONE_HANDLERS
from teatree.loop.phases import orchestrate


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
