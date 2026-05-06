---
name: sweeping-prs
description: Maintenance sweep across all your open PRs/MRs — merge the default branch, fix conflicts, monitor CI, push, and (per-repo policy) optionally squash-merge each PR before moving to the next. Never rebases. Use when user says "sweep PRs", "update all my MRs", "merge main into open PRs", or wants to keep open PRs up to date with main.
compatibility: macOS/Linux, zsh, git, issue tracker CLI (glab, gh).
requires:
  - workspace
  - ship
  - rules
  - platforms
triggers:
  priority: 95
  keywords:
    - '\b(pr.?sweep|sweep (prs?|mrs?)|update (all )?(my )?(open )?(prs?|mrs?)|merge main into (open )?(prs?|mrs?)|refresh (open )?(prs?|mrs?))\b'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# PR Sweep — Batch Maintenance for Open PRs

Walk every open PR/MR you authored, sequentially, and bring each up to date with the default branch:

1. Merge `origin/<default>` into the source branch (**never rebase**).
2. If conflicts are mechanical, resolve and continue. If not, prompt the user.
3. Push.
4. Watch CI. On red, hand off to the existing `/t3:debug` + `/t3:ship` fix-push-monitor loop.
5. Depending on the per-repo policy (see § Per-Repo Policy below), either squash-merge the PR before moving on, or stop at "green and up to date".

The goal is to keep stale PRs mergeable without burying review feedback under a force-push, and — for repos the user fully owns — actually drain the queue rather than just refresh it.

## Per-Repo Policy

Each PR's repo (`org/repo`, taken from the JSON `references.full` minus the `!iid` suffix) is matched against a `SWEEP_POLICY` map declared in `~/.ac-reviewing-codebase`:

```
SWEEP_POLICY="<owned-org>/(repo-a|repo-b):serial-merge;<other-org>/.+:bulk-update"
```

Same regex+semicolon shape as the other knobs in that file. Two policies are recognized:

| Policy | What it does | When to pick it |
|---|---|---|
| **`bulk-update`** (default) | For each open PR: update from `<default>` → push → watch CI → next PR. The PR itself is **not** merged. | Repos where merging requires human approval, or where the user only wants stale branches refreshed. |
| **`serial-merge`** | For each open PR: update from `<default>` → push → wait for CI **green** → squash-merge the PR → fetch the next PR (next iteration sees the just-landed commit as part of `main`). | Repos the user fully owns and wants drained (e.g. `souliane/teatree`, `souliane/skills`) without piling conflict cascades onto the next PR. |

Repos absent from `SWEEP_POLICY` default to `bulk-update` so existing behavior is unchanged for unconfigured repos.

**Why serial, not parallel, for `serial-merge`:** the whole point is that PR #N+1 must see the *post-merge* state of `main` before it tries to update — that is what removes the conflict cascade. Sweeping the full list in parallel and then merging sequentially defers the same conflicts to merge time instead of preventing them. The `serial-merge` loop therefore re-runs the discovery CLI after each merge, so it always operates on a fresh "open PRs as of now" snapshot.

## Dependencies

- **t3:workspace** (required) — needed to create or reuse a worktree per PR.
- **t3:ship** (required) — the CI fix-push-monitor loop is documented there.

## Discovery (the only mutating CLI piece)

```bash
t3 <overlay> pr sweep
```

This emits JSON listing every open PR authored by the user across the forge:

```json
{
  "author": "adrien.cossa",
  "count": 5,
  "prs": [
    {
      "iid": 1234,
      "title": "feat(scope): description",
      "web_url": "https://gitlab.com/org/repo/-/merge_requests/1234",
      "source_branch": "ac-myrepo-1234-...",
      "target_branch": "main",
      "draft": false,
      "references": {"full": "org/repo!1234"}
    }
  ]
}
```

The `author` field is resolved from the overlay's `get_gitlab_username()` (or the configured `<host>_username` in `~/.teatree.toml`) with a fallback to `host.current_user()`. Set the username explicitly when the configured user differs from the OAuth identity.

The CLI is intentionally read-only — it does not modify branches, push, or change CI. Mutating actions live in this skill so the agent can prompt for non-default-base PRs and conflict resolution.

## Per-PR Loop (sequential, not parallel)

For each PR in `prs`, in order:

### Gate 1 — Non-default base

Compare `target_branch` to the repo's default branch. If they differ, **stop and ask the user** via `AskUserQuestion`:

- Skip this PR (default).
- Merge the parent branch into the PR (stacked PR — keep dependency intact).
- Custom: ask for instructions.

**Do not change the target branch under any circumstances** — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Never Change MR Base Branch or Dependencies".

### Gate 2 — Approved

Check whether the PR has approvals. If approved and the merge will introduce new commits, ask before pushing:

- Skip — don't disturb a reviewed branch.
- Push anyway — the user has accepted re-review.

