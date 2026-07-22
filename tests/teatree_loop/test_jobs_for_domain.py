"""``jobs_for_domain`` partitions the per-overlay fan-out exhaustively and disjointly (#1482).

The per-overlay scanner fan-out (:func:`teatree.loop.domain_jobs._jobs_for_overlay_backend`)
is the single source of which scanners run for one overlay. ``jobs_for_domain``
slices it by :class:`Domain` so the mini-loops consume one typed seam instead of
reaching into ``domain_jobs`` privates. These tests pin the seam's two structural
invariants: every legacy per-overlay scanner is owned by exactly one domain
(EXHAUSTIVE), and no scanner is owned by two domains (DISJOINT). A dropped domain
turns the exhaustiveness assertion RED.
"""

import dataclasses
from collections import Counter
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from django.test import TestCase

from teatree.config import UserSettings
from teatree.core.backend_factory import OverlayBackends
from teatree.core.backend_protocols import CodeHostBackend, MessagingBackend
from teatree.loop.domain_jobs import _jobs_for_overlay_backend, jobs_for_domain
from teatree.loop.job_identity import PER_OVERLAY_DOMAINS, Domain


def _signature(job: Any) -> tuple[Any, ...]:
    scanner = job.scanner
    fields = sorted(f.name for f in dataclasses.fields(scanner)) if dataclasses.is_dataclass(scanner) else []
    args = tuple((name, _arg_value(getattr(scanner, name))) for name in fields)
    return (type(scanner).__name__, getattr(scanner, "name", ""), job.overlay, args)


def _arg_value(value: object) -> object:
    if isinstance(value, MagicMock):
        return id(value)
    if isinstance(value, (list, tuple)):
        return tuple(_arg_value(item) for item in value)
    if isinstance(value, (str, int, float, bool, bytes, type(None))):
        return value
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return (
            type(value).__name__,
            tuple((f.name, _arg_value(getattr(value, f.name))) for f in dataclasses.fields(value)),
        )
    return type(value).__name__


class JobsForDomainPartitionTestCase(TestCase):
    """``jobs_for_domain`` slices the per-overlay fan-out exhaustively + disjointly."""

    @staticmethod
    def _backend() -> OverlayBackends:
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

    def test_per_overlay_sum_equals_legacy_builder(self) -> None:
        backend = self._backend()
        legacy = _jobs_for_overlay_backend(backend, all_backends=(backend,))
        partitioned: list[Any] = []
        for domain in PER_OVERLAY_DOMAINS:
            partitioned.extend(jobs_for_domain(domain, backend, all_backends=(backend,)))
        assert sorted(map(_signature, partitioned), key=repr) == sorted(map(_signature, legacy), key=repr)

    def test_partition_is_disjoint(self) -> None:
        backend = self._backend()
        counts: Counter[tuple[Any, ...]] = Counter()
        for domain in PER_OVERLAY_DOMAINS:
            for job in jobs_for_domain(domain, backend, all_backends=(backend,)):
                counts[_signature(job)] += 1
        double_emitted = {sig for sig, n in counts.items() if n > 1}
        assert not double_emitted

    def test_partition_is_exhaustive_no_scanner_dropped(self) -> None:
        backend = self._backend()
        legacy = {_signature(j) for j in _jobs_for_overlay_backend(backend, all_backends=(backend,))}
        owned: set[tuple[Any, ...]] = set()
        for domain in PER_OVERLAY_DOMAINS:
            owned |= {_signature(j) for j in jobs_for_domain(domain, backend, all_backends=(backend,))}
        assert legacy <= owned, f"legacy scanners owned by no domain: {legacy - owned}"

    def test_dropping_one_domain_breaks_exhaustiveness(self) -> None:
        backend = self._backend()
        legacy = {_signature(j) for j in _jobs_for_overlay_backend(backend, all_backends=(backend,))}
        owned: set[tuple[Any, ...]] = set()
        for domain in [d for d in PER_OVERLAY_DOMAINS if d is not Domain.TICKETS]:
            owned |= {_signature(j) for j in jobs_for_domain(domain, backend, all_backends=(backend,))}
        assert not legacy <= owned

    def test_dispatch_domain_returns_global_set(self) -> None:
        backend = self._backend()
        dispatch_jobs = jobs_for_domain(Domain.DISPATCH, backend)
        names = {job.scanner.name for job in dispatch_jobs}
        assert names == {
            "pending_tasks",
            "incoming_events",
            "outbound_audit",
            "undelivered_notify",
            "deferred_question_poster",
            "waiting_digest",
            "work_state",
        }
        assert all(job.overlay == "" for job in dispatch_jobs)

    def test_dispatch_excluded_from_per_overlay_domains(self) -> None:
        assert Domain.DISPATCH not in PER_OVERLAY_DOMAINS

    def test_per_overlay_domain_requires_backend(self) -> None:
        with pytest.raises(ValueError, match="per-overlay domain"):
            jobs_for_domain(Domain.TICKETS, None)


