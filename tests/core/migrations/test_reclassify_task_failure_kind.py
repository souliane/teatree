# test-path: cross-cutting
"""0059 re-reads every stored reason through the current vocabulary.

The kind is a pure function of the reason, so a row naming a verdict the classifier no
longer reaches is stale data, not history. Each case is paired with what must NOT move,
so a future "just stamp a default" cannot pass this file.
"""

import importlib

from django.apps import apps
from django.test import TestCase

from teatree.core.modelkit.task_failure_taxonomy import FailureKind
from teatree.core.models import Session, Task, TaskAttempt, Ticket

_MIGRATION = importlib.import_module("teatree.core.migrations.0075_task_failure_kind_credential_missing")

_MISSING_CREDENTIAL = (
    "no ANTHROPIC_API_KEY credential available and no OAuth `pass` path is configured. "
    "Set ANTHROPIC_API_KEY in the environment, or configure a per-account `pass` entry."
)


def _failed_task(*, reason: str, kind: str) -> Task:
    ticket = Ticket.objects.create(role=Ticket.Role.AUTHOR, state=Ticket.State.STARTED)
    session = Session.objects.create(ticket=ticket, agent_id="coding")
    return Task.objects.create(
        ticket=ticket,
        session=session,
        phase="coding",
        status=Task.Status.FAILED,
        failure_reason=reason,
        failure_kind=kind,
    )


class TestReclassifyReadsTheStoredReason(TestCase):
    def test_an_unconfigured_credential_stops_reading_as_unclassified(self) -> None:
        task = _failed_task(reason=_MISSING_CREDENTIAL, kind=FailureKind.UNCLASSIFIED)

        _MIGRATION.reclassify(apps, None)

        task.refresh_from_db()
        assert task.failure_kind == FailureKind.CREDENTIAL_MISSING

    def test_an_attempt_row_is_reclassified_from_its_own_error(self) -> None:
        task = _failed_task(reason=_MISSING_CREDENTIAL, kind=FailureKind.UNCLASSIFIED)
        attempt = TaskAttempt.objects.create(task=task, error=_MISSING_CREDENTIAL)
        TaskAttempt.objects.filter(pk=attempt.pk).update(failure_kind=FailureKind.UNCLASSIFIED)

        _MIGRATION.reclassify(apps, None)

        attempt.refresh_from_db()
        assert attempt.failure_kind == FailureKind.CREDENTIAL_MISSING

    def test_a_reason_the_vocabulary_still_names_the_same_way_is_untouched(self) -> None:
        task = _failed_task(reason="lease_expired: reaped after 3600s", kind=FailureKind.LEASE_EXPIRED)

        _MIGRATION.reclassify(apps, None)

        task.refresh_from_db()
        assert task.failure_kind == FailureKind.LEASE_EXPIRED

    def test_a_blank_reason_gains_no_invented_kind(self) -> None:
        task = _failed_task(reason="", kind="")

        _MIGRATION.reclassify(apps, None)

        task.refresh_from_db()
        assert task.failure_kind == ""
