---
name: t3-contribute
description: Push retro improvements to your fork and optionally open upstream issues. Use when user says "contribute", "push improvements", "push skills", "t3-contribute", or after t3-retro creates a local commit.
compatibility: macOS/Linux, git, gh CLI (for upstream issues).
requires:
  - t3-retro
triggers:
  priority: 90
  keywords:
    - '\b(t3.?contribute|push improvements?|push skills?|contribute upstream)\b'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Contribute — Push & Upstream Issue Creation

Handles the **push and upstream interaction** for skill improvements created by `t3-retro`. Retro commits improvements directly on the current branch in `$T3_REPO`; this skill reviews unpushed commits and pushes them — with explicit user consent every time.

## Dependencies

- `t3-retro` — creates the local commits that this skill pushes.

## Configuration

Uses these `~/.teatree` variables (set by `/t3-setup`):

- **`T3_CONTRIBUTE`** — must be `true` for this skill to do anything.
- **`T3_PUSH`** — `false` (default) or `true`. When `false`, this skill refuses to push and tells the user to push manually. Exists as a hard safety stop.
- **`T3_UPSTREAM`** — upstream GitHub repo (e.g., `souliane/teatree`). If set and origin differs, this skill opens an issue after pushing.
- **`T3_PRIVACY`** — `strict` (default) or `relaxed`. See `t3-retro` § Privacy Scan.

## Why Not Just `git push`?

Do **not** push retro commits with `git push` directly. This skill:

1. Runs pre-flight checks (pre-commit, tests, privacy scan)
2. Requires explicit user confirmation before every push
3. Performs divergence analysis before creating upstream issues
4. Includes fork/upstream metadata in issues so upstream reviewers can evaluate relevance

Without this, improvements may leak private data, fail quality gates, or create useless upstream issues from heavily diverged forks.

## Workflow

### 1. Find Unpushed Commits

```bash
cd "$T3_REPO"
git log --oneline @{upstream}..HEAD
```

If no unpushed commits exist, inform the user and stop.

Show the commit log to the user.

### 1b. Squash Option

If there is **more than one** unpushed commit, offer to squash following the canonical squash rules from `ac-managing-repos` § Workflow 2 — Squash & Prepare. Key points: group by topic, keep human-sized, never rewrite pushed history, squash integrity check. Respect `T3_AUTO_SQUASH`.

### 2. Pre-Flight Checks (all must pass)

1. **`T3_CONTRIBUTE=true`** — if not, stop: "Self-improvement is disabled. Set `T3_CONTRIBUTE=true` in `~/.teatree`."
2. **`T3_PUSH` is `true`** — if not, stop: "Pushing is disabled (`T3_PUSH=false`). Push manually with `git push` if you're sure."
3. **Has a push remote:** `git -C "$T3_REPO" remote -v` → shows a push URL for `origin`.
4. **Pre-commit passes:** `cd "$T3_REPO" && prek run --all-files` — fix first if it fails.
5. **All tests pass:** `cd "$T3_REPO" && uv run pytest` — must be green.
6. **Privacy scan passes:** see `t3-retro` § Privacy Scan. Scan the diff of unpushed commits.

### 3. Push Confirmation (Non-Negotiable)

**Every push requires explicit user consent.** No exceptions, no config to bypass this.

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

### 4. Push

```bash
cd "$T3_REPO"
git push
```

### 5. Upstream Issue (if applicable)

After pushing, check if an upstream issue should be created:

- **`T3_UPSTREAM` is not set** → stop, no issue needed.
- **Fork's `origin` matches `T3_UPSTREAM`** → stop, the push already landed upstream.
- **Otherwise** → proceed with divergence analysis and issue creation.

#### 5a. Divergence Analysis (Non-Negotiable)

Before creating an issue, run the divergence check (requires an `upstream` git remote):

```bash
t3 ci divergence
```

The command prints `<ahead> ahead, <behind> behind upstream`. Parse the counts to decide whether to proceed.

**If divergence is excessive (>50 fork-only commits or >20 upstream-only commits):**

```text
════════════════════════════════════════════════════════════════
  UPSTREAM ISSUE BLOCKED — Fork too diverged
════════════════════════════════════════════════════════════════

  Your fork has diverged significantly from upstream:
  - N commits on your fork not in upstream
  - M commits on upstream not in your fork
  - Common base: <merge-base-hash> (<date>)

  The retro improvement may not apply cleanly to upstream.
  Merge upstream first, or skip the upstream issue.
════════════════════════════════════════════════════════════════
```

Stop — do not create the issue.

#### 5b. Pre-Flight Checks

1. **`gh` is authenticated:** `gh auth status` succeeds.
2. **Fork is public:** `gh repo view <fork-owner>/<fork-name> --json isPrivate -q '.isPrivate'` → `false`. If private, **STOP**.
3. **Privacy scan passes** on the issue body.

#### 5c. Issue Content

Build the issue body with full context for the upstream repo:

```markdown
## Context

<What went wrong — GENERIC description only, no project names,
no internal URLs, no personal details>

## Suggested Changes

<Summary of what was changed and why>

## Fork Branch

<link to fork branch>

## Files Changed

<list with one-line description per file>

## Fork/Upstream Status

- **Common base:** `<merge-base-hash>` (<date>, <subject>)
- **Fork-only commits:** N
- **Upstream-only commits:** M
- **Related upstream issues:** <links to any open issues on T3_UPSTREAM that reference the same skill files, or "none found">

## Commits

<list of pushed retro commits with hash + message>

## Validation

- Pre-commit: passing
- Tests: passing (N tests)
- Privacy scan: passing
```

#### 5d. Check for Related Issues

Before creating, search for existing open issues that touch the same skill:

```bash
gh issue list -R "$T3_UPSTREAM" --search "<skill-name> in:title" --state open
```

If related issues exist, mention them in the issue body under "Related upstream issues" and tell the user — they may want to comment on the existing issue instead of creating a new one.

#### 5e. User Confirmation (Non-Negotiable)

**Display the full issue for review before creating it:**

```text
════════════════════════════════════════════════════════════════
  UPSTREAM ISSUE — REVIEW BEFORE CONFIRMING
════════════════════════════════════════════════════════════════

  Will be posted as a PUBLIC issue on: <T3_UPSTREAM>

  Title: fix(<skill>): <title>

  Body:
  ---
  <full issue body>
  ---

  References your PUBLIC fork:
  https://github.com/<fork-owner>/<fork-name>

  Related open issues: <links or "none found">

════════════════════════════════════════════════════════════════
  → Type "yes" to create the issue, anything else to skip.
════════════════════════════════════════════════════════════════
```

**Do NOT proceed without explicit "yes".**

#### 5f. Create the Issue

```bash
gh issue create -R "$T3_UPSTREAM" \
  --title "fix(<skill>): <title>" \
  --body "<built issue body>"
```

After creation, print the issue URL.

### Issue Body Rules (Non-Negotiable)

- **Never include:** personal names, email addresses, company names, internal URLs, project-specific repo names, API keys, hostnames, IP addresses, file paths outside `$T3_REPO`, customer/tenant names.
- **Always include:** generic description of the problem, which core skill is affected, link to the fork branch, validation evidence, divergence status.
- **Tone:** technical and factual. Describe the gap and the fix, not the user's workflow.

## What NOT to Do

- Do not push without explicit user consent — ever.
- Do not create upstream issues from heavily diverged forks — they're not useful.
- Do not use `git push` directly — always go through this skill for retro commits.
- Do not create duplicate upstream issues — check for existing ones first.
