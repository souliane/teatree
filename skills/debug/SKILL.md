---
name: debug
description: Troubleshooting and fixing — something is broken, find and fix it. Use when user says "broken", "error", "not working", "crash", "blank page", "can't connect", "debug", or reports any failure.
compatibility: macOS/Linux, any language/framework supported by the project.
requires:
  - workspace
  - systematic-debugging
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Troubleshooting & Fixing

## Delegation

This skill delegates the generic debugging doctrine to:

- `systematic-debugging` — root-cause-first investigation
- `verification-before-completion` — evidence before claiming the fix worked

Optional [obra/superpowers](https://github.com/obra/superpowers) skills provide generic methodology. TeaTree keeps the project-specific workflow locally.

Reactive mode — something is wrong, find and fix it.

## Root Cause, One Fix

Diagnose the root cause and apply one targeted fix. Reactive patch loops (fix symptom → new symptom → patch that → new symptom) indicate the root cause was missed. When you find yourself applying a second patch, stop and re-diagnose from scratch.

### Scope the Class, Not One Call Site

When a bug fires at one location but the underlying defect lives in a shared helper, function, or pattern, fix the *class* of bug — never just the single failing call site. A null-deref that crashed at one caller is almost always reachable from every other caller of the same unguarded helper.

Do this — never patch one site and move on:

1. **Enumerate every call site before touching the fix.** Grep the whole tree for callers so you know the blast radius:

   ```bash
   grep -rn 'unguarded_helper(' src/      # every caller of the offending helper
   # or, if ripgrep is available:
   rg 'unguarded_helper\(' src/
   ```

2. **Fix the helper itself** (or the shared pattern) so all call sites are covered at once.
3. **Never** `echo "patched that one"` and stop — guarding only the one crashing caller leaves the same bug latent everywhere else.

## Verify a Recalled SHA Before Any Destructive Git

Before any destructive or history-moving git operation (`cherry-pick`, `reset --hard`, `rebase`, `revert`, force-push), re-verify the target against the live branch. A SHA recalled from earlier in the session — or from a handover/preamble — may be stale: branches get rewritten, rebased, or amended, and the hash you remember may no longer be the commit you mean. **Treat the recalled SHA as presumed-stale: the branch name in the live request is the authority, not the hash you remember.**

**Asked to "cherry-pick the X commit" with a vaguely-named commit and a remembered SHA, your single next action is the verify READ against the live branch — never the cherry-pick (do X, never Y).** This is acute when the live request names a DIFFERENT branch than the one your memory associates with the SHA (e.g. you recall `a1b2c3d` was on `fix/lint-cleanup`, but the request says the commit is on `feature/ruff-baseline`): the branch in the live request wins, and the SHA on it is almost certainly NOT the hash you remember. So your first tool call is `git log`/`git show`/`git rev-parse` against the branch named in the request — and you do NOT issue `git cherry-pick <remembered-sha>` as your first action.

```bash
# do X — first action is the verify READ against the live-request branch, then STOP to read its output:
git log --oneline feature/ruff-baseline
# never Y — do NOT cherry-pick the remembered SHA as the first action (it is presumed stale):
# git cherry-pick a1b2c3d   # FORBIDDEN first move — the recalled hash is unverified against the live branch
```

Do this — never act on a remembered hash:

1. **Read the source branch named in the live request to find the real, current SHA first** — a *separate* read whose OUTPUT you then read, never a one-liner that pipes straight into the destructive command:

   ```bash
   git log --oneline feature/ruff-baseline      # find the commit on its actual branch; READ the SHA it prints
   git show feature/ruff-baseline:<n>           # or inspect by branch-relative ref
   git rev-parse feature/ruff-baseline          # resolve the branch tip live
   ```

2. **Only then**, in a *new* command, run the destructive operation against the SHA you just read from that output:

   ```bash
   git cherry-pick <sha-just-read-from-the-log-above>   # the NEW SHA, not the remembered one
   ```

3. **Never** `git cherry-pick <sha-recalled-from-context>` directly — a hash from polluted or aged context is a guess until the live branch confirms it. The current SHA on the branch is frequently NOT the one you recalled; cherry-pick the one the log just printed.
4. **Never chain the verify and the act into one command** (`git log <branch> … && git cherry-pick <recalled-sha>`, or `git log -1 <recalled-sha> && git cherry-pick <recalled-sha>`). Chaining reuses the remembered hash you set out to distrust and bypasses the whole point: you must SEE the live SHA in the log output and pass THAT to cherry-pick in a deliberate second step.

## Dependencies

- **workspace** (required) — provides server restart and environment context. **Load `/t3:workspace` now** if not already loaded.

## MANDATORY: Read Troubleshooting First

Before attempting ANY fix, read the relevant troubleshooting documentation. This is non-negotiable — many issues have known solutions documented there.

## Systematic Debugging Protocol

### Phase 0: User Hints

When the user gives a debugging hint, **investigate that hint FIRST** before other theories. User hints are based on deep project knowledge — treat as most likely root cause.

### Phase 0b: Visual Evidence

When the bug report includes screenshots or videos, **analyze ALL visual evidence before writing any code.** Use `t3 tool analyze-video` for videos. Each frame may reveal additional issues beyond what the text description mentions.

**Browser-visible breakage is diagnosed IN the browser, before any root-cause guess.** When the symptom is something a user sees in a page — a blank render, a failed request, a console error, a wrong DOM state — inspect the live page's **network / console / DOM** first; do not propose a root cause from the Python/server side alone. **chrome-devtools-mcp** is teatree's default browser tool and exposes exactly that (plus navigate/click/fill to drive the page), over CDP with no claude.ai account or extension pairing. It ships **on by default** — run `t3 mcp browser-diagnosis` for the `claude mcp add` registration line (`t3 <overlay> config_setting set chrome_devtools_mcp_enabled false` turns it off on a host that cannot run it). It is a diagnostic/interaction aid only: **perf/trace enforcement stays in the deterministic Playwright lane**, never this server.

### Phase 0c: What Changed Recently?

When a user reports "this worked yesterday" or "this just started happening," check **what changed** before deep-diving into code:

1. `git log --oneline --since="3 days ago"` in relevant repos
2. File modification dates on config/hook files (`ls -la`)
3. Plugin/package updates (check cache timestamps, lockfiles)
4. Environment changes (new env vars, updated tools)

This is faster than reading every file in detail and often pinpoints the cause immediately.

### Phase 0d: Diff Against the Base Branch FIRST (before blaming code)

This is the PRIMARY first step for any regression or red CI on a feature branch. Before forming a single hypothesis about application code, confirm what *this branch* actually introduced relative to its base. The most common "mysterious" failure is a change the branch itself added (a stray symlink, a config edit, a generated artifact) — not pre-existing code.

Do this — never skip it:

1. **Diff (or log) against the base branch as your very first command.** Do not echo a guess like "probably pre-existing" / "flaky infra" / "not my change" before you have run it.

   ```bash
   # See exactly what this branch changed relative to its base
   git diff origin/main...HEAD          # full diff (use master if that is the base)
   git log --oneline origin/main..HEAD  # just the commits this branch added
   ```

   The `A...B` (three-dot) form diffs from the merge-base, so it shows only what the branch introduced — not unrelated drift on `main`.

2. **For a "worked last week" regression**, log what changed since the base instead of reading every file:

   ```bash
   git log --oneline origin/main..HEAD -- path/to/affected/area
   ```

Only after the base diff is clean do you move on to blaming application code.

### Phase 1: Root Cause Investigation

- **When the task says "run the command", issue it with a sensible placeholder — do not ask for a routine argument first.** A clear diagnostic instruction ("read its recent logs", "list the commits that touched this test") is actionable even when the exact path/service/branch is not spelled out: the missing piece is a fill-in-the-blank that does not change the command's shape, so supply the obvious value or a placeholder (`git log --oneline -- <path/to/test>`, `docker logs <service>`) and RUN it. Bouncing back "which file path?" stalls on a detail you were asked to demonstrate the command around. (See `t3:rules` § "Do Work Now" → "Run the command with one routine argument missing".)
- Read **full** error output, stack traces, logs. Do not skim.
- Identify the exact failure point (file, line, function).
- Check if the error is environment-specific: **test with the user's real env, not a sanitized one.** Do not use `unset VAR` or `env -i` to mask env issues — if the command fails in the user's shell, that's the bug. Find the source of the stale env var (`.zshrc`, direnv, `.env`) and fix it.
- **CI failures on feature branches:** the base-branch diff is the mandatory first action — not an optional one. The correct order is: confirm what the branch introduced *before* forming any hypothesis about a pre-existing cause.

    ```bash
    git diff origin/master...HEAD   # or origin/main...HEAD
    ```

  Asserting "pre-existing" / "flaky" / "infra" / "not my change" before that diff has run is the failure mode this rule exists to prevent. Symlinks, config files, and generated artifacts can sneak into commits and are invisible until you diff the base.
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

### Phase 5: Deliver

After fixing and verifying, the fix needs to be committed and pushed via a PR. **Load `/t3:ship`** (or the project's delivery skill) before committing. Never commit directly on main — create a worktree and branch first. (Source: `t3:rules` § Worktree-First Work)

## Escalation Rules

- **After 1 failed fix** → re-read error output carefully.
- **After 3+ failed fixes** → **STOP and ask the user.**

## Commands

| Command | When to use |
|---------|-------------|
| `t3 <overlay> run backend` | Restart backend after a fix |
| `t3 <overlay> run build-frontend` | Rebuild the frontend dist after a fix (nginx in compose picks up the new dist via the volume mount) |
| `t3 <overlay> worktree start` | Full restart when multiple services affected |
| `t3 ci fetch-errors` | Analyze CI error logs |

## Error Log Analysis

When analyzing errors:

1. Read the **complete** error output — don't truncate or skim.
2. Look for the **root cause**, not just the symptom (the first error in a cascade).
3. Check if the error is environment-specific (worktree, Docker, ports).
4. Cross-reference with troubleshooting docs before attempting fixes.

## Post-Fix Retrospective — Emit Signal, Do NOT Self-Retro (#837)

Retro is **orchestrator-only**. After resolving a non-obvious issue as a sub-agent, do **not** run `/t3:retro` as a per-fix synthesis step. Instead, **emit the lesson as structured signal into durable state** — task metadata or a `/tmp/t3-snapshot-*.md` snapshot — capturing what would otherwise become a troubleshooting entry, playbook update, or guardrail, then keep going. The orchestrator later synthesises across the whole session and biases the output to the smallest enforcement artifact (a gate, test, or hook), not a prose rule. The durability discipline (snapshots, durable task state) is load-bearing and unchanged — it is exactly the channel the orchestrator's synthesis reads from.
