from django.apps import AppConfig


class LoopsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.loops"
    verbose_name = "TeaTree Loops"

    @staticmethod
    def ready() -> None:
        from teatree.core.views._webhook_persistence import set_ingest_dispatch  # noqa: PLC0415 — lazy import
        from teatree.loops.incoming_event_wake import RegistryIngestDispatch  # noqa: PLC0415 — lazy import

        set_ingest_dispatch(RegistryIngestDispatch())
