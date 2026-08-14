"""Django app config for ``teatree.agents`` — registration only, no SDK imports.

``ready()`` runs inside every ``django.setup()``, which is on the critical path of
EVERY ``t3`` invocation that touches the ORM — including ``t3 mcp serve``, whose
startup has to fit the MCP client's handshake window. Importing the runners here
eagerly pulled ``teatree.agents.runner`` -> ``teatree.agents.harness`` ->
``pydantic_ai.models.openai`` -> the whole ``openai.types.*`` pydantic model tree,
measured at ~10s of ``django.setup()`` on a loaded box. A client that gives up
before that finishes reports only ``Connection closed``.

So the registries are handed THUNKS instead. Both are inversion seams
(:mod:`teatree.core.agent_runner`, :mod:`teatree.core.deterministic_phases`)
that store a callable and invoke it later, so the import moves from app-ready to
first dispatch — paid by the code path that actually needs a model client, and by
nothing else. The registries stay fail-loud: an unresolvable runner raises out of
the thunk exactly as it would have raised out of ``ready()``.
"""

from typing import TYPE_CHECKING

from django.apps import AppConfig

if TYPE_CHECKING:
    from teatree.core.models import Task, TaskAttempt
    from teatree.types import SkillMetadata


def run_agent_deferred(task: "Task", *, phase: str, overlay_skill_metadata: "SkillMetadata") -> "TaskAttempt":
    """Dispatch to the real agent runner, importing the agent SDKs on first use."""
    from teatree.agents.runner import run_agent  # noqa: PLC0415 — the deferral IS this function's purpose

    return run_agent(task, phase=phase, overlay_skill_metadata=overlay_skill_metadata)


def run_short_describe_deferred(task: "Task") -> str:
    """Dispatch to the real ``short_describe`` runner, importing it on first use."""
    from teatree.agents.ticket_short_description import (  # noqa: PLC0415 — the deferral IS this function's purpose
        run_short_describe,
    )

    return run_short_describe(task)


class AgentsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "teatree.agents"
    verbose_name = "TeaTree Agents"

    def ready(self) -> None:  # noqa: PLR6301 — Django AppConfig.ready() hook; on the class by Django contract, uses no self
        from teatree.core.agent_runner import register_agent_runner  # noqa: PLC0415 — lazy import
        from teatree.core.deterministic_phases import register_phase_runner  # noqa: PLC0415 — lazy import
        from teatree.core.modelkit.phases import SHORT_DESCRIBE_PHASE  # noqa: PLC0415 — lazy import

        register_agent_runner(run_agent_deferred)
        # #3570: short_describe is deterministic, not agentic — the agentic runner has
        # no path to Ticket.short_description at all, so it narrated a summary it never
        # wrote. Registered here (not imported by core) to keep the layer inversion.
        register_phase_runner(SHORT_DESCRIBE_PHASE, run_short_describe_deferred)