This guards against silent re-approval after an unrelated rebase. Canonical rule: see [`../rules/SKILL.md`](../rules/SKILL.md) § "Publishing Actions Are Mode-Conditional".

### Step 3 — Worktree

Find or create a worktree for this PR's source branch. Reuse an existing worktree only when its branch matches; otherwise create a fresh one with `t3 <overlay> workspace ticket <pr-url>`. Never edit the main clone.

### Step 4 — Merge default branch

```bash
git fetch origin <default>
git merge origin/<default>
```

**Never rebase.** The PR has been reviewed against shared history; rewriting it forces reviewers to re-read the whole diff.

If the merge is clean, continue. If conflicts arise:

- **Mechanical conflicts** (e.g., adjacent edits to a translations file, formatting drift): resolve and commit.
- **Semantic conflicts** (logic from main collides with the PR's logic): stop and ask the user via `AskUserQuestion` with the conflicting hunks. Do not guess.

### Step 5 — Local quality gates

If the merge changed the working tree, run prek + targeted tests for the affected files. If something breaks, fix before pushing — see [`../ship/SKILL.md`](../ship/SKILL.md) § "Self-Review Against Repo Rules".

### Step 6 — Push

Subject to mode (canonical rule: [`../rules/SKILL.md`](../rules/SKILL.md) § "Publishing Actions Are Mode-Conditional"):

- **`auto` mode:** push without confirmation.
- **`interactive` mode:** ask once via `AskUserQuestion` before the first push of the sweep, then apply that answer to every subsequent PR's push during the same sweep.

### Step 7 — CI watch

After push, watch the pipeline. On red, delegate to the existing fix-push-monitor loop (see [`../ship/SKILL.md`](../ship/SKILL.md) § "Monitor Pipeline" and [`../debug/SKILL.md`](../debug/SKILL.md)). When it goes green:

- **`bulk-update`** policy: mark the PR done and move to the next.
- **`serial-merge`** policy: continue to Step 8.

### Step 8 — Merge (serial-merge only)

Squash-merge the PR (`gh pr merge <iid> --squash` or the GitLab equivalent). On any merge failure (review required, branch protection block, conflict that snuck in between Step 5 and now) **stop the sweep and surface the failure** — do not silently skip to the next PR, because the next PR's update step would still be racing against the unmerged predecessor.

After a successful merge, **re-run the discovery CLI** (`t3 <overlay> pr sweep`) to refresh the "open PRs" list before picking the next entry. The list shrinks by one and any sibling PR may now be conflict-free where it wasn't before.

## Summary Table

After all PRs are processed, emit a summary:

| PR | Policy | Action | Result |
|----|--------|--------|--------|
| [org/repo!1234](https://gitlab.com/...) | bulk-update | merged main + pushed | green |
| [org/repo!1235](https://gitlab.com/...) | bulk-update | skipped | non-default base |
| [org/repo!1240](https://gitlab.com/...) | bulk-update | merged main + pushed | red — fix queued |
| [souliane/teatree#526](https://github.com/...) | serial-merge | merged main + pushed + squash-merged | landed |
| [souliane/teatree#527](https://github.com/...) | serial-merge | not reached | sweep stopped on #526 merge failure |

Use clickable references — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Clickable References".

## Rules

### Sequential Only (Non-Negotiable)

Process one PR at a time in the main conversation. Never dispatch parallel agents for sweep work — each one would race on git operations, post duplicate MR comments, and corrupt worktrees. See [`../rules/SKILL.md`](../rules/SKILL.md) § "Never Post MR Comments from Parallel Agents".

### Never Rebase (Non-Negotiable)

The sweep workflow is **merge-only**. Rebasing a reviewed branch destroys reviewers' line anchors and forces re-review. If a PR genuinely needs rebasing (e.g., to drop a sensitive commit), that is a separate, manual action — not part of the sweep.

### Stop on Approved + Non-Trivial Merge

When an approved PR's merge from main introduces non-trivial changes (new conflicts, reformatted files, regenerated lockfiles), surface the diff to the user before pushing. Reviewers approved the previous diff, not the new one.

### One Worktree Per PR

Never reuse one worktree across multiple PRs in a sweep. Each PR gets its own worktree (or its existing one), so the per-PR DB state and ports stay isolated. Cleanup happens via `t3 <overlay> workspace clean-all` after the sweep.

## Configuration

| `~/.teatree.toml` key | Purpose |
|---|---|
| `[overlays.<name>]` `gitlab_username_pass_key` | Pass-store key holding the GitLab username for the overlay. Resolves to `overlay.config.get_gitlab_username()`. |
| `[overlays.<name>]` `github_username` | Plain GitHub login for the overlay (no secret needed). |

Without a configured username, the sweep falls back to `host.current_user()` (the OAuth-authenticated identity). Set the username explicitly when the workforce identity differs from the bot identity.
