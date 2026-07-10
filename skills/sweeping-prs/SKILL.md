---
name: sweeping-prs
description: Maintenance sweep across all your open PRs/PRs — merge the default branch, fix conflicts, monitor CI, push, and (per-repo policy) optionally squash-merge each PR before moving to the next. Never rebases. Use when user says "sweep PRs", "update all my PRs", "merge main into open PRs", or wants to keep open PRs up to date with main.
compatibility: macOS/Linux, zsh, git, issue tracker CLI (glab, gh).
requires:
  - workspace
  - ship
  - rules
  - platforms
metadata:
  version: 0.0.1
  subagent_safe: false
---

# PR Sweep — Batch Maintenance for Open PRs

Walk every open MR/PR you authored, sequentially, and bring each up to date with the default branch:

1. Merge `origin/<default>` into the source branch (**never rebase**).
2. If conflicts are mechanical, resolve and continue. If not, prompt the user.
3. Push.
4. Watch CI. On red, hand off to the existing `/t3:debug` + `/t3:ship` fix-push-monitor loop.
5. Re-read the PR's live merge state (Step 7.5) and **skip it if it is already merged** — the user may be merging in parallel.
6. Depending on the per-repo policy (see § Per-Repo Policy below), either merge the PR via the §17.4 keystone — `t3 <overlay> ticket merge <clear_id>`, Step 8 — before moving on, or stop at "green and up to date".
7. After the burst, **health-check `origin/main`** (`makemigrations --check --dry-run`, Step 9) before declaring done.

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
| **`serial-merge`** | For each open PR: update from `<default>` → push → wait for CI **green** → re-read live merge state and skip if already merged (Step 7.5) → merge via the §17.4 keystone (`ticket clear` → `t3 <overlay> ticket merge <clear_id>`, Step 8) → fetch the next PR (next iteration sees the just-landed commit as part of `main`) → after the burst, health-check `main` (Step 9). | Repos the user fully owns and wants drained (e.g. `souliane/teatree`, `souliane/skills`) without piling conflict cascades onto the next PR. |

Repos absent from `SWEEP_POLICY` default to `bulk-update` so existing behavior is unchanged for unconfigured repos.

### Mergeable colleague-facing MRs are notify-only

When a self-authored MR on a **colleague-facing** repo turns green, is not draft, not conflicted, and up to date with main but has no independent CLEAR, the loop's `PrSweepScanner` does NOT auto-merge it and does NOT auto-request review (a colleague-facing overlay runs `autonomy notify`, whose resolved `review_request_post_disabled = true` blocks the review-request post). It DMs you the MR link + "mergeable, ready to request review" **once per head** (idempotent via the `MergeableNotified` ledger; re-fires only on a new commit), so you can decide when to request a colleague's review. The DM is the only action — colleague review remains the merge gate.

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
  "author": "<your-handle>",
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

The `author` field is resolved from the overlay's `get_gitlab_username()` (or the configured `<host>_username` in the overlay's DB `overlays` registry row) with a fallback to `host.current_user()`. Set the username explicitly when the configured user differs from the OAuth identity.

The CLI is intentionally read-only — it does not modify branches, push, or change CI. Mutating actions live in this skill so the agent can prompt for non-default-base PRs and conflict resolution.

## Per-PR Loop (sequential, not parallel)

For each PR in `prs`, in order:

### Gate 1 — Non-default base

Compare `target_branch` to the repo's default branch. If they differ, **stop and ask the user** via `AskUserQuestion`:

- Skip this PR (default).
- Merge the parent branch into the PR (stacked PR — keep dependency intact).
- Custom: ask for instructions.

**Do not change the target branch under any circumstances** — see [`../rules/SKILL.md`](../rules/SKILL.md) § "Never Change PR Base Branch or Dependencies".

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
- **`serial-merge`** policy: continue to Step 7.5, then Step 8.

### Step 7.5 — Re-read live merge state (before every merge, non-negotiable)

A sweep often runs while the user is *also* merging PRs by hand. Before you act on any PR — and especially before Step 8 — **re-read that PR's live merge state and skip it if it is already merged.** Do — never assume the discovery snapshot is still current. Prefer the `mcp__teatree__github_pr_get` / `mcp__teatree__gitlab_pr_get` MCP tool — it returns the live `{open_state, merge_state, draft, author, approvals}` for one PR as structured JSON, no text parsing; fall back to the forge CLI only when the MCP server isn't connected:

```bash
# CLI fallback (MCP server not connected) — GitHub: re-read live state for THIS PR
gh pr view <pr-number> --repo <org/repo> --json state,mergedAt,mergeStateStatus,reviewDecision

# CLI fallback — GitLab: same live re-read
glab mr view <iid> --repo <org/repo> --output json   # inspect "state" / "merged_at"
```

Do:

1. Run the live re-read above for the PR you are about to touch.
2. If `state` is `MERGED`/`merged` (or `mergedAt`/`merged_at` is non-null), **skip it** — mark it "already merged (by hand)" in the summary and move to the next PR. Never re-merge.
3. Only if it is still open do you proceed to Step 8.

