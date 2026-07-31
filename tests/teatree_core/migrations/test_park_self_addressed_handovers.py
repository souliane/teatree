"""The ``0048`` forward parks the hand-offs no session could ever claim (#3821).

Refusing the degenerate row at creation protects new hand-offs; it does nothing
for rows already persisted, and each of those holds a real payload while counting
as pending in every unclaimed-queue tally. Parking (``to_session = ""``) is the
one move that makes such a row reachable again without loosening the exclusion
that keeps a session from re-claiming its own snapshot.

The tests pin what makes the backfill safe: it touches only UNCLAIMED
self-addressed rows, leaves every legitimate row alone, and the parked row is
genuinely claimable afterwards.

Driven through the real migration executor from ``0047``, which is the only run
that proves the deployed shape.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

_BEFORE = ("core", "0047_alter_pullrequest_state")
_AFTER = ("core", "0048_park_self_addressed_handovers")


@pytest.mark.timeout(240)
class TestSelfAddressedHandoversAreParked(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    def _rewind(self) -> MigrationExecutor:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        return executor

    @staticmethod
    def _model(executor: MigrationExecutor, state: tuple[str, str]):
        return executor.loader.project_state(state).apps.get_model("core", "SessionHandover")

    def test_an_unclaimed_self_addressed_row_is_parked(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        stranded = model.objects.create(from_session="s1", to_session="s1", payload="real state")

        executor.loader.build_graph()
        executor.migrate([_AFTER])

        after = self._model(executor, _AFTER).objects.get(pk=stranded.pk)
        assert after.to_session == "", "an unreachable row must become claimable by the next session"
        assert after.from_session == "s1", "the author is unchanged — only the address was impossible"
        assert after.payload == "real state", "the payload is the whole point of un-stranding it"

    def test_a_claimed_self_addressed_row_is_left_alone(self) -> None:
        """Already delivered — re-parking it would re-inject state a session has seen."""
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        claimed = model.objects.create(
            from_session="s1", to_session="s1", payload="state", claimed_at=timezone.now(), claimed_by="s1"
        )

        executor.loader.build_graph()
        executor.migrate([_AFTER])

        assert self._model(executor, _AFTER).objects.get(pk=claimed.pk).to_session == "s1"

    def test_legitimate_rows_are_untouched(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        addressed = model.objects.create(from_session="s1", to_session="s2", payload="state")
        parked = model.objects.create(from_session="s1", to_session="", payload="state")

        executor.loader.build_graph()
        executor.migrate([_AFTER])

        after = self._model(executor, _AFTER)
        assert after.objects.get(pk=addressed.pk).to_session == "s2"
        assert after.objects.get(pk=parked.pk).to_session == ""
