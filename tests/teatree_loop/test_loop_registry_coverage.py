"""Loop registry-coverage conformance — every domain/global scanner has a live MiniLoop (#22, #23).

The autonomy layer's recurring failure is *opt-in code that nothing on the live
per-loop fan-out consumes*: ``Domain.ISSUE_DISPOSITION`` had no MiniLoop and
``backlog_sweep`` had no loop package, so their opt-in flags toggled nothing
(#22); and the two single-overlay messaging builders diverged from
``_messaging_jobs_for_backend`` (#23). These lanes make each seam structural — a
new ``Domain`` with no consuming MiniLoop, a new ``build_default_jobs`` global
factory with no MiniLoop, or a MiniLoop with no seed row fails CI instead of
shipping dead.
"""

import contextlib
import inspect
import re
from typing import Any
from unittest.mock import MagicMock, patch

import teatree.loop.global_scanner_factories as gsf
from teatree.loop.domain_jobs import _messaging_jobs_for_backend, single_overlay_messaging_jobs
from teatree.loop.global_scanner_factories import build_default_jobs
from teatree.loop.job_identity import PER_OVERLAY_DOMAINS, Domain
from teatree.loop.scanners import AskUserQuestionReplyScanner
from teatree.loops.backlog_sweep.loop import MINI_LOOP as BACKLOG_SWEEP_LOOP
from teatree.loops.inbox import loop as inbox_loop
from teatree.loops.inbox.loop import MINI_LOOP as INBOX_LOOP
from teatree.loops.issue_disposition.loop import MINI_LOOP as ISSUE_DISPOSITION_LOOP
from teatree.loops.registry import iter_loops
from teatree.loops.seed import DEFAULT_LOOPS

_FACTORY_CALL = re.compile(r"\b(_[a-z][a-z0-9_]*_scanner)\(\)")


def _returns_none(*_args: object, **_kwargs: object) -> None:
    return None


def _global_factory_calls(fn: object) -> set[str]:
    """Zero-arg ``_*_scanner()`` global-factory calls in *fn*'s source."""
    return set(_FACTORY_CALL.findall(inspect.getsource(fn)))


def _domains_requested_by_every_miniloop() -> set[Domain]:
    """Every ``Domain`` any registry MiniLoop asks ``jobs_for_domain`` for.

    ``jobs_for_domain`` is replaced by a recorder and the global cadence
    factories are stubbed to ``None`` so invoking every ``build_jobs`` with a
    stub backend roster is fast and side-effect-free — the sweep observes which
    domains are actually requested, so a ``Domain`` no MiniLoop consumes is
    caught structurally, not by a hand-maintained list.
    """
    requested: set[Domain] = set()

    def _record(domain: Domain, _backend: object = None, **_kwargs: object) -> list[object]:
        requested.add(domain)
        return []

    factory_names = [name for name in vars(gsf) if name.endswith("_scanner") and callable(getattr(gsf, name))]
    with contextlib.ExitStack() as stack:
        for name in factory_names:
            stack.enter_context(patch.object(gsf, name, _returns_none))
        stack.enter_context(patch("teatree.loop.domain_jobs.jobs_for_domain", side_effect=_record))
        for loop in iter_loops():
            loop.build_jobs(
                backends=[object()],
                host=None,
                messaging=None,
                notion_client=None,
                ready_labels=(),
            )
    return requested


class TestPerOverlayDomainCoverage:
    """Every ``PER_OVERLAY_DOMAINS`` member is consumed by some registry MiniLoop (#22)."""

    def test_every_per_overlay_domain_has_a_consuming_miniloop(self) -> None:
        requested = _domains_requested_by_every_miniloop()
        missing = set(PER_OVERLAY_DOMAINS) - requested
        assert not missing, f"per-overlay Domain(s) no MiniLoop consumes (dead opt-in code): {sorted(missing)}"

    def test_cardinality_floor_anti_vacuity(self) -> None:
        # A refactor that empties PER_OVERLAY_DOMAINS must not make the lane
        # vacuously green — the floor sits safely below the real cardinality.
        assert len(PER_OVERLAY_DOMAINS) >= 10, PER_OVERLAY_DOMAINS


class TestGlobalFactoryCoverage:
    """Every ``build_default_jobs`` global scanner factory is reachable from a MiniLoop (#22).

    The legacy monolithic ``build_default_jobs`` fan-out and the per-loop
    MiniLoop fan-out must wire the SAME global cadence scanners — a factory
    ``build_default_jobs`` wires but no MiniLoop wraps would silently vanish from
    the live per-loop path.
    """

    #: Global factories deliberately reachable via NO dedicated MiniLoop. Empty:
    #: every ``build_default_jobs`` global cadence factory has a MiniLoop home.
    _ALLOWLIST: frozenset[str] = frozenset()

    def test_every_global_factory_has_a_consuming_miniloop(self) -> None:
        producers = _global_factory_calls(build_default_jobs)
        consumers: set[str] = set()
        for loop in iter_loops():
            consumers |= _global_factory_calls(loop.build_jobs)
        missing = producers - consumers - self._ALLOWLIST
        assert not missing, f"build_default_jobs global factor(y/ies) no MiniLoop wraps: {sorted(missing)}"

    def test_cardinality_floor_anti_vacuity(self) -> None:
        producers = _global_factory_calls(build_default_jobs)
        assert len(producers) >= 8, producers


