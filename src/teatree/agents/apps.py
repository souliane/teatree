from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.agents"
    verbose_name = "TeaTree Agents"

    def ready(self) -> None:  # noqa: PLR6301 — Django AppConfig.ready() hook; on the class by Django contract, uses no self
        from teatree.agents.headless import run_headless  # noqa: PLC0415 — deferred: call-time import, kept lazy
        from teatree.core.headless_dispatch import register_headless_runner  # noqa: PLC0415 — lazy import

        register_headless_runner(run_headless)
