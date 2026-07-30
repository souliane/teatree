"""``PendingPullRequest`` — the obligation ledger a pre-push PR deferral writes."""

import dataclasses
import typing

import django.test

from teatree.core.backend_protocols import PullRequestSpec
from teatree.core.models import PendingPullRequest
from teatree.core.models.pending_pull_request import MAX_DRAIN_ATTEMPTS, SerializedPrSpec
from tests.factories import serialized_pr_spec


def test_serialized_spec_mirrors_every_pull_request_spec_field() -> None:
    """The model layer cannot import the dataclass, so parity is pinned instead of inherited."""
    assert set(typing.get_type_hints(SerializedPrSpec)) == {f.name for f in dataclasses.fields(PullRequestSpec)}


class PendingPullRequestTestCase(django.test.TestCase):
    @staticmethod
    def _owe(*, reason: str = "branch not on remote yet", spec: SerializedPrSpec | None = None) -> PendingPullRequest:
        return PendingPullRequest.objects.owe(repo_path="/w/clone", branch="feat/orphan", reason=reason, spec=spec)

    def test_re_deferral_refreshes_the_obligation_without_resetting_its_age(self) -> None:
        row = self._owe()
        row.record_failed_drain(error="still not on remote")

        self._owe(reason="remote ref not yet current", spec=serialized_pr_spec("feat: the feature"))

        assert PendingPullRequest.objects.count() == 1
        refreshed = PendingPullRequest.objects.get(pk=row.pk)
        assert refreshed.reason == "remote ref not yet current"
        assert refreshed.intended_title == "feat: the feature"
        assert refreshed.drain_attempts == 1

    def test_a_spec_less_re_deferral_keeps_the_spec_already_recorded(self) -> None:
        self._owe(spec=serialized_pr_spec("feat: the feature"))

        self._owe()

        assert PendingPullRequest.objects.get(branch="feat/orphan").intended_title == "feat: the feature"

    def test_overdue_selects_only_obligations_past_the_drain_budget(self) -> None:
        row = self._owe()
        for _ in range(MAX_DRAIN_ATTEMPTS - 1):
            row.record_failed_drain()
        assert not PendingPullRequest.objects.overdue().exists()

        row.record_failed_drain()

        assert row.is_overdue
        assert list(PendingPullRequest.objects.overdue()) == [row]

    def test_discharge_removes_the_obligation(self) -> None:
        self._owe()

        PendingPullRequest.objects.discharge(repo_path="/w/clone", branch="feat/orphan")

        assert not PendingPullRequest.objects.exists()