class JobsForDomainTaskSweepTestCase(TestCase):
    """``task_sweep`` — emitted by the legacy per-overlay builder — is owned by a domain (#1482).

    The pre-seam mini-loops dropped ``task_sweep`` (no mini-loop reproduced
    :func:`teatree.loop.scanner_factories._task_sweep_scanner_for`). The exhaustive
    partition assigns it to ``Domain.TICKETS`` (it verifies overlay-scoped Task
    rows, the same surface as the active/stale ticket scanners).
    """

    @staticmethod
    def _backend_with_python_overlay() -> OverlayBackends:
        overlay = MagicMock()
        overlay.config.get_review_broadcast_channels.return_value = []
        overlay.metadata.get_followup_repos.return_value = []
        overlay.get_workspace_repos.return_value = []
        return OverlayBackends(
            name="teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=(),
            overlay=overlay,
        )

    def test_task_sweep_owned_by_tickets_domain(self) -> None:
        backend = self._backend_with_python_overlay()
        tickets_names = {job.scanner.name for job in jobs_for_domain(Domain.TICKETS, backend)}
        assert "task_sweep" in tickets_names


class PrSweepShipDomainTestCase(TestCase):
    """The auto-merge PR-sweep is owned by SHIP, not REVIEW (#3244).

    The review loop is ``colleague_facing`` and is skipped under
    ``autonomous_away``; ship is not. Moving the sweep to ship keeps the merge
    path alive when the operator is away. A REVIEW-domain sweep would starve it.
    """

    @staticmethod
    def _backend_with_sweep() -> OverlayBackends:
        overlay = MagicMock()
        overlay.config.get_review_broadcast_channels.return_value = []
        overlay.config.get_review_channel.return_value = ("", "")
        overlay.metadata.get_followup_repos.return_value = ["souliane/teatree"]
        overlay.get_workspace_repos.return_value = []
        return OverlayBackends(
            name="t3-teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=("ready",),
            overlay=overlay,
        )

    @staticmethod
    def _scanner_type_names(jobs: list[Any]) -> set[str]:
        return {type(job.scanner).__name__ for job in jobs}

    def test_ship_domain_owns_the_pr_sweep(self) -> None:
        backend = self._backend_with_sweep()
        ship = self._scanner_type_names(jobs_for_domain(Domain.SHIP, backend, all_backends=(backend,)))
        assert "PrSweepScanner" in ship

    def test_review_domain_does_not_own_the_pr_sweep(self) -> None:
        backend = self._backend_with_sweep()
        review = self._scanner_type_names(jobs_for_domain(Domain.REVIEW, backend, all_backends=(backend,)))
        assert "PrSweepScanner" not in review


_SETTINGS_PATCH_TARGET = "teatree.loop.scanner_factories._effective_settings_for_overlay"