Never call `gh pr merge` / `glab mr merge` here — this step only *reads*. The actual merge is the keystone in Step 8. This is the merge-burst reconcile rule: see [`../rules/SKILL.md`](../rules/SKILL.md) § "Never Post PR Comments from Parallel Agents" for why two actors on one PR must never both merge it.

### Step 8 — Merge (serial-merge only)

Merge through the §17.4 keystone. Do — never reach for a raw forge merge (raw `gh pr merge` / `glab mr merge` and the old `t3 <overlay> pr merge` are mechanically refused — they bypass `MergeClear` validation / `expected_head_oid` / audit / `mark_merged()`). Two `t3` steps, maker != checker:

1. The orchestrator (coordinator) issues the per-diff CLEAR after its independent cold review of this PR's exact head, and captures the printed `clear_id` to hand to the loop:

   ```bash
   t3 <overlay> ticket clear <pr> <slug> \
     --reviewed-sha <sha> \
     --reviewer-identity <independent-reviewer> \
     --blast-class <substrate|logic|docs>
   # → prints CLEAR_ID=<clear_id>  (pass it to the loop's merge step below)
   ```

2. The durable loop runs the keystone merge with that `clear_id` — **this is the only sanctioned merge command in the sweep**:

   ```bash
   t3 <overlay> ticket merge <clear_id>
   # substrate change → carry the recorded human approval:
   t3 <overlay> ticket merge <clear_id> --human-authorized <id>
   ```

   It re-verifies live head SHA == `reviewed_sha`, live checks green, not-draft, and binds the merge to `expected_head_oid`. The #764 noreply-author guarantee is preserved by the server-side squash. The loop NEVER self-issues its own CLEAR (§17.8 clause 3).

**Do — never:** the merge MUST be `t3 <overlay> ticket merge <clear_id>`. NEVER run `gh pr merge`, `glab mr merge`, or `t3 <overlay> pr merge` — they stay mechanically prohibited (#863) precisely because they skip the keystone's live re-verification.

Substrate CLEARs are never swept-merged — they require an explicit recorded human approval (`--human-authorize <id>` at issue); the agent then executes the merge with `--human-authorized <id>`. The human approves; the agent merges. On any pre-condition failure or `escalated` result (review required, branch protection block, head moved, conflict that snuck in between Step 5 and now) **stop the sweep and surface the failure** — do not silently skip to the next PR, because the next PR's update step would still be racing against the unmerged predecessor.

After a successful merge, **re-run the discovery CLI** to refresh the "open PRs" list before picking the next entry. The list shrinks by one and any sibling PR may now be conflict-free where it wasn't before:

```bash
t3 <overlay> pr sweep
```

### Step 9 — Health-check `main` after the burst (non-negotiable)

A burst of merges — yours plus any the user landed by hand in parallel — can fork the migration graph: two PRs each add a migration off the *same* parent, so each is fine alone but together `main` has two leaf migrations and `migrate` fails on a fresh DB. Before declaring the sweep done, **confirm `origin/main`'s migration graph is still linear.** Do — never declare done on an unverified `main`:

```bash
git fetch origin <default> && git checkout origin/<default>
uv run python manage.py makemigrations --check --dry-run   # exit 0 = no fork / no missing migration
```

A non-zero exit (or "Merge multiple leaf nodes" / "would create migrations") means the burst forked the graph — **stop and reconcile** with `makemigrations --merge` in a worktree before calling the sweep finished.

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

Process one PR at a time in the main conversation. Never dispatch parallel agents for sweep work — each one would race on git operations, post duplicate PR comments, and corrupt worktrees. See [`../rules/SKILL.md`](../rules/SKILL.md) § "Never Post PR Comments from Parallel Agents".

### Never Rebase (Non-Negotiable)

The sweep workflow is **merge-only**. Rebasing a reviewed branch destroys reviewers' line anchors and forces re-review. If a PR genuinely needs rebasing (e.g., to drop a sensitive commit), that is a separate, manual action — not part of the sweep.

### Stop on Approved + Non-Trivial Merge

When an approved PR's merge from main introduces non-trivial changes (new conflicts, reformatted files, regenerated lockfiles), surface the diff to the user before pushing. Reviewers approved the previous diff, not the new one.

### One Worktree Per PR

Never reuse one worktree across multiple PRs in a sweep. Each PR gets its own worktree (or its existing one), so the per-PR DB state and ports stay isolated. Cleanup happens via `t3 <overlay> workspace clean-all` after the sweep.

## Configuration

| Overlay `overlays`-registry key | Purpose |
|---|---|
| `gitlab_username_pass_key` | Pass-store key holding the GitLab username for the overlay. Resolves to `overlay.config.get_gitlab_username()`. |
| `github_username` | Plain GitHub login for the overlay (no secret needed). |

Without a configured username, the sweep falls back to `host.current_user()` (the OAuth-authenticated identity). Set the username explicitly when the workforce identity differs from the bot identity.
