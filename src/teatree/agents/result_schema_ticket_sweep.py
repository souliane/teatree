"""The ``ticket_sweep`` envelope field (#162 Rule 4).

Split out of the over-cap ``result_schema.py`` (docs/module-health.md), not for
a concern of its own.

The metric the owner reads is "each sweep tends to zero changes", and a count
the sweeping agent types into its own envelope measures nothing — a skipped
sweep and a clean backlog both report ``0``. The run row is the measurement;
:mod:`teatree.agents.ticket_sweep_recorder` checks the envelope against it.
"""

from typing import TypedDict

type JSONSchema = dict[str, object]


class TicketSweepEvidence(TypedDict, total=False):
    """The sweep run a ``backlog_sweep`` envelope names, so its count is read not typed."""

    run_id: str
    changed_count: int
    examined_count: int


#: Merged into ``RESULT_JSON_SCHEMA["properties"]`` under the ``ticket_sweep`` key.
TICKET_SWEEP_SCHEMA_PROPERTY: JSONSchema = {
    "type": "object",
    "description": (
        "The TicketSweepRun a backlog sweep opened and closed (#162 Rule 4). Required on "
        "the backlog_sweep phase and ignored elsewhere. `run_id` must name a FINISHED run; "
        "`changed_count`, when given, must equal the run's own — the count is read from "
        "the URLs the facade recorded, never taken from this envelope."
    ),
    "properties": {
        "run_id": {"type": "string"},
        "changed_count": {"type": "integer"},
        "examined_count": {"type": "integer"},
    },
    "required": ["run_id"],
}
