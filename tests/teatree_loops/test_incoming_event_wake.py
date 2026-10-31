"""The wake task and the app-ready registration of the ingest dispatch."""

from django.apps import apps
from django.tasks import TaskResultStatus
from django.test import TestCase, override_settings
from django_tasks_db.models import DBTaskResult

from teatree.core.models import IncomingEvent
from teatree.core.views import _webhook_persistence
from teatree.core.views._webhook_persistence import IngestionRecord
from teatree.loops.apps import DeferredIngestDispatch
from teatree.loops.incoming_event_wake import RegistryIngestDispatch, wake_incoming_event
from teatree.loops.timer_chains import LOOPS_QUEUE
from teatree.loops.timer_reconciler import WAKE_PRIORITY
from tests._incoming_event_handlers import TASKS_SETTING, registered


def record(**fields: str) -> IngestionRecord:
    defaults = {"source": "github", "idempotency_key": "github:x", "event_name": "pull_request_review"}
    return IngestionRecord(**({"action": "submitted"} | defaults | fields))


class TestAppReady(TestCase):
    def test_the_loops_app_registers_the_deferred_dispatch_on_the_ingest_seam(self) -> None:
        assert apps.get_app_config("loops").name == "teatree.loops"
        assert isinstance(_webhook_persistence._INGEST.dispatch, DeferredIngestDispatch)

    def test_the_deferred_dispatch_answers_from_the_live_handler_registry(self) -> None:
        with registered():
            assert DeferredIngestDispatch().handles(record()) is True
            assert DeferredIngestDispatch().handles(record(action="dismissed")) is False


class TestRegistryIngestDispatch(TestCase):
    def test_it_claims_exactly_the_registered_triple(self) -> None:
        with registered():
            assert RegistryIngestDispatch().handles(record()) is True
            assert RegistryIngestDispatch().handles(record(action="dismissed")) is False
            assert RegistryIngestDispatch().handles(record(event_name="issues")) is False
            assert RegistryIngestDispatch().handles(record(source="gitlab")) is False


@override_settings(USE_TZ=True, TASKS=TASKS_SETTING)
class TestWakeTask(TestCase):
    def test_wake_enqueues_one_top_priority_task_on_the_loops_queue(self) -> None:
        RegistryIngestDispatch().wake(7)

        row = DBTaskResult.objects.get(task_path=wake_incoming_event.module_path)
        assert (row.queue_name, row.priority, row.status) == (LOOPS_QUEUE, WAKE_PRIORITY, TaskResultStatus.READY)
        assert row.args_kwargs["kwargs"] == {"event_pk": 7}

    def test_the_deferred_dispatch_wakes_through_the_same_task(self) -> None:
        DeferredIngestDispatch().wake(7)

        row = DBTaskResult.objects.get(task_path=wake_incoming_event.module_path)
        assert row.args_kwargs["kwargs"] == {"event_pk": 7}

    def test_a_row_pruned_before_the_wake_ran_is_not_an_error(self) -> None:
        assert wake_incoming_event.call(event_pk=987654) == "skipped"
        assert not IncomingEvent.objects.exists()
