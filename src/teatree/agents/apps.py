from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.agents"
    verbose_name = "TeaTree Agents"

    def ready(self) -> None:  # noqa: PLR6301
        from teatree.agents.headless import run_headless  # noqa: PLC0415
        from teatree.core.headless_dispatch import register_headless_runner  # noqa: PLC0415

        register_headless_runner(run_headless)
