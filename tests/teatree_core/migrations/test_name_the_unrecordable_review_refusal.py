"""The ``0108`` data migration re-names the refusals recorded before the name existed.

``failure_kind`` is a stored derivation, so the 53 historical rows sat at ``unclassified``
where the re-dispatch budget could not see them. Anti-vacuous: dropping the ``RunPython``
leaves them ``unclassified`` and the first test goes RED, and the last test proves the
backfill is scoped to rows the vocabulary genuinely had no name for rather than sweeping
every row that mentions the phrase.
"""

import pytest
from django.core.management import call_command
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase

_BEFORE = ("core", "0107_e2emandatoryrun_target")
_AFTER = ("core", "0108_review_unrecordable_failure_kind")

#: The wording the recorder carried through the whole measured window, before the
#: greppable prefix existed — what a historical row actually looks like.
_HISTORICAL = (
    "review verdict cannot be persisted: this review is answerable for souliane/teatree#4225 "
    "but no pull request head is recorded for it"
)


@pytest.mark.timeout(240)
class TestNameTheUnrecordableReviewRefusal(TransactionTestCase):
    def setUp(self) -> None:
        self.addCleanup(self._restore_head)

    @staticmethod
    def _restore_head() -> None:
        connection.close()
        call_command("migrate", "core", "--no-input", verbosity=0)

    @staticmethod
    def _seed_before(rows: tuple[tuple[str, str], ...]) -> None:
        executor = MigrationExecutor(connection)
        executor.migrate([_BEFORE])
        apps = executor.loader.project_state(_BEFORE).apps
        ticket = apps.get_model("core", "Ticket").objects.create(role="reviewer")
        session = apps.get_model("core", "Session").objects.create(ticket=ticket, agent_id="external-review")
        attempt_model = apps.get_model("core", "TaskAttempt")
        for failure_kind, error in rows:
            task = apps.get_model("core", "Task").objects.create(
                ticket=ticket, session=session, phase="reviewing", status="failed"
            )
            attempt_model.objects.create(task=task, error=error, failure_kind=failure_kind, exit_code=0)

    @staticmethod
    def _migrate_forward_and_read() -> list[tuple[str, str]]:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_AFTER])
        attempt = MigrationExecutor(connection).loader.project_state([_AFTER]).apps.get_model("core", "TaskAttempt")
        return [(row.failure_kind, row.error) for row in attempt.objects.order_by("pk")]

    @staticmethod
    def _migrate_back_and_read() -> list[str]:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate([_BEFORE])
        attempt = MigrationExecutor(connection).loader.project_state([_BEFORE]).apps.get_model("core", "TaskAttempt")
        return [row.failure_kind for row in attempt.objects.order_by("pk")]

    def test_a_historical_unclassified_refusal_is_renamed(self) -> None:
        self._seed_before((("unclassified", _HISTORICAL),))

        assert [kind for kind, _ in self._migrate_forward_and_read()] == ["review_unrecordable"]

    def test_the_rename_reverses(self) -> None:
        self._seed_before((("unclassified", _HISTORICAL),))
        self._migrate_forward_and_read()

        assert self._migrate_back_and_read() == ["unclassified"]

    def test_a_row_already_classified_as_something_else_is_untouched(self) -> None:
        self._seed_before(
            (
                ("unclassified", _HISTORICAL),
                ("recording_refused", f"review verdict recording refused: {_HISTORICAL}"),
            )
        )

        assert [kind for kind, _ in self._migrate_forward_and_read()] == [
            "review_unrecordable",
            "recording_refused",
        ]

    def test_an_unrelated_unclassified_row_is_untouched(self) -> None:
        self._seed_before((("unclassified", "something else entirely went wrong"),))

        assert [kind for kind, _ in self._migrate_forward_and_read()] == ["unclassified"]
