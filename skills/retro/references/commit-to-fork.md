# Commit to Fork (`T3_CONTRIBUTE=true`) — Automatic

When `T3_CONTRIBUTE=true` and retro modified files under `$T3_REPO`, **proceed to commit automatically** — do not wait for the user to ask. The commit is local-only (never pushes), so it is safe to create without confirmation. This ensures improvements are captured immediately rather than forgotten.

## Pre-Flight Checks (all must pass)

1. **Repo is a full clone:** `git -C "$T3_REPO" rev-parse --is-shallow-repository` → `false`
2. **Pre-commit passes:** `cd "$T3_REPO" && prek run --all-files` — if it fails, fix first.
3. **All tests pass:** `cd "$T3_REPO" && uv run pytest` — must be green.
4. **Privacy scan passes:** see § Privacy Scan.

## Never Work on Main (Non-Negotiable)

**NEVER commit to the default branch (`main`/`master`) directly. NEVER push to it.** Always work on a feature branch in a worktree. This applies to retro commits, skill edits, "quick fixes" — everything. No exceptions.

## Worktree for Retro Commits (Non-Negotiable)

**All retro commits MUST happen in a worktree — never in the main clone.** Even for "small" skill fixes. Use `t3 workspace ticket` or `EnterWorktree` to get a worktree before touching any file.

If you are already in a worktree from the session, commit there. If not, create one now. When unsure which worktree to use, **ask the user** with `AskUserQuestion`.

## Branch Selection for Retro Commits

Retro commits go on the **teatree branch that was already used during the session** — never on a dedicated `retro-findings` or `retro/*` branch. Rules:

1. **Session used a teatree branch in a worktree** (e.g., `feat/dashboard-fix`) → commit there.
2. **Session's branch was already merged** → create a new worktree from `main` (e.g., `fix/retro-<topic>`) and open an MR.
3. **Session didn't touch any teatree branch** → create a new worktree from `main`, commit, and open an MR.

## Commit

```bash
cd "$T3_REPO_WORKTREE"   # the worktree path, NOT the main clone
git add <changed files>
git commit -m "fix(<skill>): <what was learned>"
```

## Squashing Retro Commits

Squash retro commits into clean, human-sized units **before chaining to the review skill**. Follow the squash rules in `../t3:ship/SKILL.md` § "Finalize Branch".

## After Committing

**Always inform the user:**

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

**Ask the user whether to push** using `AskUserQuestion` — even when `T3_PUSH=false`. Show the branch name and commit hash so the user can make an informed decision. If they say yes, load `/t3:contribute` and run it. If they decline, remind them to run `/t3:contribute` later.

**Auto-push exception** (`T3_AUTO_PUSH_FORK=true`): skip the confirmation above and chain directly into `/t3:contribute` when **all** of the following hold:

1. `T3_PUSH=true` — pushing is globally enabled.
2. `T3_AUTO_PUSH_FORK=true` — auto-push to fork is opted in.
3. `origin`'s push URL does **not** match `T3_UPSTREAM` — the push lands on the user's fork, not upstream.
4. The privacy scan (§ Privacy Scan) passed.

When any of these fail, fall back to the confirmation flow. Upstream issue creation always requires explicit confirmation regardless of `T3_AUTO_PUSH_FORK`.

## Chain to Review Skill

After committing and squashing, if `T3_REVIEW_SKILL` is configured in `~/.teatree`, offer to chain into the review skill:

```text
Retro complete. Chain to cross-repo review? (T3_REVIEW_SKILL=ac-reviewing-codebase)
```

The review skill will then squash its own commits and chain into its `DELIVERY_SKILL` (e.g., `ac-reviewing-codebase`) for infrastructure audit and final delivery status.
