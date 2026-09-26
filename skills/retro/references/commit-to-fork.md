# Commit to Fork (`T3_CONTRIBUTE=true`) — Automatic

When `T3_CONTRIBUTE=true` and retro modified files under `$T3_REPO`, **proceed to commit automatically** — do not wait for the user to ask. The commit is local-only (never pushes), so it is safe to create without confirmation. This ensures improvements are captured immediately rather than forgotten.

## Pre-Flight Checks (all must pass)

1. **Repo is a full clone:** `git -C "$T3_REPO" rev-parse --is-shallow-repository` → `false`
2. **Full gate set passes:** `cd "$T3_REPO" && t3 tool verify-gates` — runs BOTH commit- and push-stage hooks (a bare `prek run --all-files` skips the push-stage gates CI re-runs); if it fails, fix first.
3. **Affected tests pass:** `cd "$T3_REPO" && bash dev/test-affected.sh` — must be green. The diff-scoped lane, not the whole suite: it escalates to FULL itself on anything it cannot prove local, and CI's sharded lane is the whole-tree authority ([#3994](https://github.com/souliane/teatree/issues/3994)).
4. **Privacy scan passes:** see § Privacy Scan.

## Worktree, Never the Main Clone, Never `main`

Canonical rule: [`../../rules/SKILL.md`](../../rules/SKILL.md) § "Worktree-First Work (Non-Negotiable)". It binds retro commits like everything else — "small" skill fixes included. Commit in the session's existing worktree when there is one; otherwise create one before touching any file.

## Branch Selection for Retro Commits

Retro commits go on the **teatree branch that was already used during the session** — never on a dedicated `retro-findings` or `retro/*` branch. Rules:

1. **Session used a teatree branch in a worktree** (e.g., `feat/loop-scanner`) → commit there.
2. **Session's branch was already merged** → create a new worktree from `main` (e.g., `fix/retro-<topic>`) and open a PR.
3. **Session didn't touch any teatree branch** → create a new worktree from `main`, commit, and open a PR.

## Commit

```bash
cd "$T3_REPO_WORKTREE"   # the worktree path, NOT the main clone
git add <changed files>
git commit -m "fix(<skill>): <what was learned>"
```

## Squashing Retro Commits

Squash retro commits into clean, human-sized units **before chaining to the review skill**. Follow the squash rules in `../ship/SKILL.md` § "Finalize Branch".

## After Committing

**In `interactive` mode, inform the user:**

```text
════════════════════════════════════════════════════════════════
  SKILL IMPROVEMENT COMMITTED (not pushed)

  Branch: <current-branch>
  Commit: <hash> — fix(<skill>): <what was learned>

  To review and push, run: /t3:contribute
  Do NOT use "git push" directly — /t3:contribute handles
  push confirmation, upstream issues, and divergence checks.
════════════════════════════════════════════════════════════════
```

**Whether to push is the mode's decision, not this file's.** Resolve the effective mode and follow it: `auto` pushes and opens the PR once the privacy scan passes, with no confirmation; `interactive` stops at the local commit and leaves the push to `/t3:contribute`. The resolution order — and the legacy `T3_PUSH` / `T3_AUTO_PUSH_FORK` vars the `mode` setting subsumes — is in [`../SKILL.md`](../SKILL.md) § Configuration and [`../../rules/SKILL.md`](../../rules/SKILL.md) § "Publishing Actions Are Mode-Conditional". Upstream issue creation needs explicit confirmation under either mode.

## Chain to Review Skill

After committing and squashing, if a review skill is configured (the `review_skill` DB setting, or the `T3_REVIEW_SKILL` env var), offer to chain into the review skill:

```text
Retro complete. Chain to cross-repo review? (T3_REVIEW_SKILL=ac-reviewing-codebase)
```

The review skill will then squash its own commits and chain into its `DELIVERY_SKILL` (e.g., `ac-reviewing-codebase`) for infrastructure audit and final delivery status.
