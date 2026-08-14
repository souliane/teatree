"""The ``0059`` forward makes every persisted hand-off reachable, and keeps one per author (#4194).

Four rows on the reported box were addressed to ``"loop-runner"`` — the ``t3
worker``'s durable principal, an id no receiving session can ever have — so they
were claimable by nobody while counting as pending. Others had fanned out: one
session wrote three rows in fourteen minutes, each superseding the last in prose.

What makes the backfill safe is pinned here: it touches only UNCLAIMED rows, it
CONCATENATES the duplicates rather than choosing a winner (those authors never
opted into the absorb contract), it never merges across authors, and the
constraints it then adds hold on the deployed shape.

Driven through the real migration executor from ``0058``, which is the only run
that proves it.
"""

import importlib

import pytest
from django.core.management import call_command
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase
from django.utils import timezone

from teatree.core.session_identity import LOOP_RUNNER_SESSION_ID

_BEFORE = ("core", "0058_merge_vendor_sync_and_availability_removal")
_AFTER = ("core", "0059_one_unclaimed_handover_per_session")


def test_the_migrations_frozen_literal_matches_the_runtime_constant() -> None:
    """A migration may not import runtime code, so the two are pinned instead of shared."""
    module = importlib.import_module(f"teatree.core.migrations.{_AFTER[1]}")
    assert module._LOOP_RUNNER_SESSION_ID == LOOP_RUNNER_SESSION_ID


@pytest.mark.timeout(240)
class TestOneUnclaimedHandoverPerSession(TransactionTestCase):
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

    @staticmethod
    def _forward(executor: MigrationExecutor) -> None:
        executor.loader.build_graph()
        executor.migrate([_AFTER])

    def test_the_four_measured_loop_runner_rows_become_claimable(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        pks = [
            model.objects.create(from_session=f"author-{index}", to_session="loop-runner", payload=f"state-{index}").pk
            for index in range(4)
        ]

        self._forward(executor)

        after = self._model(executor, _AFTER)
        for pk in pks:
            row = after.objects.get(pk=pk)
            assert row.to_session == "", "a slot alias is not an id any receiver can have — park it"
        from teatree.core.models import SessionHandover  # noqa: PLC0415 — the real manager, post-forward

        assert SessionHandover.objects.claimable_for("a-starting-session").count() == 4

    def test_duplicate_unclaimed_rows_collapse_without_losing_a_payload(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        now = timezone.now()
        rows = [
            model.objects.create(
                from_session="busy",
                to_session="",
                payload=f"payload-{index}",
                created_at=now.replace(microsecond=index),
            )
            for index in range(3)
        ]

        self._forward(executor)

        after = self._model(executor, _AFTER)
        survivors = list(after.objects.filter(from_session="busy"))
        assert len(survivors) == 1
        survivor = survivors[0]
        assert survivor.pk == min(row.pk for row in rows), "the lowest pk survives"
        for index in range(3):
            assert f"payload-{index}" in survivor.payload, "no author's state may be discarded"
        assert survivor.payload.index("payload-0") < survivor.payload.index("payload-2"), "oldest first"
        assert "Hand-off 1 of 3" in survivor.payload, "each is fenced so three narratives never read as one"
        assert survivor.created_at == rows[-1].created_at, "created_at is when the current payload was written"

    def test_claimed_rows_are_untouched(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        claimed = model.objects.create(
            from_session="s1",
            to_session="loop-runner",
            payload="delivered",
            claimed_at=timezone.now(),
            claimed_by="s2",
        )
        sibling = model.objects.create(
            from_session="s1", to_session="s1", payload="also delivered", claimed_at=timezone.now(), claimed_by="s2"
        )

        self._forward(executor)

        after = self._model(executor, _AFTER)
        assert after.objects.get(pk=claimed.pk).to_session == "loop-runner"
        assert after.objects.get(pk=sibling.pk).payload == "also delivered"

    def test_rows_from_different_authors_are_never_merged(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        first = model.objects.create(from_session="a", to_session="", payload="FROM-A")
        second = model.objects.create(from_session="b", to_session="", payload="FROM-B")

        self._forward(executor)

        after = self._model(executor, _AFTER)
        assert after.objects.get(pk=first.pk).payload == "FROM-A"
        assert after.objects.get(pk=second.pk).payload == "FROM-B"

    def test_a_self_addressed_row_is_parked_and_then_unrepresentable(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        stranded = model.objects.create(from_session="s1", to_session="s1", payload="real state")

        self._forward(executor)

        after = self._model(executor, _AFTER)
        assert after.objects.get(pk=stranded.pk).to_session == ""
        with pytest.raises(IntegrityError), transaction.atomic():
            after.objects.create(from_session="s2", to_session="s2", payload="P")

    def test_a_duplicate_unclaimed_insert_is_refused_after_the_forward(self) -> None:
        executor = self._rewind()
        model = self._model(executor, _BEFORE)
        model.objects.create(from_session="a", to_session="b", payload="FIRST")

        self._forward(executor)

        after = self._model(executor, _AFTER)
        with pytest.raises(IntegrityError), transaction.atomic():
            after.objects.create(from_session="a", to_session="b", payload="SIBLING")