class ReviewDomainUnifiedIntakeTestCase(TestCase):
    """``Domain.REVIEW`` is the SINGLE review intake — self + colleague (#3569).

    Self-authored PRs are ALWAYS admitted (the ``ClaudeSelfPrReviewScanner``);
    colleague PRs are admitted only when ``admit_colleague_prs_to_board`` is ON
    (the ``ReviewerPrsScanner``). Both feed the SAME ``reviewing`` → ``t3:reviewer``
    gate. There is no separate self_review domain and codex is not wired here.
    """

    @staticmethod
    def _backend() -> OverlayBackends:
        overlay = MagicMock()
        overlay.config.get_github_token.return_value = ""
        overlay.config.get_review_broadcast_channels.return_value = []
        overlay.config.get_review_channel.return_value = ("", "")
        overlay.metadata.get_followup_repos.return_value = ["souliane/teatree"]
        overlay.get_workspace_repos.return_value = []
        return OverlayBackends(
            name="t3-teatree",
            hosts=(MagicMock(spec=CodeHostBackend),),
            messaging=None,
            ready_labels=("ready",),
            overlay=overlay,
        )

    def _review_names(self, backend: OverlayBackends) -> set[str]:
        return {job.scanner.name for job in jobs_for_domain(Domain.REVIEW, backend, all_backends=(backend,))}

    def test_self_pr_scanner_always_admitted_even_when_colleague_off(self) -> None:
        backend = self._backend()
        with patch(_SETTINGS_PATCH_TARGET, return_value=UserSettings(admit_colleague_prs_to_board=False)):
            names = self._review_names(backend)
        assert "self_pr_review" in names

    def test_colleague_scanner_admitted_only_when_setting_on(self) -> None:
        backend = self._backend()
        with patch(_SETTINGS_PATCH_TARGET, return_value=UserSettings(admit_colleague_prs_to_board=True)):
            assert "reviewer_prs" in self._review_names(backend)
        with patch(_SETTINGS_PATCH_TARGET, return_value=UserSettings(admit_colleague_prs_to_board=False)):
            assert "reviewer_prs" not in self._review_names(backend)

    def test_codex_scanner_never_wired_into_review(self) -> None:
        backend = self._backend()
        with patch(_SETTINGS_PATCH_TARGET, return_value=UserSettings(admit_colleague_prs_to_board=True)):
            assert "codex_review" not in self._review_names(backend)

    def test_no_separate_self_review_domain(self) -> None:
        assert not hasattr(Domain, "SELF_REVIEW")


