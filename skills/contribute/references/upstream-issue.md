# Upstream Issue Creation

After creating the PR (§ 5 of the main workflow), check if an upstream issue should also be created (for visibility when the PR comes from a fork):

- **`T3_UPSTREAM` is not set** → stop, the PR is already on the same repo.
- **Fork's `origin` matches `T3_UPSTREAM`** → stop, the PR already landed upstream.
- **Otherwise** → proceed with divergence analysis and issue creation.

## 1. Divergence Analysis

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

## 2. Pre-Flight Checks

1. **`gh` is authenticated:** `gh auth status` succeeds.
2. **Fork is public:** `gh repo view <fork-owner>/<fork-name> --json isPrivate -q '.isPrivate'` → `false`. If private, **STOP**.
3. **Privacy scan passes** on the issue body.

## 3. Issue Content

Build the issue body with full context for the upstream repo:

```markdown
## Context

<What went wrong — GENERIC description only, no project names,
no internal URLs, no personal details>

## Suggested Changes

<Summary of what was changed and why>

## Pull Request

<link to the PR created in step 5>

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

## 4. Check for Related Issues

Before creating, search for existing open issues that touch the same skill:

```bash
gh issue list -R "$T3_UPSTREAM" --search "<skill-name> in:title" --state open
```

If related issues exist, mention them in the issue body under "Related upstream issues" and tell the user — they may want to comment on the existing issue instead of creating a new one.

## 5. User Confirmation (Non-Negotiable)

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

## 6. Create the Issue

```bash
gh issue create -R "$T3_UPSTREAM" \
  --title "fix(<skill>): <title>" \
  --body "<built issue body>"
```

After creation, print the issue URL.

## Issue Body Rules

- **Never include:** personal names, email addresses, company names, internal URLs, project-specific repo names, API keys, hostnames, IP addresses, file paths outside `$T3_REPO`, customer/tenant names.
- **Always include:** generic description of the problem, which core skill is affected, link to the fork branch, validation evidence, divergence status.
- **Tone:** technical and factual. Describe the gap and the fix, not the user's workflow.
