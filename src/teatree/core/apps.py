from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.core"
    verbose_name = "TeaTree Core"

    def ready(self) -> None:  # noqa: PLR6301 — Django AppConfig.ready() hook; on the class by Django contract, uses no self
        from teatree.core.model_registries import populate_model_registries  # noqa: PLC0415 — lazy import
        from teatree.core.process_freshness import record_loaded_snapshot  # noqa: PLC0415 — lazy import
        from teatree.core.projection_signals import register_projection_signals  # noqa: PLC0415 — lazy import
        from teatree.core.signals import register_signals  # noqa: PLC0415 — deferred: call-time import, kept lazy

        populate_model_registries()
        register_signals()
        register_projection_signals()
        # Freeze the migration heads this interpreter loaded, BEFORE any DB access (#4387).
        # It is the only moment the answer is knowable: from here on the files on disk can be
        # fast-forwarded under a running process while its imported model classes stay old.
        record_loaded_snapshot()
