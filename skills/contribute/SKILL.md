---
name: contribute
description: Push retro improvements to a branch, open a PR, and optionally create upstream issues. Use when user says "contribute", "push improvements", "push skills", or after retro creates a local commit.
compatibility: macOS/Linux, git, gh CLI (for PRs and upstream issues).
requires:
  - retro
triggers:
  priority: 90
  keywords:
    - '\b(t3.?contribute|push improvements?|push skills?|contribute upstream)\b'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Contribute — Branch, PR & Upstream Issue Creation

Handles the **push and PR workflow** for skill improvements created by `t3:retro`. Retro commits improvements locally in `$T3_REPO`; this skill pushes them to a branch and opens a PR — with explicit user consent every time.

**All pushes go to branches, never to main.** The PR is the review gate.

## Dependencies

- `t3:retro` — creates the local commits that this skill pushes.

## Configuration

Uses these `~/.teatree` variables (set by `/t3:setup`):

- **`T3_CONTRIBUTE`** — must be `true` for this skill to do anything.
- **`T3_PUSH`** — `false` (default) or `true`. When `false`, this skill refuses to push and tells the user to push manually. Exists as a safety stop for privacy/secret review.
- **`T3_AUTO_PUSH_FORK`** — `false` (default) or `true`. When `true`, step 4 (push confirmation) is skipped **only** when the push target is the user's fork (`origin` push URL differs from `T3_UPSTREAM`). Upstream issue creation (step 6) always requires confirmation.
- **`T3_UPSTREAM`** — upstream repo (e.g., `souliane/teatree`). Controls where the PR is created:
  - **Empty** → push to branch on `origin`, create PR on `origin` (same repo).
  - **Set** → push to branch on `origin` (your fork), create PR targeting `T3_UPSTREAM`.
- **`T3_PRIVACY`** — `strict` (default) or `relaxed`. See `t3:retro` § Privacy Scan.

## Why Not Just `git push`?

Do **not** push retro commits with `git push` directly. This skill:

1. Ensures commits are on a feature branch (never pushes to main)
2. Runs pre-flight checks (pre-commit, tests, privacy scan)
3. Requires explicit user confirmation before every push
4. Opens a PR for review
5. Optionally creates an upstream issue when working from a fork

## Workflow

### 1. Find Unpushed Commits

```bash
cd "$T3_REPO"
git log --oneline @{upstream}..HEAD
```

If no unpushed commits exist, inform the user and stop.

Show the commit log to the user.

### 1b. Squash Option

If there is **more than one** unpushed commit, offer to squash following the squash rules in [`../t3:ship/SKILL.md`](../t3:ship/SKILL.md) § "Finalize Branch".

### 2. Pre-Flight Checks (all must pass)

1. **`T3_CONTRIBUTE=true`** — if not, stop: "Self-improvement is disabled. Set `T3_CONTRIBUTE=true` in `~/.teatree`."
2. **`T3_PUSH` is `true`** — if not, stop: "Pushing is disabled (`T3_PUSH=false`). Push manually with `git push` if you're sure."
3. **Has a push remote:** `git -C "$T3_REPO" remote -v` → shows a push URL for `origin`.
4. **Pre-commit passes:** `cd "$T3_REPO" && prek run --all-files` — fix first if it fails.
5. **All tests pass:** `cd "$T3_REPO" && uv run pytest` — must be green.
6. **Privacy scan passes:** see `t3:retro` § Privacy Scan. Scan the diff of unpushed commits.

### 3. Verify Branch

Retro commits should already be on the session's working branch (see `t3:retro` § Branch Selection). Verify you're not on `main`:

```bash
cd "$T3_REPO"
DEFAULT=$(git config init.defaultBranch || echo main)
[ "$(git branch --show-current)" = "$DEFAULT" ] && echo "ERROR: on default branch" && exit 1
```

If somehow on `main` (shouldn't happen — retro handles branch selection), create a new branch:

```bash
BRANCH="fix/retro-$(date +%Y%m%d)-$(git log -1 --format=%s | sed 's/[^a-zA-Z0-9]/-/g' | head -c 50)"
git checkout -b "$BRANCH"
```

### 4. Push Confirmation

**Default: every push requires explicit user consent.**

**Auto-push exception** (`T3_AUTO_PUSH_FORK=true`): skip the confirmation below and proceed directly to push when **all** of the following hold:

1. `T3_CONTRIBUTE=true` and `T3_PUSH=true` (already required to reach this step).
2. `T3_AUTO_PUSH_FORK=true`.
3. `origin`'s push URL does **not** match `T3_UPSTREAM` — the push lands on the user's fork, not upstream.
4. Pre-flight checks in § 2 all passed, including the privacy scan.

When any of the above fail, fall back to the confirmation flow below.

Show:

```text
════════════════════════════════════════════════════════════════
  PUSH REVIEW — <branch> (<N> unpushed commits)
════════════════════════════════════════════════════════════════

  Remote: origin (<push-url>)

  Commits to push:
  - <hash> fix(<skill>): <description>

  Files changed:
  - <file list with short stat>

  Privacy scan: PASSED

════════════════════════════════════════════════════════════════
  → Type "yes" to push, anything else to skip.
════════════════════════════════════════════════════════════════
```

**Do NOT push without explicit "yes" from the user.**

### 5. Push and Create PR

```bash
cd "$T3_REPO"
git push -u origin HEAD
```

Then create the PR:

- **`T3_UPSTREAM` is empty** → PR on `origin` (same repo):

  ```bash
  gh pr create --title "fix(<skill>): <title>" --body "<body>" --base main
  ```

- **`T3_UPSTREAM` is set** → PR targeting upstream:

  ```bash
  gh pr create --title "fix(<skill>): <title>" --body "<body>" \
    --repo "$T3_UPSTREAM" --head "<fork-owner>:<branch>" --base main
  ```

The PR body should include: summary of changes, files changed, validation status (pre-commit, tests, privacy scan).

### 6. Upstream Issue (if applicable)

When `T3_UPSTREAM` is set AND the PR landed on a fork (not upstream itself), optionally file a public upstream issue for visibility. See [`references/upstream-issue.md`](references/upstream-issue.md) for the full procedure — divergence gate, issue template, privacy/tone rules, and user-confirmation gate.

Skip this step entirely when `T3_UPSTREAM` is empty or when the PR already targets upstream.

## What NOT to Do

- Do not push without explicit user consent — except when `T3_AUTO_PUSH_FORK=true` and the push target is the user's fork (see § 4).
- Do not push to main — always use a branch.
- Do not auto-create upstream issues — § 6 always requires confirmation, regardless of `T3_AUTO_PUSH_FORK`.
- Do not create upstream issues from heavily diverged forks — they're not useful.
- Do not use `git push` directly — always go through this skill for retro commits.
- Do not create duplicate upstream issues — check for existing ones first.
