"""Every registered loop carries its reach set and its determinism, declared in code.

The guard this module pins is the one an unclassified loop must not be able to
slip past: :func:`teatree.loops.classification.unclassified_loops` enumerates the
registry and names every ``MINI_LOOP`` that declares no reach set or no
determinism. Removing that check makes a tagless loop shippable, so the
``rejects_a_loop_that_declares_nothing`` lane below is the anti-vacuity floor —
it registers a synthetic tagless loop and requires the guard to name it.

The second lane is the one that stops a *lie*. ``deterministic`` is worth nothing
if a loop that spawns an agent can carry it, so the declared value is cross-checked
against the loop's real dispatch behaviour: the agent-routing signal kinds
(``AGENT_BY_KIND``, the ``("agent", …)`` rows of ``MECHANICAL_BY_KIND``, the
conditional handlers) and the agent-dispatched phase vocabulary, read out of the
loop's own package and the scanner modules it actually wires this tick. A loop
declaring ``deterministic`` while that evidence exists fails.
"""

import importlib
from pathlib import Path
from unittest.mock import MagicMock

from django.test import TestCase

from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.loops.base import LoopDeterminism, LoopReach, MiniLoop
from teatree.loops.classification import (
    agent_dispatch_vocabulary,
    ai_evidence,
    loop_package_sources,
    unclassified_loops,
)
from teatree.loops.registry import iter_loops
from teatree.loops.seed import DEFAULT_LOOPS

#: Loops whose AI evidence the derivation can see without arming any opt-in
#: setting. The floor stops the cross-check going vacuously green if the
#: evidence extractor stops finding anything.
_MIN_DERIVED_AI_LOOPS = 10


def _stub_backend() -> OverlayBackends:
    overlay = MagicMock()
    overlay.config.get_review_broadcast_channels.return_value = []
    overlay.config.get_review_channel.return_value = ("", "")
    overlay.metadata.get_followup_repos.return_value = []
    overlay.get_workspace_repos.return_value = []
    return OverlayBackends(
        name="teatree",
        hosts=(MagicMock(spec=CodeHostBackend),),
        messaging=MagicMock(spec=MessagingBackend),
        ready_labels=("ready",),
        overlay=overlay,
    )


def _wired_scanner_sources(mini_loop: MiniLoop) -> tuple[Path, ...]:
    """The defining file of every scanner *mini_loop* wires against a stub overlay."""
    backend = _stub_backend()
    jobs = mini_loop.build_jobs(
        backends=[backend],
        host=MagicMock(spec=CodeHostBackend),
        messaging=MagicMock(spec=MessagingBackend),
        notion_client=MagicMock(),
        ready_labels=("ready",),
    )
    modules = {type(job.scanner).__module__ for job in jobs}
    return tuple(Path(str(importlib.import_module(name).__file__)) for name in modules)


def _evidence(mini_loop: MiniLoop) -> tuple[str, ...]:
    return ai_evidence((*loop_package_sources(mini_loop.name), *_wired_scanner_sources(mini_loop)))


class LoopClassificationConformanceTestCase(TestCase):
    """No loop ships without a reach set and a determinism value."""

    def test_every_registered_loop_is_classified(self) -> None:
        assert unclassified_loops(iter_loops()) == ()

    def test_registry_is_not_empty(self) -> None:
        assert len(iter_loops()) > _MIN_DERIVED_AI_LOOPS

    def test_rejects_a_loop_that_declares_nothing(self) -> None:
        tagless = MiniLoop(name="tagless", default_cadence_seconds=60, build_jobs=lambda **_: [])
        assert unclassified_loops([tagless]) == ("tagless",)

    def test_rejects_a_loop_declaring_only_reach(self) -> None:
        half = MiniLoop(
            name="half",
            default_cadence_seconds=60,
            build_jobs=lambda **_: [],
            declared_reach=frozenset({LoopReach.INGRESS}),
        )
        assert unclassified_loops([half]) == ("half",)

    def test_an_empty_reach_set_is_a_declaration_not_a_gap(self) -> None:
        local_only = MiniLoop(
            name="local_only",
            default_cadence_seconds=60,
            build_jobs=lambda **_: [],
            declared_reach=frozenset(),
            determinism=LoopDeterminism.DETERMINISTIC,
        )
        assert unclassified_loops([local_only]) == ()
        assert local_only.tags == ("deterministic",)


class ColleagueRefinesEgressTestCase(TestCase):
    """``colleague`` is a refinement of ``egress``, never a peer of it."""

    def test_colleague_implies_egress_without_declaring_it(self) -> None:
        dms_the_owner = MiniLoop(
            name="dms_the_owner",
            default_cadence_seconds=60,
            build_jobs=lambda **_: [],
            declared_reach=frozenset({LoopReach.COLLEAGUE}),
            determinism=LoopDeterminism.DETERMINISTIC,
        )
        assert dms_the_owner.reach == frozenset({LoopReach.COLLEAGUE, LoopReach.EGRESS})
        assert dms_the_owner.tags == ("egress", "colleague", "deterministic")

    def test_no_registered_loop_reaches_a_colleague_without_egress(self) -> None:
        reaches = {loop.name: loop.reach for loop in iter_loops()}
        offenders = [
            name for name, reach in reaches.items() if LoopReach.COLLEAGUE in reach and LoopReach.EGRESS not in reach
        ]
        assert offenders == []

    def test_filtering_by_egress_catches_every_colleague_loop(self) -> None:
        colleague = {loop.name for loop in iter_loops() if LoopReach.COLLEAGUE in loop.reach}
        egress = {loop.name for loop in iter_loops() if LoopReach.EGRESS in loop.reach}
        assert colleague
        assert colleague <= egress

    def test_shipped_away_gate_is_a_subset_of_the_colleague_tag(self) -> None:
        """A loop the shipped away-gate suppresses must be tagged as reaching a person."""
        colleague = {loop.name for loop in iter_loops() if LoopReach.COLLEAGUE in loop.reach}
        away_gated = {spec.name for spec in DEFAULT_LOOPS if spec.colleague_facing}
        assert away_gated
        assert away_gated <= colleague


class DeterministicClaimTestCase(TestCase):
    """A ``deterministic`` claim is cross-checked against real dispatch behaviour."""

    def test_agent_dispatch_vocabulary_is_populated(self) -> None:
        assert len(agent_dispatch_vocabulary()) > _MIN_DERIVED_AI_LOOPS

    def test_no_deterministic_loop_reaches_an_agent(self) -> None:
        claimed_pure = [loop for loop in iter_loops() if loop.determinism is LoopDeterminism.DETERMINISTIC]
        evidence_by_name = {loop.name: _evidence(loop) for loop in claimed_pure}
        assert {name: found for name, found in evidence_by_name.items() if found} == {}

    def test_every_loop_with_dispatch_evidence_is_declared_ai(self) -> None:
        derived = {loop.name for loop in iter_loops() if _evidence(loop)}
        declared = {loop.name for loop in iter_loops() if loop.determinism is LoopDeterminism.AI}
        assert len(derived) >= _MIN_DERIVED_AI_LOOPS
        assert derived <= declared

    def test_evidence_names_the_route_it_found(self) -> None:
        review = next(loop for loop in iter_loops() if loop.name == "review")
        assert "reviewer_pr.new_sha" in _evidence(review)
