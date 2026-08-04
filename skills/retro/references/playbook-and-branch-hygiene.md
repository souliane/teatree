# Playbook lifecycle and the unpushed-work check

The procedures behind `/t3:retro` § "5. Playbook Lifecycle" and § "5b. Unpushed Commits & Dirty Repos Check". Those sections carry when each runs; this file carries the create/update/where criteria and the per-repo collection steps.

## Playbook lifecycle

**WHEN to create a new playbook:**

- A ticket required 4+ files across 2+ repos with a repeatable pattern
- A new integration point was discovered (webhook, API, document pipeline)

**WHEN to update an existing playbook:**

- A step was missing or wrong, discovered during implementation
- The codebase evolved and a step is now unnecessary (e.g., config-driven instead of code-driven)

**WHERE to create playbooks:**

- `<project-skill>/references/playbooks/<scope>-<topic>.md`
- Scope prefixes: `<project>-` (backend), `frontend-` (frontend), `cross-repo-` (multi-repo), none (process)
- **After creating/updating:** update the playbook `README.md` index with the new entry

**Playbook staleness check:** Before following any playbook, verify instructions against current code. If the codebase has moved to a config-driven approach or the referenced pattern no longer exists, the playbook is stale — fix it immediately.

## The unpushed-work check

For each touched repo, collect and display:

1. **Unpushed commits:** `git log --oneline @{u}..HEAD`
2. **Non-main branches:** detect the main branch via `git config init.defaultBranch` (fallback: `main`), then list all other local branches with `git branch --no-merged <main>` — these may contain in-progress work
3. **Stashes:** `git stash list` — stashes are easy to forget and may contain important WIP
4. **Uncommitted changes:** `git status --short` — show the summary, not just "dirty"
5. Flag any commits with `Co-Authored-By` trailers (should be removed per user's global config)
6. Flag merge commits that could be rebased away
7. Suggest consolidating multiple commits targeting the same skill into one
8. Present a concrete consolidation proposal and ask before acting

### Squash-merge cross-check (Non-Negotiable)

Before treating any local branch as "unpushed work", **cross-reference against the default branch**. Squash-merges create new SHAs, so `git log --not --remotes` by SHA alone will flag merged branches as unsynced.

Delegate this to the CLI: **run `t3 teatree workspace clean-all`**. It classifies each branch's unsynced commits into `squash_merged` (subject matches a commit on `origin/main` after stripping `(#NNN)` suffix and conventional-commit type prefix), `merge_commits` (multi-parent — safe to discard), and `genuinely_ahead` (real pending work). Only genuinely-ahead branches block cleanup.

Inside a TTY, `clean-all` prompts for each blocked worktree — `[P]ush to remote / [A]bandon (force delete) / [S]kip`. In a non-TTY context it preserves the old skip-and-report behaviour. Reach for the subject-matching Python recipe only when you need to classify raw stashes or stray local branches outside a tracked worktree.
