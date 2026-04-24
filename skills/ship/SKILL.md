---
name: ship
description: Delivery — committing, pushing, creating MR/PR, pipeline monitoring, review requests. Use when user says "commit", "push", "MR", "merge request", "pull request", "finalize", "deliver", "ship", or is in the delivery phase.
compatibility: macOS/Linux, git, glab or gh CLI, CI system.
requires:
  - workspace
  - rules
companions:
  - finishing-a-development-branch
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
- **Baseline noqa in new files uses `relax:` type.** The teatree `quality-gates` hook flags any new `# noqa` / `# type: ignore` / `# pragma: no cover` in source files (excluding `tests/`, `scripts/hooks/`, `e2e/`). When a new file needs the house pattern `# noqa: S404` at `import subprocess` and `# noqa: S603` at each `subprocess.run` call (the pattern used by every existing CLI module), the hook treats it as a relaxation. Use `relax(<scope>): …` as the commit type, with a body explaining it follows the established baseline. Do NOT remove the suppressions — the ruff config relies on them.

### 2. Finalize Branch

- `t3 <overlay> workspace finalize [msg]` — squash commits + rebase on default branch.
- Run in each repo that has changes.
- Verify the commit message follows the project's format.

**Squash rules:**

- **Use `git reset --soft`, not interactive rebase.** `git rebase -i` with custom editors is fragile when pre-commit hooks run on each commit. Use `git reset --soft $(git merge-base origin/<default-branch> HEAD) && git commit` to squash, or cherry-pick for non-adjacent commits.
- **Never rewrite pushed history** — see § Rules below for the full statement. Before any squash, check `git log origin/<branch>..HEAD` to confirm which commits are local-only.
- Group by topic, keep human-sized commits.
- Squash integrity check: save `OLD_TIP=$(git rev-parse HEAD)`, verify `git diff $OLD_TIP..HEAD` is empty after rewrite.
- Respect `T3_AUTO_SQUASH` (`true` = auto, `false` = ask first).
- **Always use `git merge-base`** for the squash target. NEVER use `origin/master` or `origin/main` directly — the branch may have been created from a stale local copy, causing the squash to include unrelated commits. The `t3 <overlay> workspace finalize` command handles this correctly.

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

**Before every push**, run the self-review gate from [`../review/SKILL.md`](../review/SKILL.md) § "Active Verification Against Repo Rules":

1. **Load the project's code-review skill** (e.g., `/code-review`) if available. This skill contains the exact rules enforced by automated review bots — loading it prevents multi-round push-fix-push cycles.
2. **Read** the repo's `AGENTS.md` (or equivalent agent instructions file).
3. **For each changed file**, verify compliance against every applicable rule — commit message format, architectural patterns, banned patterns, feature flags.
4. Fix any violations **before** pushing.

Skipping this step is the #1 cause of wasted push-fix-push cycles. The rules exist in `t3:review` and the project's code-review skill — this step ensures they are applied even when the agent goes directly from code to ship without a formal review phase.

### 3c. Retrospective Before Push (Non-Negotiable, Enforced)

Run `/t3:retro` **before** pushing — not after merge. Retro findings often surface skill fixes, guardrail improvements, or documentation updates that belong in the same PR as the feature. Running retro after push/merge means those improvements ship as a separate follow-up PR (or, worse, get lost to context compaction before they land anywhere).

Sequence: code → test → review gate → **retro** → push → MR → monitor CI.

If retro produces file edits (skill fixes, reference updates, docs), commit them on the current branch before § 4 Push so they ride along with the MR.

**Enforcement:** `t3 <overlay> pr create` refuses to create the MR until the `retro` phase is marked visited on the active session. Retro marks its own visit via `t3 <overlay> lifecycle visit-phase <ticket_id> retro` (documented in `/t3:retro`). If the shipping gate complains that `retro` is missing, run retro — do not bypass with `--skip-validation` unless explicitly instructed.

### 4. Push

- **Reconcile with the default branch first.** `git fetch origin <default> && git log <branch>..origin/<default> --oneline` — if any commits appear, merge them in (`git merge origin/<default>`) and re-run lint/tests before pushing. Opening a PR that is already BEHIND main forces the user (or you) to do a second round-trip to resolve conflicts; catch them now, while you have the context of your own change open.
- Push to remote. Cancel stale pipelines first if the branch has an existing MR (see § Rules).

### 4b. Review Gate

Before creating an MR, the `pr create` command automatically checks the session gate:

- **shipping** requires prior `testing` and `reviewing` phases
- If no review session ran for this ticket, `pr create` returns an error with a hint to run `/t3:review`
- Use `--skip-validation` only when explicitly told to bypass gates

