# CLI Parity with Dashboard

Every dashboard action must have a corresponding `t3` CLI subcommand.

| Dashboard Action | CLI Equivalent | Status |
|-----------------|---------------|--------|
| Sync All | `t3 <overlay> followup sync` | Done |
| Launch Terminal | `t3 <overlay> tasks work-next-user-input` | Done |
| Launch Agent | `t3 <overlay> tasks work-next-sdk` | Done |
| Cancel Task | `t3 <overlay> tasks cancel <id>` | TODO |
| Ticket Transition | `t3 <overlay> workspace ticket --transition <name>` | TODO |
| Create Task | `t3 <overlay> tasks create <ticket_id>` | TODO |
| View Session History | `t3 <overlay> tasks history <session_id>` | TODO |
| View Task Detail | `t3 <overlay> tasks detail <task_id>` | TODO |
| Sort Tickets | N/A (display logic) | N/A |
| Toggle Dismissed | N/A (filter state) | N/A |

## Principles

- CLI must work when dashboard is down or rate-limited
- Every mutating action needs a CLI path
- CLI output should be machine-parseable (JSON option)