class IssueImplementerDomainPartitionTestCase(TestCase):
    """``ISSUE_IMPLEMENTER`` joins the partition without breaking it (#1553).

    The domain is default-OFF: :func:`_issue_implementer_scanner_for`
    returns ``None`` unless an overlay opts in, so the per-overlay sum stays
    byte-for-byte equal to the legacy builder by default (the parity
    invariant). When enabled it owns exactly the one scanner the partition
    seam emits — the single source both fan-out paths consume.
    """

    @staticmethod
    def _backend() -> OverlayBackends:
        host = MagicMock(spec=CodeHostBackend)
        return OverlayBackends(
            name="teatree",
            hosts=(host,),
            messaging=MagicMock(spec=MessagingBackend),
            ready_labels=("ready",),
            identities=("alice",),
        )

    def test_member_of_per_overlay_partition(self) -> None:
        assert Domain.ISSUE_IMPLEMENTER in PER_OVERLAY_DOMAINS

    def test_disabled_slice_is_empty_so_partition_sum_is_unchanged(self) -> None:
        backend = self._backend()
        with patch(_SETTINGS_PATCH_TARGET, return_value=UserSettings()):
            assert jobs_for_domain(Domain.ISSUE_IMPLEMENTER, backend) == []
            partitioned: list[Any] = []
            for domain in PER_OVERLAY_DOMAINS:
                partitioned.extend(jobs_for_domain(domain, backend, all_backends=(backend,)))
            legacy = _jobs_for_overlay_backend(backend, all_backends=(backend,))
        assert sorted(map(_signature, partitioned), key=repr) == sorted(map(_signature, legacy), key=repr)

    def test_enabled_slice_owns_exactly_the_issue_implementer_scanner(self) -> None:
        backend = self._backend()
        with patch(
            _SETTINGS_PATCH_TARGET,
            return_value=UserSettings(issue_implementer_enabled=True, issue_implementer_label="auto-implement"),
        ):
            slice_jobs = jobs_for_domain(Domain.ISSUE_IMPLEMENTER, backend)
            legacy = _jobs_for_overlay_backend(backend, all_backends=(backend,))
        assert [job.scanner.name for job in slice_jobs] == ["issue_implementer"]
        # Enabled, the legacy aggregator carries exactly the one slice scanner —
        # both fan-out paths derive from this same partition slice (no divergence).
        legacy_ii = [j for j in legacy if j.scanner.name == "issue_implementer"]
        assert len(legacy_ii) == 1
        assert _signature(legacy_ii[0]) == _signature(slice_jobs[0])

    def test_enabled_partition_still_disjoint(self) -> None:
        backend = self._backend()
        with patch(
            _SETTINGS_PATCH_TARGET,
            return_value=UserSettings(issue_implementer_enabled=True, issue_implementer_label="auto-implement"),
        ):
            counts: Counter[tuple[Any, ...]] = Counter()
            for domain in PER_OVERLAY_DOMAINS:
                for job in jobs_for_domain(domain, backend, all_backends=(backend,)):
                    counts[_signature(job)] += 1
        assert not {sig for sig, n in counts.items() if n > 1}


class TriageAssessorDomainPartitionTestCase(TestCase):
    """``TRIAGE_ASSESSOR`` joins the partition without breaking it.

    Default-OFF: :func:`_triage_assessor_scanner_for` returns ``None`` unless an
    overlay opts in, so the per-overlay sum stays byte-for-byte equal to the legacy
    builder by default. When enabled it owns exactly the one scanner the partition
    seam emits — the single source both fan-out paths consume.
    """

    @staticmethod
    def _backend() -> OverlayBackends:
        host = MagicMock(spec=CodeHostBackend)
        return OverlayBackends(
            name="teatree",
            hosts=(host,),
            messaging=MagicMock(spec=MessagingBackend),
            ready_labels=("ready",),
            identities=("alice",),
        )

    def test_member_of_per_overlay_partition(self) -> None:
        assert Domain.TRIAGE_ASSESSOR in PER_OVERLAY_DOMAINS

    def test_disabled_slice_is_empty_so_partition_sum_is_unchanged(self) -> None:
        backend = self._backend()
        with patch(_SETTINGS_PATCH_TARGET, return_value=UserSettings()):
            assert jobs_for_domain(Domain.TRIAGE_ASSESSOR, backend) == []
            partitioned: list[Any] = []
            for domain in PER_OVERLAY_DOMAINS:
                partitioned.extend(jobs_for_domain(domain, backend, all_backends=(backend,)))
            legacy = _jobs_for_overlay_backend(backend, all_backends=(backend,))
        assert sorted(map(_signature, partitioned), key=repr) == sorted(map(_signature, legacy), key=repr)

    def test_enabled_slice_owns_exactly_the_triage_assessor_scanner(self) -> None:
        backend = self._backend()
        with patch(_SETTINGS_PATCH_TARGET, return_value=UserSettings(triage_assessor_enabled=True)):
            slice_jobs = jobs_for_domain(Domain.TRIAGE_ASSESSOR, backend)
            legacy = _jobs_for_overlay_backend(backend, all_backends=(backend,))
        assert [job.scanner.name for job in slice_jobs] == ["triage_assessor"]
        legacy_ta = [j for j in legacy if j.scanner.name == "triage_assessor"]
        assert len(legacy_ta) == 1
        assert _signature(legacy_ta[0]) == _signature(slice_jobs[0])
