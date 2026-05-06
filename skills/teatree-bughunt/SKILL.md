---
name: teatree-bughunt
description: Self-QA variant of batch mode — dogfood the teatree dashboard, find real bugs (missing items, state/action mismatches, broken links, stale data), file them, then fix them in worktrees. Use when the user says "bug hunt", "self-qa", "hunt bugs", "find and fix bugs", or "dogfood the dashboard".
metadata:
  version: 0.0.1
  subagent_safe: false
triggers:
  priority: 85
  keywords:
    - '\b(bug hunt|bug[- ]hunt|self[- ]qa|hunt (for )?bugs|find and fix bugs)\b'
    - '\bdogfood the dashboard\b'
search_hints:
  - bug hunt
  - self-qa
  - dogfood dashboard
  - hunt bugs
  - find and fix
---

# TeaTree — Bug Hunt Mode (Self-QA on the Dashboard)

A Quick Wins variant where, instead of picking tickets off the board, the agent dogfoods the dashboard, finds bugs, files them, and fixes them in the same session. The user no longer has to play QA.

Shares the Quick Wins family with `/teatree-batch`.

## Prerequisites

Same as `/teatree-batch` (`ac-python`, `ac-django`, overlay skill loaded). Plus: `t3 dashboard` must boot cleanly from the main clone (no uncommitted in-progress edits blocking startup).

## Step 1 — Ask the scope

Use `AskUserQuestion` with three options:

- **Existing** — tackle open issues labelled `bug` from the board (no hunting).
- **New** — skip the board, dogfood the dashboard, file and fix whatever turns up.
- **Both** — existing first (they've already been triaged), then hunt for new ones.

Never silently pick one. The choice changes the workload materially.

## Step 2 — Launch the dashboard (New / Both)

From the main clone — NOT a worktree. The goal is to QA the deployed state.

```bash
cd "$T3_REPO"
t3 dashboard &
DASHBOARD_PID=$!
# Wait for HTTP 200 before inspecting
until curl -sf http://127.0.0.1:8000/ > /dev/null; do sleep 1; done
```

Remember the PID — kill it at the end.

## Step 3 — Inspect every view

**Preferred tool:** Chrome DevTools MCP (`mcp__chrome-devtools__*`) if loaded — it gives live DOM, JS console errors, network failures, and screenshots. Fall back to `WebFetch` per URL if the MCP is unavailable. Raw `curl` HTML is last resort because dynamic content won't render.

Walk every view in the dashboard IA. For each list page, also open 2–3 detail pages. Focus on:

- **Tickets list / detail** — counts match DB? `overlay`, `variant`, `status`, `repos` populated? Links work?
- **Worktrees list / detail** — FSM `state` coherent with filesystem? Ports shown? No duplicates from stale rows?
- **Sessions list / detail** — visited phases match the `Session` record? Repos modified/tested populated?
- **Task queue** — PENDING + CLAIMED + DONE counts add up to total? No stuck leases (CLAIMED with stale heartbeat)?
- **Review / PR views** — action buttons match item state? (e.g., a "request review" action must not appear for already-merged MRs; a "waiting for my review" list must offer a "start review" affordance).
- **Followup views** — sync status fresh? No orphan tickets?

### What counts as a bug (file it)

- **Missing items** that should appear (empty list when DB has rows).
- **Extra items** that shouldn't appear (stale entries, soft-deleted rows leaking through).
- **Corrupted / stale data** (timestamps in the wrong tz, nulls where the DB has a value, counts that don't match the underlying query).
- **State / action mismatch** — action offered that can't apply to the item's current state (e.g. "post Slack review request" on a merged MR, "approve" on a draft), or expected action missing (e.g. no "start review" button on an MR assigned to the user).
- **Broken links / 500s / 404s / JS console errors.**
- **Layout glitches** that block interaction (button offscreen, modal can't close).

### What does NOT count (don't file)

- Subjective UX preferences, cosmetic nits with no functional impact.
- Feature requests (file separately with label `enhancement`, don't mix into the bug batch).
- Flakes that don't reproduce on a second load — note them, re-check at the end.

## Step 4 — Present findings before filing

List every bug with: page URL, symptom (concrete: what you saw vs. what you expected), probable cause if you can tell from a quick code scan, severity (blocker / high / medium / low). Ask the user to confirm the list — this waives the standing "never create tickets without asking" rule **only for the confirmed batch**.

Dedupe aggressively: if three findings share one root cause, file one ticket with all three symptoms listed.

## Step 5 — File and implement

For each confirmed bug, in severity order:

1. `gh issue create` with label `bug`, clear reproduction steps, severity.
2. Add to the project board.
3. Implement per `/teatree-batch` rules (worktree via `t3 teatree workspace ticket`, TDD, `t3:reviewer` sub-agent, sequential merge).
4. Close the issue via the PR.

## Step 6 — Tear down

```bash
kill "$DASHBOARD_PID" 2>/dev/null
pkill -f "uvicorn teatree.asgi" 2>/dev/null
```

Report: bugs found, filed, fixed, skipped (with reasons).

## Rules

- The dashboard runs from the main clone, but all **fixes** happen in worktrees — don't edit the main clone.
- Bound the hunt: one pass through every top-level view. Don't spiral into exhaustive edge-case exploration — if a view looks fine on a first careful pass, move on.
- If the dashboard won't boot, that's bug #1 — file and fix it before continuing.
- Chrome DevTools MCP screenshots belong in the issue body when the bug is visual.
