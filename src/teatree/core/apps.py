from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.core"
    verbose_name = "TeaTree Core"

    def ready(self) -> None:  # noqa: PLR6301 — Django AppConfig.ready() hook; on the class by Django contract, uses no self
        from teatree.core.model_registries import populate_model_registries  # noqa: PLC0415 — lazy import
        from teatree.core.signals import register_signals  # noqa: PLC0415 — deferred: call-time import, kept lazy

        populate_model_registries()
        register_signals()