### 4c. Visual QA Gate

`pr create` also runs a pre-push browser sanity gate as a side effect of the shipping flow. It loads the page(s) the branch diff actually touches and reports silent-render regressions (page crashes, console errors, raw `app.*` translation keys, blocking asset 404s). Target URLs come from the overlay's `get_visual_qa_targets(changed_files)` — overlays opt in by mapping diff paths to URLs.

- Runs automatically before MR creation; the report is recorded on `Ticket.extra['visual_qa']`.
- Blocks MR creation when findings exist; the error payload includes `report_markdown` for a `## Visual QA` section.
- Bypass: `t3 <overlay> pr create <ticket> --skip-visual-qa "<reason>"` or `T3_VISUAL_QA=disabled` in the environment.
- Skipped when Playwright cannot start — fails open with a clear message rather than blocking the push.

### 5. Create MR/PR

**Prefer `t3 <overlay> pr create` over raw `gh`/`glab`.** The CLI handles the title/body format, ticket URL injection, assignee, and fork-vs-upstream remote resolution — all of which are easy to get wrong by hand. Reach for raw `gh`/`glab` only when the overlay doesn't expose a `pr create` subcommand, or when you're fixing the CLI itself and need to bypass it for this one call.

#### Scope-Match Gate Before `Closes/Fixes #N` (Non-Negotiable)

Before writing `Closes #N` or `Fixes #N` in a PR body, re-read the linked issue end-to-end and **enumerate every acceptance criterion, phase, or deliverable it names**. For each one, mark it ✅ shipped, ⚠️ partial, or ❌ not started, and paste the matrix into the PR body. `Closes/Fixes` is only legal when every row is ✅. Otherwise:

- Use `Relates-to #N` (so the issue stays open).
- List the unshipped phases/AC in the PR body under a "Remaining scope" heading so the next agent sees the gap.
- Do NOT rely on "I'll do the rest later" memory. The issue body is the contract; a partial PR that auto-closes the issue silently discards the rest of the contract.

