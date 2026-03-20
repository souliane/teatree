---
name: t3-ship
description: Delivery — committing, pushing, creating MR/PR, pipeline monitoring, review requests. Use when user says "commit", "push", "MR", "merge request", "pull request", "finalize", "deliver", "ship", or is in the delivery phase.
compatibility: macOS/Linux, git, glab or gh CLI, CI system.
requires:
  - t3-workspace
  - t3-rules
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Delivery

## Delegation

This skill delegates the generic branch-finalization doctrine to:

- `finishing-a-development-branch` — decide how to wrap up a ready branch
- `verification-before-completion` — fresh verification before claiming the branch is ready

TeaTree keeps the delivery-specific mechanics locally: ticket-aware commit metadata, MR creation rules, CI cancellation, and post-review branch policy.

From "code is done" to "MR is merged."

## Dependencies

- **t3-workspace** (required) — provides environment context. **Load `/t3-workspace` now** if not already loaded.

## Workflow

### 1. Commit

- **Verify branch matches ticket:** Run `git branch --show-current` and confirm the branch name relates to the ticket you're working on. If on the wrong branch (e.g., a stale branch from a previous task), create a clean branch from the default branch and cherry-pick your commit before pushing.
- Check for unstaged changes: `git status --short` in **every** repo of the ticket directory.
- Format commit message following the project's commit format reference.
- Read `TICKET_URL` from `.env.worktree` — never construct it from the branch name.

### 2. Finalize Branch

- `t3 workspace finalize [msg]` — squash commits + rebase on default branch.
- Run in each repo that has changes.
- Verify the commit message follows the project's format.

**Squash rules:** Follow the canonical squash rules from `ac-managing-repos` § Workflow 2 — Squash & Prepare. Key points: never rewrite pushed history, group by topic, keep human-sized, squash integrity check (`OLD_TIP` before/after diff), respect `T3_AUTO_SQUASH`.

**Squash base (Non-Negotiable):** Always compute the squash target with `git merge-base origin/<default-branch> HEAD`. NEVER use `origin/master` or `origin/main` directly — the branch may have been created from a stale local copy, causing the squash to include unrelated commits from other authors. The `t3 workspace finalize` command handles this correctly.

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
- **Never rewrite settled commits (Non-Negotiable).** Never rebase, amend, or force-push commits that are already on origin. This applies always — not just after review. Before any squash/fixup, check `git log origin/<branch>..HEAD` to confirm which commits are local-only. Even within local-only commits, **only squash commits from the current work session** — older commits on the branch that predate the current task are settled history. When the user says "squash what belongs together", ask which commit range is in scope rather than assuming the entire local history is fair game.
- **No rebase / force push after review.** Once an MR has been reviewed, the branch history is shared. Only merge the default branch and push new commits.
- **Cancel stale pipelines** before every push to a branch with an existing MR.
- **Cancel running pipelines when closing an MR/PR.** When an MR is closed (abandoned, superseded, or replaced), cancel any running or pending pipelines for that branch immediately — they waste CI resources on code that will never be merged.
- **Clickable references:** Every MR, ticket, or note reference must be a markdown link — see [`../t3-rules/SKILL.md`](../t3-rules/SKILL.md) § "Clickable References".
- **Never push without explicit approval (Non-Negotiable).** Squash approval ≠ push approval. "All done" ≠ push approval. Always present the final state and ask "Push?" before running `git push`. This applies to ALL repos, ALL contexts.
- **Squash with `git reset --soft`, not interactive rebase.** `git rebase -i` with custom editors is fragile when pre-commit hooks run on each commit. Use `git reset --soft HEAD~N && git commit` for adjacent commits, or cherry-pick for non-adjacent ones.
- **Respect commit trailer preferences.** Check the user's global agent config for rules about `Co-Authored-By` trailers before committing. Some users explicitly opt out. When in doubt, **do not add trailers** — the user can always configure their agent to add them.

### Git History Rewriting Recipes

When rewriting commit messages, use `filter-branch --msg-filter` — it reliably matches commits by full hash. Do NOT use `git rebase -i` with `GIT_SEQUENCE_EDITOR="sed"` — the short hash in the rebase todo may differ from `git log --oneline`, causing a silent no-op.

```bash
# Rewrite one commit's message:
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --msg-filter '
if [ "$GIT_COMMIT" = "'"$(git rev-parse <short-hash>)"'" ]; then
  cat << "NEWMSG"
<new message here>
NEWMSG
else
  cat
fi
' <short-hash>^..HEAD
git update-ref -d refs/original/refs/heads/main

# Drop a commit:
git rebase --onto <commit>^ <commit>

# Remove a trailer from all commits in a range:
FILTER_BRANCH_SQUELCH_WARNING=1 git filter-branch -f --msg-filter 'sed "/^Co-Authored-By:/d"' <oldest-commit>^..HEAD
```

**Post-rewrite verification (Non-Negotiable):** After ANY rebase or filter-branch, verify the hash changed. Same hash = no-op.

## Command Reference

| Command | Purpose |
|---------|---------|
| `t3 <overlay> workspace finalize` | Squash + rebase on default branch |
| `t3 <overlay> pr create` | MR/PR creation |
| `t3 ci trigger-e2e` | Trigger E2E tests on CI |
| `t3 ci fetch-failed-tests` | CI failure analysis |
