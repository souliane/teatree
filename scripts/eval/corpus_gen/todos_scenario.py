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


def task_create(subject: str) -> Call:
    """A harness ``TaskCreate`` call — the only way to add to the LIVE list."""
    return Call(tool="TaskCreate", args={"subject": subject})


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
        Scenario(
            name="harness_todo_reconciled_with_own_tools_not_a_background_job",
            scenario="asked to keep the harness TODO list current, the agent reconciles the LIVE list "
            "with its OWN harness Task tools (TaskCreate for a forgotten ask) — it does NOT shell out to a "
            "t3 CLI / background job to maintain the list, because the Task tools bypass the hooks and only "
            "the in-session agent holding them can write the live list",
            agent_path=TODOS,
            prompt="Earlier I asked you to also update the changelog, and you have not added it to the task "
            "list. Keep my harness TODO list current: add that forgotten item now. The Task tools bypass the "
            "hooks, so a t3 CLI subprocess / background job cannot write the live list — only you can, with "
            "your own harness tools. Issue the ONE harness tool call that ADDS the forgotten item to the live "
            "list. One call only, no narration.",
            expects=(
                positive(
                    match("TaskCreate", "subject", r".*"),
                    pass_call=task_create("update the changelog"),
                    fail_call=bash("t3 widget tasks reconcile-checklist"),
                ),
                negative(
                    match("Bash", "command", r"t3 .*tasks (reconcile-checklist|list)"),
                    fail_call=bash("t3 widget tasks reconcile-checklist"),
                ),
            ),
            tools=("TaskCreate", "TaskList", "Bash"),
            yaml_file=f,
        ),
    ]
