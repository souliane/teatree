from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.agents"
    verbose_name = "TeaTree Agents"

    def ready(self) -> None:  # noqa: PLR6301 — Django AppConfig.ready() hook; on the class by Django contract, uses no self
        from teatree.agents.headless import run_headless  # noqa: PLC0415 — deferred: call-time import, kept lazy
        from teatree.agents.short_describe import run_short_describe  # noqa: PLC0415 — lazy import
        from teatree.core.deterministic_dispatch import register_deterministic_phase  # noqa: PLC0415 — lazy import
        from teatree.core.headless_dispatch import register_headless_runner  # noqa: PLC0415 — lazy import
        from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE  # noqa: PLC0415 — lazy import

        register_headless_runner(run_headless)
        register_deterministic_phase(SHORT_DESCRIBE_PHASE, run_short_describe)
