---
name: teatree-plan
description: Backlog prioritization with the GitHub Projects v2 board as single source of truth. Syncs repo issues to the board, walks the user through prioritization one question at a time, and reorders/updates board columns. Use when the user asks for planning, prioritization, roadmap, sprint, or "what's next".
metadata:
  version: 0.0.1
  subagent_safe: false
triggers:
  priority: 85
  keywords:
    - '\b(plan(ning)?|prioriti[zs]e|backlog|project board|sprint|roadmap)\b'
    - '\bwhat.s next\b'
  exclude: '\b(batch mode|bug hunt|unattended|quick wins)\b'
search_hints:
  - planning
  - prioritize
  - backlog
  - project board
  - sprint
  - roadmap
  - what's next
---

# TeaTree — Backlog Prioritization

Keep the GitHub Projects v2 board as the single source of truth for what to work on next.

## Prerequisites

- The overlay must have `github_owner` and `github_project_number` configured in its `overlay_settings.py`. These are user settings — never hardcode project URLs.
- The `gh` CLI must have the `project` scope. If missing: `gh auth refresh -s read:project -s project`.

## 1. Sync Issues to the Project Board

Auto-add all repo issues that aren't on the board yet:

```bash
# List all open issues in the repo
gh issue list --repo <owner>/<repo> --state open --json number,url --limit 200

# List items already on the board
gh project item-list <project_number> --owner <owner> --format json

# For each issue NOT on the board:
gh project item-add <project_number> --owner <owner> --url <issue_url>
```

Do this for ALL repos the overlay manages (check `get_repos()` or the repo's GitHub org). Every open issue must be on the board — no orphans.

## 2. Present the Backlog

Fetch all board items and present them grouped by status column:

| Priority | # | Title | Labels | Status | Iteration |
|----------|---|-------|--------|--------|-----------|
| 1 | #97 | Shared Docker images | architecture | Todo | — |
| 2 | #167 | Dockerize t3 itself | enhancement | Todo | — |

Sort within each status by current board position (the board's drag order is authoritative).

## 3. Help Prioritize

Guide the user through prioritization by asking about each unordered item:

- **Dependencies**: "Does #X block #Y? Should #X come first?"
- **Impact vs effort**: "This looks high-impact/low-effort — move it up?"
- **Grouping**: "These 3 issues are related — tackle them together?"

Ask **one question at a time** using `AskUserQuestion`. Never dump a priority matrix — walk through it interactively.

After the user decides, reorder the board:

```bash
# Move an item to a specific position using the GraphQL API
gh api graphql -f query='
  mutation {
    updateProjectV2ItemPosition(input: {
      projectId: "<project_node_id>"
      itemId: "<item_node_id>"
      afterId: "<after_item_node_id>"
    }) { item { id } }
  }'
```

## 4. Update Status Columns

If the user wants to move items between columns (Todo → In Progress, etc.):

```bash
# Get the field ID for Status and the option IDs
gh project field-list <project_number> --owner <owner> --format json

# Update an item's status
gh project item-edit --project-id <project_id> --id <item_id> --field-id <status_field_id> --single-select-option-id <option_id>
```

## 5. Iteration (Optional)

The board may have an "Iteration" field for sprint-like grouping. Use it only if the user asks. Otherwise, priority order within "Todo" is sufficient.

## 6. What to Tackle Next

After prioritization, the agent knows the backlog order. When the user asks "what's next?" or starts a new session:

1. Fetch the board (`fetch_project_items` or `gh project item-list`).
2. The first item in "Todo" (by board position) is the next ticket.
3. Suggest it: "Next up: #X — *title*. Load `/t3:ticket` to start?"

This replaces guessing or asking the user what to work on — the board is the queue.

## Follow-On Modes

After the board is prioritized, the user may want to execute tickets unattended or hunt for new bugs. These are separate skills:

- **`/teatree-batch`** — work the prioritized backlog unattended, one ticket at a time.
- **`/teatree-bughunt`** — dogfood the dashboard, file and fix whatever turns up.
