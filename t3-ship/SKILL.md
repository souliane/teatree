---
name: t3-ship
description: Delivery — committing, pushing, creating MR/PR, pipeline monitoring, review requests. Use when user says "commit", "push", "MR", "merge request", "pull request", "finalize", "deliver", "ship", or is in the delivery phase.
compatibility: macOS/Linux, git, glab or gh CLI, CI system.
requires:
  - t3-workspace
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Delivery

From "code is done" to "MR is merged."

## Dependencies

- **t3-workspace** (required) — provides environment context. **Load `/t3-workspace` now** if not already loaded.

## Workflow

### 1. Commit

- Check for unstaged changes: `git status --short` in **every** repo of the ticket directory.
- Format commit message following the project's commit format reference.
- Read `TICKET_URL` from `.env.worktree` — never construct it from the branch name.

### 2. Finalize Branch

- `t3_finalize [msg]` — squash commits + rebase on default branch.
- Run in each repo that has changes.
- Verify the commit message follows the project's format.

### 3. Local Verification

- Start servers and verify functionality.
- **E2E gate:** If the project requires E2E tests for the type of changes made (UI, forms, user flows), those tests must be written and passing BEFORE proceeding. E2E is part of implementation, not a post-push activity.
- **Wait for user feedback.** Do NOT proceed to push without user approval.

### 4. Push

- Cancel stale pipelines before pushing (if branch has an existing MR).
- Push to remote.

### 5. Create MR/PR

**Read the project's delivery hooks reference** (e.g. `references/delivery-hooks.md`) for the concrete MR creation template. Critical rules:

- **MR title = squash commit message** (MRs use squash-before-merge, so the title becomes the final commit). It MUST include the ticket URL: `type(scope): description [flag_if_feat] (TICKET_URL)`
- **MR description first line = same format as title** (CI validates it). NEVER start with `## Summary` — that fails validation.
- **Always assign to the user.**
- Flags: `--squash-before-merge`, `--remove-source-branch`, `--assignee @me`.

> **PreToolUse hook:** A `validate-mr-metadata.sh` hook automatically intercepts MR create/update commands in project repos. It validates the title and description first line against the release-notes format rules and **blocks** non-compliant calls with a clear error. Fix the reported issues and retry — no manual validation needed.

### 6. Monitor Pipeline

- Background polling for pipeline status.
- On failure → delegate to fix-push-monitor loop (see `/t3-test`).

### 7. Review Request

- Send notification to the appropriate review channel.
- Only after pipeline is green.

## Addressing Review Comments (Post-MR)

When fixing review comments on an already-existing MR:

1. **Fix the issues** as requested.
2. **Merge the default branch** if needed: `git merge origin/main`. **Never rebase** — the branch has already been reviewed.
3. **Push without squashing or rebasing** (regular commits on top).
4. **Reply to the review comments on the MR.**
5. **Do NOT send a review request notification** — reviewers are already watching.

## Isolate Unrelated Fixes (Non-Negotiable)

When a CI failure (or any bug found during work) is **pre-existing** — not introduced by the current branch:

1. **Do NOT fix it on the feature branch.** It pollutes the MR diff and conflates unrelated changes.
2. Create a **dedicated branch** from the default branch (e.g., `<prefix>-myproject-fix-flaky-test-ordering`).
3. Apply the fix there, push, and open a **separate MR** targeting the default branch.
4. Once merged (or while waiting), rebase the feature branch to pick up the fix.

**How to detect:** `git diff origin/main...HEAD --name-only` — if the failing file was never touched by the feature branch, the bug is pre-existing.

## Post-Delivery Retrospective

After delivery is complete (MR created, pipeline green), run `/t3-retro` to capture any lessons learned during the session.

## Rules

- **Never push untested code.** Local verification by the user is mandatory before pushing. If the project requires E2E tests for UI changes, those tests must be **written and green** before pushing — not "pending" or "will do after MR".
- **No rebase / force push after review.** Once an MR has been reviewed, the branch history is shared. Only merge the default branch and push new commits.
- **Cancel stale pipelines** before every push to a branch with an existing MR.
- **Clickable references:** Every MR, ticket, or note reference must be a markdown link — see [`../references/agent-rules.md`](../references/agent-rules.md) § "Clickable References".

## Script Reference

| Script | Purpose |
|--------|---------|
| `t3_finalize` | Squash + rebase on default branch |
| Issue tracker CLI (`glab mr create` / `gh pr create`) | MR/PR creation |
| `t3_trigger_e2e` | Trigger E2E tests on CI (ext: `wt_trigger_e2e`) |
| `t3_fetch_failed_tests` | CI failure analysis (ext: `wt_fetch_failed_tests`) |