class TestMiniLoopSeedCoverage:
    """Every registry MiniLoop is seed-covered and every seed row has a MiniLoop (#22, #2584).

    A MiniLoop package with no ``DEFAULT_LOOPS`` seed row is a loop the per-loop
    fan-out can never admit (no ``Loop`` row to enable/schedule); a seed row with
    no MiniLoop is an orphan the fan-out can never dispatch. Both revived loops
    (``issue_disposition`` / ``backlog_sweep``) must appear on both sides.
    """

    def test_registry_and_seed_cover_each_other(self) -> None:
        registry = {loop.name for loop in iter_loops()}
        seeded = {spec.name for spec in DEFAULT_LOOPS}
        assert registry == seeded, (
            f"registry/seed mismatch — MiniLoop(s) with no seed row: {sorted(registry - seeded)}; "
            f"seed row(s) with no MiniLoop: {sorted(seeded - registry)}"
        )

    def test_revived_loops_are_registered_and_seeded(self) -> None:
        registry = {loop.name for loop in iter_loops()}
        seeded = {spec.name for spec in DEFAULT_LOOPS}
        for name in ("issue_disposition", "backlog_sweep"):
            assert name in registry, f"{name} MiniLoop is not registered"
            assert name in seeded, f"{name} has no seed row"


def _messaging_scanner_types(jobs: list[Any]) -> set[type]:
    return {type(job.scanner) for job in jobs}


class TestSingleOverlayMessagingParity:
    """The single-overlay messaging builder is the ONE SSOT both callers use (#23).

    The two single-overlay builders (inbox mini-loop, ``build_default_jobs``) had
    each dropped ``AskUserQuestionReplyScanner`` and diverged from
    ``_messaging_jobs_for_backend``. Now both import the ONE
    ``single_overlay_messaging_jobs`` builder, so the sets cannot re-diverge.
    """

    @staticmethod
    def _stub_messaging() -> Any:
        return MagicMock()

    def test_shared_builder_equals_per_overlay_inbound_set_and_includes_ask_reply(self) -> None:
        messaging = self._stub_messaging()
        backend = MagicMock()
        backend.messaging = messaging
        shared = _messaging_scanner_types(single_overlay_messaging_jobs(messaging))
        per_overlay = _messaging_scanner_types(
            _messaging_jobs_for_backend(backend, "", include_review_nag=False),
        )
        assert shared == per_overlay, (shared, per_overlay)
        assert AskUserQuestionReplyScanner in shared

    def test_single_overlay_builder_is_overlay_scoped_empty(self) -> None:
        # The single-overlay projection tags every job with overlay="".
        jobs = single_overlay_messaging_jobs(self._stub_messaging())
        assert {job.overlay for job in jobs} == {""}

    def test_both_callers_go_through_the_shared_builder(self) -> None:
        # Structural: neither single-overlay caller may re-fork the inbound set —
        # both must import the ONE builder so they cannot silently diverge.
        assert "single_overlay_messaging_jobs" in inspect.getsource(inbox_loop._build_jobs)
        assert "single_overlay_messaging_jobs" in inspect.getsource(build_default_jobs)

    def test_inbox_single_overlay_output_is_the_shared_builder(self) -> None:
        messaging = self._stub_messaging()
        inbox_jobs = INBOX_LOOP.build_jobs(messaging=messaging)
        assert _messaging_scanner_types(inbox_jobs) == _messaging_scanner_types(
            single_overlay_messaging_jobs(messaging),
        )


class TestRevivedLoopsDefaultOff:
    """Both revived loops fan out nothing unless their opt-in flag is set (#22).

    Anti-vacuity twin: each also produces its job once its gate opens, so the
    default-OFF assertion is not vacuously always-empty.
    """

    @staticmethod
    def _stub_backend() -> Any:
        backend = MagicMock()
        backend.name = "stub-overlay"
        backend.overlay = None
        return backend

    def test_backlog_sweep_default_off_produces_no_jobs(self) -> None:
        # Real default config: backlog_sweep_disabled=True → the builder returns
        # None → no job.
        assert BACKLOG_SWEEP_LOOP.build_jobs() == []

    def test_backlog_sweep_runs_when_enabled(self) -> None:
        fake = MagicMock()
        fake.name = "backlog_sweep"
        with patch("teatree.loop.global_scanner_factories._backlog_sweep_scanner", return_value=fake):
            jobs = BACKLOG_SWEEP_LOOP.build_jobs()
        assert [job.scanner for job in jobs] == [fake]
        assert jobs[0].overlay == ""

    def test_issue_disposition_default_off_produces_no_jobs(self) -> None:
        # Real default config: auto_disposition_enabled=False → the gate returns
        # None → no job.
        assert ISSUE_DISPOSITION_LOOP.build_jobs(backends=[self._stub_backend()]) == []

    def test_issue_disposition_runs_when_gate_opens(self) -> None:
        backend = self._stub_backend()
        fake = MagicMock()
        fake.name = "issue_disposition"
        with patch("teatree.loop.domain_optional_scanner_jobs._issue_disposition_scanner_for", return_value=fake):
            jobs = ISSUE_DISPOSITION_LOOP.build_jobs(backends=[backend])
        assert [job.scanner for job in jobs] == [fake]
