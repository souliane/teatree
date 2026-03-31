---
name: debug
description: Troubleshooting and fixing — something is broken, find and fix it. Use when user says "broken", "error", "not working", "crash", "blank page", "can't connect", "debug", or reports any failure.
compatibility: macOS/Linux, any language/framework supported by the project.
requires:
  - workspace
triggers:
  priority: 50
  keywords:
    - '\b(broken|error|not working|crash|blank page|can.t connect|debug|fix this|won.t start|500|traceback|exception)\b'
  urls:
    - 'https?://[^\s]*sentry\.[^\s]+/issues/'
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Troubleshooting & Fixing

## Delegation

This skill delegates the generic debugging doctrine to:

- `systematic-debugging` — root-cause-first investigation
- `verification-before-completion` — evidence before claiming the fix worked

TeaTree keeps the workflow-specific parts locally: service/worktree failure modes, repo scripts, and when to bounce back into lifecycle commands.

Reactive mode — something is wrong, find and fix it.

## Dependencies

- **t3-workspace** (required) — provides server restart and environment context. **Load `/t3:workspace` now** if not already loaded.

## MANDATORY: Read Troubleshooting First

Before attempting ANY fix, read the relevant troubleshooting documentation. This is non-negotiable — many issues have known solutions documented there.

## Systematic Debugging Protocol

### Phase 0: User Hints

When the user gives a debugging hint, **investigate that hint FIRST** before other theories. User hints are based on deep project knowledge — treat as most likely root cause.

### Phase 0b: Visual Evidence

When the bug report includes screenshots or videos, **analyze ALL visual evidence before writing any code.** Use `t3 tool analyze-video` for videos. Each frame may reveal additional issues beyond what the text description mentions.

### Phase 1: Root Cause Investigation

- Read **full** error output, stack traces, logs. Do not skim.
- Identify the exact failure point (file, line, function).
- Check if the error is environment-specific: **test with the user's real env, not a sanitized one.** Do not use `unset VAR` or `env -i` to mask env issues — if the command fails in the user's shell, that's the bug. Find the source of the stale env var (`.zshrc`, direnv, `.env`) and fix it.
- **CI failures on feature branches:** always `git diff master...HEAD` first to confirm what the branch introduced. Do not speculate about pre-existing causes without checking the base branch. Symlinks, config files, and generated artifacts can sneak into commits.
- **CI fails but local passes:** follow this checklist in order:
  1. **Is the latest commit pushed?** Compare local HEAD with remote HEAD (`git log origin/<branch> --oneline -1`). Unpushed fixes are the most common cause.
  2. **Check ALL failed jobs**, not just the primary one. Large builds (e.g., full frontend apps) can swallow errors in output buffers; smaller builds of the same codebase often show errors more clearly.
  3. **Check the merge ref.** CI may test a merge of your branch with the target branch. Merge locally (`git merge --no-commit origin/master`) and rebuild to reproduce.
  4. **Check node/package versions.** CI does `npm ci` (clean install from lockfile); local `node_modules` may have drifted.
- Check if the error message matches a known pattern in troubleshooting docs.

### Phase 2: Pattern Analysis

Common failure categories:

| Category | Typical symptoms |
|---|---|
| Service startup | DB hang, port conflict, migration error |
| Docker/backend | Redis networking, migration gap, DB import chain |
| Frontend | Blank page, XHR cache, translation sync, build error |
| Worktree-specific | DB exists, port in use, direnv not loaded, DSLR failure |
| Network | Connection refused, timeout (check VPN first) |
| CI-specific | Build fails in CI but passes locally (see Phase 1 checklist) |

### Phase 3: Hypothesis Testing

- ONE hypothesis at a time.
- Add diagnostic logging if needed.
- Verify or disprove before moving to next hypothesis.

### Phase 4: Fix and Verify

- Fix the confirmed root cause.
- Remove diagnostic logging.
- Verify the fix with concrete evidence.

## Escalation Rules

- **After 1 failed fix** → re-read error output carefully.
- **After 3+ failed fixes** → **STOP and ask the user.**

## Commands

| Command | When to use |
|---------|-------------|
| `t3 <overlay> run backend` | Restart backend after a fix |
| `t3 <overlay> run frontend` | Restart frontend after a fix |
| `t3 <overlay> lifecycle start` | Full restart when multiple services affected |
| `t3 ci fetch-errors` | Analyze CI error logs |

## Error Log Analysis

When analyzing errors:

1. Read the **complete** error output — don't truncate or skim.
2. Look for the **root cause**, not just the symptom (the first error in a cascade).
3. Check if the error is environment-specific (worktree, Docker, ports).
4. Cross-reference with troubleshooting docs before attempting fixes.

## Post-Fix Retrospective

After resolving a non-obvious issue, run `/t3:retro` to capture the lesson (troubleshooting entry, playbook update, or guardrail) so it doesn't recur.
