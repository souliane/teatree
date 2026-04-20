---
name: ship
description: Delivery — committing, pushing, creating MR/PR, pipeline monitoring, review requests. Use when user says "commit", "push", "MR", "merge request", "pull request", "finalize", "deliver", "ship", or is in the delivery phase.
compatibility: macOS/Linux, git, glab or gh CLI, CI system.
requires:
  - workspace
  - rules
companions:
  - finishing-a-development-branch
  - verification-before-completion
triggers:
  priority: 10
  exclude: '\breview\b'
  keywords:
    - '\b(merge request|pull request|create an? (mr|pr)|\bmr\b|push\b|finalize|deliver|ship it|create mr|create pr)\b'
    - '\bcommit\b'
search_hints:
  - commit
  - push
  - ship
  - deliver
  - merge request
  - pull request
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Delivery

## Delegation

This skill delegates the generic branch-finalization doctrine to:

- `finishing-a-development-branch` — decide how to wrap up a ready branch
- `verification-before-completion` — fresh verification before claiming the branch is ready

These are optional companion skills from [obra/superpowers](https://github.com/obra/superpowers). If not installed, this skill still works — you just won't get the external branch-finalization and verification guidelines. TeaTree keeps the delivery-specific mechanics locally: ticket-aware commit metadata, MR creation rules, CI cancellation, and post-review branch policy.

From "code is done" to "MR is merged."

## Dependencies

- **workspace** (required) — provides environment context. **Load `/t3:workspace` now** if not already loaded.

## Workflow

### 0. Ticket-Required Overlay Gate

When the active overlay has `require_ticket = True`, refuse to commit or push without a ticket reference.

- **Detection:** check `overlay.config.require_ticket`. Overlays that dogfood their own workflow enable this flag.
- **Every commit must include** an issue reference in the message body. Run `t3 overlay config --key mr_close_ticket` to check the setting: when `true`, use `Fixes #<number>` or `Closes #<number>` (auto-closes on merge); when `false`, use `Relates-to #<number>` (links without closing).
- **If no ticket context exists:** ask "Which ticket is this for?" Do not proceed without a ticket reference.
- **Exception:** commits from `/t3:retro` (format `fix(<skill>): ...`) are exempt — retro findings are small tactical fixes committed directly on the current branch.

### 1. Commit

- **Run `prek install` before the first commit in any worktree (Non-Negotiable).** This wires prek as the git pre-commit hook runner. Without it, whatever hook runner the repo happens to have (or nothing) runs on `git commit` — you cannot rely on prek's quality gates. This applies in every worktree, including colleagues' worktrees and review worktrees.
- **Never commit to the default branch (Non-Negotiable).** Run `git branch --show-current` before every commit. If you are on `main`, `master`, `development`, or `release`, STOP — create a feature branch first (`git checkout -b <prefix>-<repo>-<topic>`), then commit there. This applies even for "quick fixes" and hotfixes with no ticket.
- **Verify branch matches ticket** before committing. If on the wrong branch, create a clean branch from the default branch and cherry-pick.
- **Check for pre-existing changes before staging.** If the diff includes changes you did not make in this session, **warn the user** — either stage only your hunks or ask how to proceed.
- Format commit message following the project's commit format reference.
- **Link commits to issues.** Check `t3 overlay config --key mr_close_ticket`: when `true`, use `Fixes #<number>` or `Closes #<number>` in the commit message body (auto-closes on merge); when `false`, use `Relates-to #<number>` (links without closing). This applies to ALL repos.
- Read `TICKET_URL` from `.env.worktree` — never construct it from the branch name.

### 2. Finalize Branch

- `t3 workspace finalize [msg]` — squash commits + rebase on default branch.
- Run in each repo that has changes.
- Verify the commit message follows the project's format.

**Squash rules:**

- **Use `git reset --soft`, not interactive rebase.** `git rebase -i` with custom editors is fragile when pre-commit hooks run on each commit. Use `git reset --soft $(git merge-base origin/<default-branch> HEAD) && git commit` to squash, or cherry-pick for non-adjacent commits.
- **Never rewrite pushed history.** Check `git log origin/<branch>..HEAD` to confirm which commits are local-only before squashing.
- Group by topic, keep human-sized commits.
- Squash integrity check: save `OLD_TIP=$(git rev-parse HEAD)`, verify `git diff $OLD_TIP..HEAD` is empty after rewrite.
- Respect `T3_AUTO_SQUASH` (`true` = auto, `false` = ask first).
- **Always use `git merge-base`** for the squash target. NEVER use `origin/master` or `origin/main` directly — the branch may have been created from a stale local copy, causing the squash to include unrelated commits. The `t3 workspace finalize` command handles this correctly.

### 3. Local Verification

- Start servers and verify functionality.
- **E2E gate:** If the project requires E2E tests for the type of changes made (UI, forms, user flows), those tests must be written and passing BEFORE proceeding. E2E is part of implementation, not a post-push activity.
- **Wait for user feedback.** Do NOT proceed to push without user approval.

### 3a. BLUEPRINT.md Sync

If the changes touch architecture, add new modules, rename commands, or change extension points:

1. Read `BLUEPRINT.md` and check if it reflects the current state.
2. If it doesn't, update it **before** pushing. Ask the user before modifying.
3. This applies to all repos that have a `BLUEPRINT.md`.

### 3b. Self-Review Against Repo Rules

**Before every push**, run the self-review gate from [`../t3:review/SKILL.md`](../t3:review/SKILL.md) § "Active Verification Against Repo Rules":

1. **Load the project's code-review skill** (e.g., `/code-review`) if available. This skill contains the exact rules enforced by automated review bots — loading it prevents multi-round push-fix-push cycles.
2. **Read** the repo's `AGENTS.md` (or equivalent agent instructions file).
3. **For each changed file**, verify compliance against every applicable rule — commit message format, architectural patterns, banned patterns, feature flags.
4. Fix any violations **before** pushing.

Skipping this step is the #1 cause of wasted push-fix-push cycles. The rules exist in `t3:review` and the project's code-review skill — this step ensures they are applied even when the agent goes directly from code to ship without a formal review phase.

### 4. Push

- Cancel stale pipelines before pushing (if branch has an existing MR).
- Push to remote.

### 4b. Review Gate

Before creating an MR, the `pr create` command automatically checks the session gate:

- **shipping** requires prior `testing` and `reviewing` phases
- If no review session ran for this ticket, `pr create` returns an error with a hint to run `/t3:review`
- Use `--skip-validation` only when explicitly told to bypass gates

### 5. Create MR/PR

**STOP — resolve the ticket URL before typing the glab command.**

Before composing any `glab mr create` or `glab mr update` call, answer these three questions:

1. **What is the ticket URL?** Find the GitLab issue/work item URL from context. If none exists, create one now (`glab issue create`) and copy the URL. Do NOT proceed without a URL.
2. **What is the feature flag?** Use `[none]` if there is no flag.
3. **Is the title in the exact format?** `type(scope): description [flag] (ticket_url)` — both `--title` and the first line of `--description` must be identical and match this format exactly.

This gate exists because the CI `validate_mr_title_and_description` job fails on every MR that skips the ticket URL, causing a red pipeline that requires a separate fix push. The CI validator is the safety net — not the first line of defence. **Always resolve the URL before creating the MR.**

**Read the project's delivery hooks reference** (e.g. `references/delivery-hooks.md`) for the concrete MR creation template. Critical rules:

- **MR title = squash commit message** (MRs use squash-before-merge, so the title becomes the final commit). It MUST include the ticket URL: `type(scope): description [flag_if_feat] (TICKET_URL)`
- **MR description first line = same format as title** (CI validates it). NEVER start with `## Summary` — that fails validation.
- **Always assign to the user.** The `t3 pr create` command handles the correct flags automatically.

> **PreToolUse hook:** A `validate-mr-metadata.sh` hook automatically intercepts MR create/update commands in project repos. It validates the title and description first line against the release-notes format rules and **blocks** non-compliant calls with a clear error. Fix the reported issues and retry — no manual validation needed.

### 6. Monitor Pipeline

- Background polling for pipeline status.
- On failure → delegate to fix-push-monitor loop (see `/t3:test`).

### 7. Review Request

- Send notification to the appropriate review channel.
- Only after pipeline is green.

## Addressing Review Comments (Post-MR)

When fixing review comments on an already-existing MR:

0. **Verify branch alignment.** Confirm the worktree is on the MR's source branch (`git branch --show-current` vs MR metadata). If the worktree uses a different branch name, resolve the mismatch **before** editing: either checkout the MR branch or plan to cherry-pick onto it after committing. Discovering the mismatch mid-push wastes time on branch gymnastics.
1. **Fix the issues** as requested.
2. **Merge the default branch** if needed: `git merge origin/main`. **Never rebase** — the branch has already been reviewed.
3. **Run lint/pre-commit** (`prek run --all-files` or equivalent) after merging — merges can expose new lint violations in your code even without conflicts.
4. **Push without squashing or rebasing** (regular commits on top).
5. **Reply to the review comments on the MR.**
6. **Do NOT send a review request notification** — reviewers are already watching.

## Merging the Default Branch into an MR (Non-Negotiable)

Before touching the MR branch to "prepare" it for a merge, reason through what a clean 3-way merge would produce on its own:

- **Default branch removed keys/lines the MR still has?** The merge will apply those removals automatically — no preemptive cleanup commit needed. Adding one creates noise and risks side effects (e.g., `json.dumps` round-tripping normalizes unrelated formatting).
- **Both branches independently added the same key/line with different values?** That is a true add/add conflict. But verify the merge result first — the merge may have already resolved it correctly. Only surface it to the user if the result actually needs to change.

**Merge conflict resolution for JSON files:**

- Use proper 3-way semantics: `result = theirs + (ours_keys − base_keys)`. This correctly applies the default branch's removals while keeping the MR's own additions.
- Do NOT use `json.dumps` to serialise back — it normalises indentation and whitespace across the entire file, producing a noisy diff far beyond the intended change. Remove keys surgically (line-by-line) to preserve original formatting.
- Do NOT use `git checkout --ours` on whole files — this discards the default branch's removals and reintroduces whatever it had cleaned up.

**After resolving conflicts, verify before asking anything:**

1. Check that all MR-own additions (keys in ours but not in the merge base) are present in the result.
2. Check that any values that differ between ours and theirs are already at the correct value per the merge strategy. If the result is already correct, do not ask the user — they made no decision to make.

## Isolate Unrelated Fixes (Non-Negotiable)

When a CI failure (or any bug found during work) is **pre-existing** — not introduced by the current branch:

1. **Do NOT fix it on the feature branch.** It pollutes the MR diff and conflates unrelated changes.
2. Create a **dedicated branch** from the default branch (e.g., `<prefix>-myproject-fix-flaky-test-ordering`).
3. Apply the fix there, push, and open a **separate MR** targeting the default branch.
4. Once merged (or while waiting), rebase the feature branch to pick up the fix.

**How to detect:** `git diff origin/main...HEAD --name-only` — if the failing file was never touched by the feature branch, the bug is pre-existing.

## Post-Delivery Retrospective

After delivery is complete (MR created, pipeline green), run `/t3:retro` to capture any lessons learned during the session.

## Rules

- **Never push untested code.** Local verification by the user is mandatory before pushing. If the project requires E2E tests for UI changes, those tests must be **written and green** before pushing — not "pending" or "will do after MR".
- **Never rewrite settled commits (Non-Negotiable).** Never rebase, amend, or force-push commits that are already on origin. This applies always — not just after review. Before any squash/fixup, check `git log origin/<branch>..HEAD` to confirm which commits are local-only. Even within local-only commits, **only squash commits from the current work session** — older commits on the branch that predate the current task are settled history. When the user says "squash what belongs together", ask which commit range is in scope rather than assuming the entire local history is fair game.
- **No rebase / force push after review.** Once an MR has been reviewed, the branch history is shared. Only merge the default branch and push new commits.
- **Cancel stale pipelines** before every push to a branch with an existing MR.
- **Cancel running pipelines when closing an MR/PR.** When an MR is closed (abandoned, superseded, or replaced), cancel any running or pending pipelines for that branch immediately — they waste CI resources on code that will never be merged.
- **Clickable references:** Every MR, ticket, or note reference must be a markdown link — see [`../t3:rules/SKILL.md`](../t3:rules/SKILL.md) § "Clickable References".
- **Commit early, commit often.** Never accumulate more than 1-2 tickets of uncommitted changes. Commit after completing each ticket or logical unit of work. Squash later with `t3 workspace finalize`.
- **Never push without explicit approval.** Canonical rule: see [`../t3:rules/SKILL.md`](../t3:rules/SKILL.md) § "Never Push Without Separate Explicit Approval". Covers commit/squash/rebase/force-push approval boundaries and the `--no-verify` ban.
- **Respect commit trailer preferences.** Check the user's global agent config for rules about `Co-Authored-By` trailers before committing. Some users explicitly opt out. When in doubt, **do not add trailers** — the user can always configure their agent to add them.

### Git History Rewriting

When rewriting commit messages, use `filter-branch --msg-filter` (matches by full hash). Do NOT use `git rebase -i` with `GIT_SEQUENCE_EDITOR="sed"` — the short hash may differ from `git log --oneline`, causing a silent no-op.

**Post-rewrite verification (Non-Negotiable):** After ANY rebase or filter-branch, verify the hash changed. Same hash = no-op.
