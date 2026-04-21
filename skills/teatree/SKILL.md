---
name: teatree
description: TeaTree agent lifecycle platform — installation, configuration, lifecycle phases, overlay concept, CLI reference, and skill loading. Use when working on teatree itself or when understanding how teatree orchestrates agent workflows.
metadata:
  version: 0.0.1
triggers:
  priority: 90
  keywords:
    - '\b(teatree|t3)\b'
    - '\b(lifecycle|overlay|worktree|provision|headless)\b'
    - '\b(plan(ning)?|prioriti[zs]e|backlog|project board|sprint|roadmap)\b'
    - '\b(batch mode|work unattended|tackle tickets|quick wins)\b'
    - '\b(bug hunt|bug[- ]hunt|self[- ]qa|dogfood the dashboard|hunt (for )?bugs|find and fix)\b'
  exclude: '\b(t3:code|t3:test|t3:ship|t3:debug|t3:review)\b'
search_hints:
  - teatree
  - lifecycle
  - overlay
  - worktree
  - provision
  - headless
  - skill loading
  - agent workflow
  - planning
  - prioritize
  - backlog
  - project board
  - batch mode
  - unattended
  - quick wins
  - bug hunt
  - self-qa
  - dogfood dashboard
---

# TeaTree — Agent Lifecycle Platform

TeaTree is a Django project that orchestrates agent workflows through lifecycle phases. Overlays are lightweight Python packages that extend it for specific projects.

## Architecture

- **TeaTree IS the Django project.** `pip install teatree` works standalone.
- **Overlays** register via `teatree.overlays` entry points and provide project-specific configuration.
- **Skills** live in `skills/` and are loaded by the agent's skill system.
- **Hooks** in `hooks/scripts/` run on agent lifecycle events (e.g., prompt submit, pre/post tool use).

## Lifecycle Phases

```
ticket → code → test → review → ship → review-request
```

