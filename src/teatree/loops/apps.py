from importlib import import_module
from typing import TYPE_CHECKING

from django.apps import AppConfig

if TYPE_CHECKING:
    from teatree.core.views._webhook_persistence import IngestDispatch, IngestionRecord


def _registry_dispatch() -> "IngestDispatch":
    return import_module("teatree.loops.incoming_event_wake").RegistryIngestDispatch()


class DeferredIngestDispatch:
    # ready() runs in every django.setup(); the registry pulls in the loop package, so resolve it on first ingest.
    @staticmethod
    def handles(record: "IngestionRecord") -> bool:
        return _registry_dispatch().handles(record)

    @staticmethod
    def wake(event_pk: int) -> None:
        _registry_dispatch().wake(event_pk)


class LoopsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.loops"
    verbose_name = "TeaTree Loops"

    @staticmethod
    def ready() -> None:
        # The persistence module imports models, so it can only load once the app registry is ready.
        import_module("teatree.core.views._webhook_persistence").set_ingest_dispatch(DeferredIngestDispatch())
