from django.apps import AppConfig


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.agents"
    verbose_name = "TeaTree Agents"

    def ready(self) -> None:  # noqa: PLR6301 — Django AppConfig.ready() hook; on the class by Django contract, uses no self
        from teatree.agents.headless import run_headless  # noqa: PLC0415 — deferred: call-time import, kept lazy
        from teatree.agents.ticket_short_description import run_short_describe  # noqa: PLC0415 — lazy import
        from teatree.core.deterministic_phases import register_phase_runner  # noqa: PLC0415 — lazy import
        from teatree.core.headless_dispatch import register_headless_runner  # noqa: PLC0415 — lazy import
        from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE  # noqa: PLC0415 — lazy import

        register_headless_runner(run_headless)
        # #3570: short_describe is deterministic, not agentic — the agentic runner has
        # no path to Ticket.short_description at all, so it narrated a summary it never
        # wrote. Registered here (not imported by core) to keep the layer inversion.
        register_phase_runner(SHORT_DESCRIBE_PHASE, run_short_describe)
