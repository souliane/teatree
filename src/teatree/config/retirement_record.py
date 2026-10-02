"""The record one retirement is written as — the shape both halves of the roll are built from.

It sits below both so the roll can be split across modules without either half
importing the other.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RetiredSetting:
    """One DB-home key that is no longer a live field, and what became of it.

    *replacement* names the current field a stored value migrates onto, or is
    ``None`` when the setting was removed outright. *reason* is rendered into the
    loud removal warning, so it is written for the operator reading it — what the
    setting used to do and what now does that job.

    *subsystem* names the whole subsystem the retirement took with it, as the
    word it is called by in scenario names (``"team"`` for the agent-teams pane
    layer). It is ``None`` — the common case — when only the setting went and the
    thing it configured is still live: ``branch_prefix`` retired the setting,
    but branch prefixes still resolve, so branch-prefix behaviour is still
    gradeable. A non-``None`` subsystem is a claim that the behaviour no longer
    exists, and ``tests/conformance/test_retired_subsystem_evals.py`` holds the
    eval catalog to it: souliane/teatree#3839 spent the full ``max_budget_usd``
    cap grading a subsystem retired in souliane/teatree#3734, because nothing
    tied the two ledgers together.
    """

    key: str
    reason: str
    replacement: str | None = None
    subsystem: str | None = None
