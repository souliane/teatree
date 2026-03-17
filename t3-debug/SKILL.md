---
name: t3-debug
description: Troubleshooting and fixing — something is broken, find and fix it. Use when user says "broken", "error", "not working", "crash", "blank page", "can't connect", "debug", or reports any failure.
compatibility: macOS/Linux, any language/framework supported by the project.
requires:
  - t3-workspace
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Troubleshooting & Fixing

Reactive mode — something is wrong, find and fix it.

## Dependencies

- **t3-workspace** (required) — provides server restart and environment context. **Load `/t3-workspace` now** if not already loaded.

## MANDATORY: Read Troubleshooting First

Before attempting ANY fix, read the relevant troubleshooting documentation. This is non-negotiable — many issues have known solutions documented there.

## Systematic Debugging Protocol

### Phase 0: User Hints

When the user gives a debugging hint, **investigate that hint FIRST** before other theories. User hints are based on deep project knowledge — treat as most likely root cause.

### Phase 1: Root Cause Investigation

- Read **full** error output, stack traces, logs. Do not skim.
- Identify the exact failure point (file, line, function).
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

## Scripts

| Script | When to use |
|--------|-------------|
| `t3 run backend` | Restart backend after a fix |
| `t3 run frontend` | Restart frontend after a fix |
| `t3 lifecycle start` | Full restart when multiple services affected |
| `t3 ci fetch-errors` | Analyze CI error logs (ext: `wt_fetch_ci_errors`) |

## Error Log Analysis

When analyzing errors:

1. Read the **complete** error output — don't truncate or skim.
2. Look for the **root cause**, not just the symptom (the first error in a cascade).
3. Check if the error is environment-specific (worktree, Docker, ports).
4. Cross-reference with troubleshooting docs before attempting fixes.

## Post-Fix Retrospective

After resolving a non-obvious issue, run `/t3-retro` to capture the lesson (troubleshooting entry, playbook update, or guardrail) so it doesn't recur.
