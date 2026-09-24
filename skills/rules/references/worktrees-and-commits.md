# Worktrees and commits

The full text of the `/t3:rules` sections on where work happens and when it is committed: worktree-first work, concurrent-agent safety, committing before declaring done, pre-commit failures, imported code, deprecated code, and inline comments. The core `skills/rules/SKILL.md` carries each Non-Negotiable rule's trigger and verdict.

## Verify Imports Before Applying External Code

When cherry-picking code from orphan commits, stashes, snapshots, or other branches, verify every import and function call exists in the target codebase before applying. Snapshot code assumes a different state — modules, classes, and function signatures may not exist in HEAD. Apply each change surgically and run the type checker (`ty-check`) before moving on.

## Commit Before Declaring Done (Non-Negotiable)

When implementation is complete (all files written, tests pass or verified), **commit immediately** in the same response — do not wait for the user to ask. An uncommitted change is not "done"; it is in-progress work at risk of being lost to context compaction, parallel agents, or session timeout.

**Commit before any long pre-push step (Non-Negotiable).** Some mandated steps between "implementation complete" and "push" are multi-tool and multi-minute (a privacy scan, a final full-suite run, an evidence-gathering pass). Running any of them with the entire change set uncommitted leaves it exposed for that whole window: a concurrent `workspace clean-all` / worktree prune that removes the worktree in that window **irrecoverably destroys uncommitted work** — no branch ref, no reflog, no remote, nothing to `git fsck`. The sequence is **implementation complete → verify → local commit → privacy scan → push (`pr create`)**. A local commit is cheap and reversible, and makes the work recoverable even if the worktree vanishes. Never start a long mandated step with the deliverable uncommitted. (#837: retro is no longer a per-ticket pre-push step — it is an orchestrator-level periodic synthesis over durable signal; sub-agents emit findings into durable state and do not self-retro before `pr create`.)

## Pre-Commit Hook Failures on Unrelated Tests

When a pre-commit hook runs the full test suite and fails on tests **unrelated to your changes** (pre-existing failures), do not fix them one by one in a loop. After the **second** unrelated failure, stop and tell the user: the hook is failing on pre-existing test issues, and list the failing tests so they can be fixed separately. Never suggest or use `--no-verify` — see `t3:ship § Never use --no-verify`.

## Worktree-First Work (Non-Negotiable)

**All development work MUST happen in a worktree**, never on the main clone. Use `t3 <overlay> workspace ticket` or the `using-git-worktrees` skill to create one before writing any code. The worktree exists _before the first file change_ — the failure mode this forecloses is editing the main clone first and "moving it into a worktree later", which loses uncommitted work and pollutes shared state. Enforced deterministically by the `refuse-main-clone-commit` pre-commit hook and the `protect-default-branch` PreToolUse deny.

**Pre-edit check — before editing ANY project file:** If the file path lives directly under `$T3_WORKSPACE_DIR/<repo>/` (not under a ticket subdirectory like `$T3_WORKSPACE_DIR/<ticket>/<repo>/`), **stop** — you are in the main clone. Find or create the correct worktree first via `t3 <overlay> workspace ticket`. The main clone may happen to be on the PR branch (from a previous checkout) — editing there "works" but pollutes the shared clone, risks merge conflicts for other worktrees, and violates isolation.

**Pre-commit check — before running `git commit` (Non-Negotiable):** Run `git rev-parse --show-toplevel`. If the result is the main clone (e.g., `$T3_REPO`, `~/workspace/<repo>/<repo>` — i.e. NOT a `$T3_WORKSPACE_DIR/<ticket>/<repo>` path), **abort the commit**. Do not proceed to commit on `main` or any default branch in the main clone, even if the staged changes are already there from a prior session. Recovery path:

1. Pick a branch name (`ac/<short-slug>` matching the change).
2. `git branch <branch> HEAD` (snapshots the current staged + working state to the new branch).
3. If staged-but-not-committed: `git stash push --staged`, `git worktree add ~/workspace/<branch>/<repo> -b <branch>`, `cd` into the worktree, `git stash pop`, then commit there.
4. If already-committed-on-main: `git branch <branch> HEAD`, `git reset --hard origin/main` (or `git reset --hard <previous-HEAD>`), then `git worktree add ~/workspace/<branch>/<repo> <branch>` and continue from the worktree.

**Collision detection — check on EVERY file write or git operation:**

1. Before writing to a file, run `git status`. If you see unexpected modifications to files you did not touch, **another agent is working in the same directory**.
2. **If you are NOT in a worktree:** STOP writing code. Move all your work to a worktree immediately (`t3 <overlay> workspace ticket` or `EnterWorktree`), then continue there.
3. **If you ARE in a worktree and see someone else's changes:** STOP ALL WORK IMMEDIATELY. Alert the user: _"ALERT: Another agent is modifying files in my worktree at `<path>`. I've stopped all work to avoid conflicts. Please resolve before I continue."_ Do NOT attempt to continue, merge, or work around the collision.

**Why:** Parallel agents modifying the same checkout cause silent data loss — commits overwrite each other, stashes destroy in-progress work, and merge conflicts go undetected. This has cost hours of wasted work. Worktrees give each agent an isolated copy. The rules below are secondary defenses.

**Pre-task check — before tackling a known issue (failing CI job, regression, "fix X" ticket):** Run `git worktree list` first. If a worktree branch name matches the bug surface (e.g., `ac/fix-loop-scanner-*` for scanner failures, or any branch with relevant commits in `git log --oneline main..HEAD`), **another agent is likely already on it**. Do NOT spawn a parallel worktree on the same problem — coordinate or stand down. The collision rule above catches conflicts at write-time; this catches them before any work starts.

## Concurrent Agent Safety (Non-Negotiable)

Assume another agent may be modifying the same repo concurrently. Never `git stash`, `git checkout --`, or `git restore` files you didn't change — this destroys the other agent's in-progress work. Only stage and commit files you explicitly modified.

**When to stage is part of that rule, not a detail.** The commit hook stashes every UNSTAGED change in the tree, the other writer's included, so `git add` right after each edit is what keeps yours out of a stash a killed run may never restore — and `git commit -o -- <your paths>` is what keeps theirs out of your commit. A bare `git commit -a` sweeps in whatever they were mid-edit on. The stash mechanics and the recovery path are in [`../ship/SKILL.md`](../../ship/SKILL.md) § "What the commit gate commits is the INDEX, not the working tree".

## Deprecated Code

When removing a function, class, flag, or CLI argument: delete it completely. Deprecated aliases, backward-compat re-exports, and `# removed` comments create maintenance debt. If callers exist, update them in the same change. Teatree is experimental — no deprecation warnings, no migration helpers. Break cleanly.

## GitLab Inline Comments

When posting inline PR comments, target **added lines only** — not context or unchanged lines.
