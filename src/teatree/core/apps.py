from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.core"
    verbose_name = "TeaTree Core"

    def ready(self) -> None:  # noqa: PLR6301
        from teatree.core.signals import register_signals  # noqa: PLC0415

        register_signals()
