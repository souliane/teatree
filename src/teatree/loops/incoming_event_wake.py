from django.tasks import task

from teatree.core.models import IncomingEvent
from teatree.core.views._webhook_persistence import IngestionRecord
from teatree.loop.incoming_event_handling import handle_incoming_event
from teatree.loop.scanners import event_handlers
from teatree.loop.scanners.event_handlers import HandlingOutcome
from teatree.loops.timer_chains import LOOPS_QUEUE
from teatree.loops.timer_reconciler import WAKE_PRIORITY


@task(queue_name=LOOPS_QUEUE, priority=WAKE_PRIORITY)
def wake_incoming_event(event_pk: int) -> str:
    event = IncomingEvent.objects.filter(pk=event_pk).first()
    outcome = handle_incoming_event(event) if event is not None else HandlingOutcome.SKIPPED
    return outcome.value


class RegistryIngestDispatch:
    @staticmethod
    def handles(record: IngestionRecord) -> bool:
        return event_handlers.HANDLERS.lookup(record.source, record.event_name, record.action) is not None

    @staticmethod
    def wake(event_pk: int) -> None:
        wake_incoming_event.enqueue(event_pk=event_pk)