Each phase maps to a skill (`t3:ticket`, `t3:code`, etc.). The `Session` model tracks visited phases and enforces quality gates (e.g., can't ship without testing).

## CLI Reference

```bash
t3 dashboard                # Start dashboard + background worker (top-level)
t3 <overlay> resetdb        # Drop and recreate the SQLite database
t3 lifecycle setup          # Provision worktree (ports, DB, overlay steps)
t3 lifecycle start          # Start dev servers
t3 lifecycle status         # Show worktree state
t3 lifecycle teardown       # Stop services, clean up
t3 tasks work-next-sdk      # Claim and execute next headless task
t3 tasks work-next-user-input  # Claim and launch next interactive task
t3 followup sync            # Daily ticket/MR sync
```

## Key Models

- **Ticket** — issue URL, overlay, variant, repos
- **Worktree** — repo path, branch, ports, state (FSM: created → provisioned → services_up → ready)
- **Session** — agent session with visited phases, repos modified/tested
- **Task** — claimable work unit with lease, heartbeat, parent chain
- **TaskAttempt** — execution result with exit code, structured output

## Overlay API

Overlays subclass `OverlayBase` and override methods:

- `get_repos()` — repo list for worktree creation
- `get_provision_steps(worktree)` — setup steps (migrations, fixtures)
- `get_run_commands(worktree)` — dev server commands
- `get_db_import_strategy(worktree)` — DSLR/dump import config
- `get_services_config(worktree)` — Docker services
- `get_visual_qa_targets(changed_files)` — URL paths the pre-push browser sanity gate should load (default: `[]` — opt in by mapping diff paths to URLs)

## Skill Loading

TeaTree's UserPromptSubmit hook detects intent from user prompts using `triggers:` patterns in skill frontmatter. The hook suggests loading the matching skill. A PreToolUse hook blocks Bash/Edit/Write until suggested skills are loaded.

The `SkillLoadingPolicy` class resolves which skills to load based on intent, overlay, and current phase. For headless tasks, `search_hints` in frontmatter provide keyword matching.

## Plugin Hooks Architecture

Hooks are registered in `hooks/hooks.json` (shipped with the plugin). This is the **sole source** for hook registrations — do NOT duplicate hooks in the user's `~/.claude/settings.json`. When adding or changing hooks, only modify `hooks.json` in this repo.

**Known failure (2026-04-02):** PR #109 moved hooks from `settings.json` to plugin `hooks.json` but didn't remove the old ones. This caused double hook execution on every tool call, accelerating context consumption and triggering aggressive microcompaction. Prevention: when migrating hooks to the plugin, always remove the `settings.json` equivalents in the same change.

## Dogfooding Checklist (CLI/Server Changes)

When modifying CLI commands, dashboard views, or server startup:

1. **Run the command yourself** — don't rely on unit tests alone. `t3 <command>` from a worktree (not the main clone) to catch cwd-dependent bugs.
2. **Verify HTTP 200** — for dashboard/server changes: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:<port>/` must return 200.
3. **Run E2E tests** — dashboard changes require Playwright E2E tests in `e2e/test_dashboard.py`. **Pre-flight:** kill any zombie servers first (`pkill -9 -f "uvicorn teatree.asgi"; pkill -9 -f "chrome-headless"; pkill -9 -f "playwright/driver"`). Then: `DJANGO_SETTINGS_MODULE=e2e.settings uv run pytest e2e/ --ds e2e.settings --no-cov -v`. Each timed-out run leaves zombie processes — kill before retrying. For full-suite validation prefer CI (clean environment, ~seconds/test vs. 7+ min locally on a loaded machine).
4. **Test the full flow** — if the change involves task execution, create a task and verify the worker picks it up. Don't declare "auto-start works" without observing a task transition from PENDING to CLAIMED.
5. **Check overlay resolution from worktrees** — `discover_active_overlay()` uses cwd-based discovery. Worktree directory names don't match overlay names. Always test from a worktree path, not the main clone.

**Known pitfall:** `discover_active_overlay()` returns the directory name when `manage.py` is found via cwd walk. In worktrees, this gives names like `move-dashboard-to-general-cli` instead of `t3-teatree`. The `_resolve_overlay_for_server()` function in `cli/__init__.py` works around this by preferring entry-point overlays.

**Known pitfall:** `uv run` rebuilds editable installs and can silently revert uncommitted source edits. See `workspace/references/troubleshooting.md` § "uv run Silently Reverts Edits". Commit changes before running `uv run pytest`, or verify file content after test runs.

**Known pitfall (git stash + checkout to switch branches):** Using `git stash` + `git checkout <other-branch>` to temporarily commit on another branch causes silent edit loss — stash pop can restore a stale file version that appears to be the current one (because inode mtime doesn't change) but isn't. The symptom: edits appear gone, or tests run against in-memory-cached version while disk has old content. Fix: always use separate worktrees, never git stash. See `feedback_always_use_worktree.md` in memory.

## Planning & Backlog Prioritization

When the user asks for planning, prioritization, or backlog management, follow this workflow. The goal is to keep the GitHub Projects v2 board as the single source of truth for what to work on next.

### Prerequisites

- The overlay must have `github_owner` and `github_project_number` configured in its `overlay_settings.py`. These are user settings — never hardcode project URLs.
- The `gh` CLI must have the `project` scope. If missing: `gh auth refresh -s read:project -s project`.

### 1. Sync Issues to the Project Board

**Auto-add all repo issues that aren't on the board yet:**

```bash
# List all open issues in the repo
gh issue list --repo <owner>/<repo> --state open --json number,url --limit 200

# List items already on the board
gh project item-list <project_number> --owner <owner> --format json

# For each issue NOT on the board:
gh project item-add <project_number> --owner <owner> --url <issue_url>
```

Do this for ALL repos the overlay manages (check `get_repos()` or the repo's GitHub org). Every open issue must be on the board — no orphans.

### 2. Present the Backlog

Fetch all board items and present them grouped by status column:

| Priority | # | Title | Labels | Status | Iteration |
|----------|---|-------|--------|--------|-----------|
| 1 | #97 | Shared Docker images | architecture | Todo | — |
| 2 | #167 | Dockerize t3 itself | enhancement | Todo | — |
| ... | | | | | |

Sort within each status by current board position (the board's drag order is authoritative).

### 3. Help Prioritize

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

### 4. Update Status Columns

If the user wants to move items between columns (Todo → In Progress, etc.):

```bash
# Get the field ID for Status and the option IDs
gh project field-list <project_number> --owner <owner> --format json

# Update an item's status
gh project item-edit --project-id <project_id> --id <item_id> --field-id <status_field_id> --single-select-option-id <option_id>
```

### 5. Iteration (Optional)

The board may have an "Iteration" field for sprint-like grouping. If the user asks about sprints or iterations, use it. Otherwise, don't force it — priority order within "Todo" is sufficient.

### 6. What to Tackle Next

After prioritization, the agent knows the backlog order. When the user asks "what's next?" or starts a new session:

1. Fetch the board (`fetch_project_items` or `gh project item-list`).
2. The first item in "Todo" (by board position) is the next ticket.
3. Suggest it: "Next up: #X — *title*. Load `/t3:ticket` to start?"

This replaces guessing or asking the user what to work on — the board is the queue.

### 7. Batch Mode (Unattended Ticket Processing)

Follows after prioritization (steps 1-6 above). When the user says "batch mode", "work unattended", "tackle tickets", or "quick wins":

**Prerequisites:** Load `ac-python` and `ac-django` — all code must follow their review checklists. If the overlay has a companion skill, load it too.

1. **Run a codebase health audit** (load `ac-reviewing-codebase` in a sub-agent). Scope: all repos in the user's workspace directories. This finds actionable items beyond the issue tracker: god-modules, broken CI gates, missing coverage, stale branches.
2. **Fetch the prioritized board** (from step 6) and sort by effort (quick wins first).
3. **For each ticket**, in order:
   - Read the issue. If it requires design decisions or user input, **skip it** and move to the next.
   - Create a worktree at `~/workspace/souliane/tickets/<slug>`.
   - Implement following `ac-python`/`ac-django` standards. When a teatree change affects the overlay API, make the corresponding overlay fix in the same session.
   - Run tests + lint, self-review with a `t3:reviewer` sub-agent.
   - Push, create PR, wait for CI, merge.
   - Clean up worktree, update main.
   - **Merge each PR before starting the next** (sequential, not parallel).
4. **Close stale issues** that are already resolved in the codebase.
5. **Report** what was done and what was skipped (with reasons) at the end.

**Handling user requests mid-session:**

During batch/quickwin sessions, the user may send new requests (bug reports, feature ideas, feedback) while you're implementing a ticket. When this happens:

1. **Create a GitHub issue immediately** for the new request — don't defer or forget it.
2. **Resume the current ticket** without losing progress.
3. If the request is a quick rename or one-line fix in a file you're already editing, fold it into the current PR.
4. If the request requires its own worktree/branch, add it to your ticket queue and implement it in order after the current ticket.

**Rules:**

- Never edit the main clone — always use worktrees.
- Never create issues/PRs without implementing them.
- Skip tickets needing architectural decisions — collect them for the user.
- Self-review every PR before merging.
- Commit progressively at stable states.
- Fix overlays together with core changes — don't leave them broken.

### 8. Bug Hunt Mode (Self-QA on the Dashboard)

A Quick Wins variant where, instead of picking tickets off the board, the agent dogfoods the dashboard, finds bugs, files them, and fixes them in the same session. The user no longer has to play QA.

Triggered by: "bug hunt", "self-qa", "dogfood the dashboard", "hunt bugs", "find and fix bugs". Shares the Quick Wins family with Batch Mode.

**Prerequisites:** same as Batch Mode (`ac-python`, `ac-django`, overlay skill loaded). Plus: `t3 dashboard` must boot cleanly from the main clone (no uncommitted in-progress edits blocking startup).

#### Step 1 — Ask the scope

Use `AskUserQuestion` with three options:

- **Existing** — tackle open issues labelled `bug` from the board (no hunting).
- **New** — skip the board, dogfood the dashboard, file and fix whatever turns up.
- **Both** — existing first (they've already been triaged), then hunt for new ones.

Never silently pick one. The choice changes the workload materially.

#### Step 2 — Launch the dashboard (New / Both)

From the main clone — NOT a worktree. The goal is to QA the deployed state.

```bash
cd "$T3_REPO"
t3 dashboard &
DASHBOARD_PID=$!
# Wait for HTTP 200 before inspecting
until curl -sf http://127.0.0.1:8000/ > /dev/null; do sleep 1; done
```

Remember the PID — kill it at the end.

#### Step 3 — Inspect every view

**Preferred tool:** Chrome DevTools MCP (`mcp__chrome-devtools__*`) if loaded — it gives live DOM, JS console errors, network failures, and screenshots. Fall back to `WebFetch` per URL if the MCP is unavailable. Raw `curl` HTML is last resort because dynamic content won't render.

Walk every view in the dashboard IA. For each list page, also open 2–3 detail pages. Focus on:

- **Tickets list / detail** — counts match DB? `overlay`, `variant`, `status`, `repos` populated? Links work?
- **Worktrees list / detail** — FSM `state` coherent with filesystem? Ports shown? No duplicates from stale rows?
- **Sessions list / detail** — visited phases match the `Session` record? Repos modified/tested populated?
- **Task queue** — PENDING + CLAIMED + DONE counts add up to total? No stuck leases (CLAIMED with stale heartbeat)?
- **Review / PR views** — action buttons match item state? (e.g., a "request review" action must not appear for already-merged MRs; a "waiting for my review" list must offer a "start review" affordance).
- **Followup views** — sync status fresh? No orphan tickets?

#### What counts as a bug (file it)

- **Missing items** that should appear (empty list when DB has rows).
- **Extra items** that shouldn't appear (stale entries, soft-deleted rows leaking through).
- **Corrupted / stale data** (timestamps in the wrong tz, nulls where the DB has a value, counts that don't match the underlying query).
- **State / action mismatch** — action offered that can't apply to the item's current state (e.g. "post Slack review request" on a merged MR, "approve" on a draft), or expected action missing (e.g. no "start review" button on an MR assigned to the user).
- **Broken links / 500s / 404s / JS console errors.**
- **Layout glitches** that block interaction (button offscreen, modal can't close).

#### What does NOT count (don't file)

- Subjective UX preferences, cosmetic nits with no functional impact.
- Feature requests (file separately with label `enhancement`, don't mix into the bug batch).
- Flakes that don't reproduce on a second load — note them, re-check at the end.

#### Step 4 — Present findings before filing

List every bug with: page URL, symptom (concrete: what you saw vs. what you expected), probable cause if you can tell from a quick code scan, severity (blocker / high / medium / low). Ask the user to confirm the list — this waives the standing "never create tickets without asking" rule **only for the confirmed batch**.

Dedupe aggressively: if three findings share one root cause, file one ticket with all three symptoms listed.

#### Step 5 — File and implement

For each confirmed bug, in severity order:

1. `gh issue create` with label `bug`, clear reproduction steps, severity.
2. Add to the project board.
3. Implement per Batch Mode rules (worktree under `~/workspace/souliane/tickets/<slug>`, TDD, `t3:reviewer` sub-agent, sequential merge).
4. Close the issue via the PR.

#### Step 6 — Tear down

```bash
kill "$DASHBOARD_PID" 2>/dev/null
pkill -f "uvicorn teatree.asgi" 2>/dev/null
```

Report: bugs found, filed, fixed, skipped (with reasons).

#### Rules specific to Bug Hunt Mode

- The dashboard runs from the main clone, but all **fixes** happen in worktrees — don't edit the main clone.
- Bound the hunt: one pass through every top-level view. Don't spiral into exhaustive edge-case exploration — if a view looks fine on a first careful pass, move on.
- If the dashboard won't boot, that's bug #1 — file and fix it before continuing.
- Chrome DevTools MCP screenshots belong in the issue body when the bug is visual.

## Configuration

`~/.teatree` sourced by hooks:

```bash
T3_REPO="$HOME/workspace/souliane/teatree"  # teatree repo path
T3_CONTRIBUTE=true                           # allow retro to modify core skills
T3_PUSH=false                                # gate pushes behind an explicit prompt
T3_AUTO_PUSH_FORK=false                      # auto-push to fork when T3_PUSH=true and origin ≠ T3_UPSTREAM
T3_AUTO_SHIP=false                           # when true, shipping tasks are headless; default gates push on user approval
T3_PRIVACY=strict                            # block commits with PII
```
