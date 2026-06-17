"""The ``/t3:todos`` behavioral scenario: build the live session list dynamically.

Extracted to its own module so the over-cap ``catalog.py`` / ``per_skill.py``
god-files (ratcheted shrink-only by ``check_module_health.py``) stay untouched.
The group is aggregated into ``ALL_SCENARIOS`` by ``all_scenarios.py``.
"""

from scripts.eval.corpus_gen.catalog import TODOS, bash
from scripts.eval.corpus_gen.model import Call, Scenario, match, negative, positive


def task_list(status: str = "") -> Call:
    """A harness ``TaskList`` call — the LIVE in-memory session todo list.

    The optional ``status`` filter is the only matchable arg; a bare list-all
    call passes ``{"status": ""}`` so the matcher always has a value to compare.
    """
    return Call(tool="TaskList", args={"status": status})


def todos_scenarios() -> list[Scenario]:
    f = "todos.yaml"
    return [
        Scenario(
            name="todos_built_dynamically_from_live_tasklist_not_stale_cli",
            scenario="asked for the current session's task list, the agent builds it DYNAMICALLY from the "
            "live harness TaskList tool (the in-memory list) — it does NOT shell out to a t3 CLI process, "
            "which can only read a stale on-disk task store that lags the live session",
            agent_path=TODOS,
            prompt="Show me my current task list for this session — what's pending and what's in progress. "
            "Remember: a t3 CLI subprocess can only read a stale on-disk snapshot of the harness tasks, so "
            "build the list from the LIVE session instead. Issue the ONE tool call that reads the live "
            "in-memory session task list directly. One call only, no narration.",
            expects=(
                positive(
                    match("TaskList", "status", r".*"),
                    pass_call=task_list("in_progress"),
                    fail_call=bash("t3 widget tasks list --session"),
                ),
                negative(
                    match("Bash", "command", r"t3 .*tasks list .*--session"),
                    fail_call=bash("t3 widget tasks list --session"),
                ),
            ),
            tools=("TaskList", "Bash"),
            yaml_file=f,
        ),
    ]
