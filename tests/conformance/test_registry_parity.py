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

from collections.abc import Iterable

import pytest

from teatree.core.modelkit.phases import SUBAGENT_BY_PHASE
from teatree.loop.dispatch_tables import AGENT_ZONES, PERSISTED_AT_SOURCE_ZONES
from teatree.loop.persistence import _HANDLER_TARGET_PHASES, _ZONE_HANDLERS


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