**Past failure (#97, PR #423):** Issue #97 defined 5 phases (`Phase 1` teardown cleanup through `Phase 5` teatree core hooks). PR #423 shipped only Phase 5 but used `Closes #97`, auto-closing the issue when 4/5 phases remained. The user had to reopen the issue manually. Prevention: the phase-by-phase ✅/❌ matrix in the PR body would have forced the correct verb (`Relates-to`).

**STOP — resolve the ticket URL before typing the glab command.**

Before composing any `glab mr create` or `glab mr update` call, answer these three questions:

1. **What is the ticket URL?** Find the GitLab issue/work item URL from context. If none exists, create one now (`glab issue create`) and copy the URL. Do NOT proceed without a URL.
2. **What is the feature flag?** Use `[none]` if there is no flag.
3. **Is the title in the exact format?** `type(scope): description [flag] (ticket_url)` — both `--title` and the first line of `--description` must be identical and match this format exactly.

This gate exists because the CI `validate_mr_title_and_description` job fails on every MR that skips the ticket URL, causing a red pipeline that requires a separate fix push. The CI validator is the safety net — not the first line of defence. **Always resolve the URL before creating the MR.**

**Read the project's delivery hooks reference** (e.g. `references/delivery-hooks.md`) for the concrete MR creation template. Critical rules:

- **MR title = squash commit message** (MRs use squash-before-merge, so the title becomes the final commit). It MUST include the ticket URL: `type(scope): description [flag_if_feat] (TICKET_URL)`
- **MR description first line = same format as title** (CI validates it). NEVER start with `## Summary` — that fails validation.
- **Always assign to the user.** The `t3 <overlay> pr create` command handles the correct flags automatically.

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

## One Open MR Per Ticket (Non-Negotiable)

Before opening a new MR/PR, check whether a sibling MR for the **same ticket** is already open on the same repo:

```bash
gh pr list --repo <repo> --search "<ticket-ref> is:open" --json number,headRefName,baseRefName
```

If a sibling is open, **do not open a second MR targeting the default branch** — the two branches will diverge on the same files and the second one will need a painful 3-way merge. Pick one:

1. **Wait for the sibling to merge**, then rebase the new work on the updated default branch and open the MR.
2. **Stack on the sibling's branch** — set the new MR's base to the sibling's source branch (`gh pr create --base <sibling-branch>`). Update the base to the default branch after the sibling merges, so the stacked MR stays minimal.

**Never open two MRs on the same ticket targeting the default branch in parallel.** The only exception is when the two MRs touch genuinely disjoint files (different repos, different modules with no shared imports, no overlapping generated docs) — and even then, the second MR's description must name the sibling PR it races with.

**Past failure (#140 / PRs #427 + #436):** Both PRs touched `README.md`, `BLUEPRINT.md`, `src/teatree/core/*` and ran in parallel against `main`. When #427 squash-merged first, #436 inherited an unsynced merge base and required a full 3-way conflict resolution. Opening #436 as a stacked PR with `--base ac/teatree-#140-initial-ship` would have avoided every conflict.

## Bundle Into an Existing Open PR

When a session uncovers a small unique commit on a now-stale branch (typical during cleanup or retro), and opening a dedicated PR for that one commit would be more ceremony than the change deserves, **bundle it into a sibling open PR** instead. This trades a little PR-scope discipline for delivery speed.

**Eligibility — all must hold:**

1. The commit is small and self-contained (single concern, no cross-cutting impact).
2. The target PR is **still open** and **not yet approved** (bundling into an approved PR forces re-review).
3. The target PR is on the same repo and the change is at least loosely thematically adjacent. Strictly unrelated bundles are still better than abandoning the work, but explain it in the PR description.
4. The bundled commit doesn't depend on or contradict anything in the target PR's diff.

**Procedure:**

1. Fetch the target PR's worktree (or create one with `t3 <overlay> workspace ticket <issue-url>` — use the same issue as the target PR).
2. Cherry-pick the commit: `git cherry-pick <sha>`. Resolve any conflicts surgically.
3. Run lint + the affected tests locally.
4. Push to the target PR's branch (regular push, no rebase).
5. **Update the target PR's title and description** to reflect both commits. Title format becomes `type(scope1): X + type(scope2): Y` if the two are heterogeneous. Body explains both fixes.
6. Notify the reviewer in the PR comments that the scope grew, with a one-line rationale.
7. Force-remove the original worktree and delete the now-empty branch (`git worktree remove --force <path>` + `git branch -D <branch>`).

**Anti-pattern:** bundling into a PR that's already passed review. The reviewer's approval covered the original scope, not the bundled commit.

## Rules

- **Never push untested code.** Local verification by the user is mandatory before pushing. If the project requires E2E tests for UI changes, those tests must be **written and green** before pushing — not "pending" or "will do after MR".
- **Never rewrite settled commits (Non-Negotiable).** Never rebase, amend, or force-push commits that are already on origin. This applies always — not just after review. Before any squash/fixup, check `git log origin/<branch>..HEAD` to confirm which commits are local-only. Even within local-only commits, **only squash commits from the current work session** — older commits on the branch that predate the current task are settled history. When the user says "squash what belongs together", ask which commit range is in scope rather than assuming the entire local history is fair game.
- **No rebase / force push after review.** Once an MR has been reviewed, the branch history is shared. Only merge the default branch and push new commits.
- **Cancel stale pipelines** before every push to a branch with an existing MR.
- **Cancel running pipelines when closing an MR/PR.** When an MR is closed (abandoned, superseded, or replaced), cancel any running or pending pipelines for that branch immediately — they waste CI resources on code that will never be merged.
- **Clickable references:** Every MR, ticket, or note reference must be a markdown link — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Clickable References".
- **Commit early, commit often.** Never accumulate more than 1-2 tickets of uncommitted changes. Commit after completing each ticket or logical unit of work. Squash later with `t3 <overlay> workspace finalize`.
- **Publishing actions are mode-conditional.** Canonical rule: see [`../rules/SKILL.md`](../rules/SKILL.md) § "Publishing Actions Are Mode-Conditional". In `interactive` mode (default) every push/MR/merge/remote-delete needs separate explicit approval. In `auto` mode (`t3.mode = "auto"` or `T3_MODE=auto`) the agent ships end-to-end without confirm prompts; only the always-gated list (force-push to defaults, history rewrites on shared defaults, destructive shared-state ops, unauthorised external writes, `--no-verify`) remains confirm-gated.
- **Commit trailer preferences** (`Co-Authored-By`) live in the user's global agent config — check it before committing; when in doubt, omit the trailer.

### Git History Rewriting

When rewriting commit messages, use `filter-branch --msg-filter` (matches by full hash). Do NOT use `git rebase -i` with `GIT_SEQUENCE_EDITOR="sed"` — the short hash may differ from `git log --oneline`, causing a silent no-op.

**Post-rewrite verification (Non-Negotiable):** After ANY rebase or filter-branch, verify the hash changed. Same hash = no-op.
