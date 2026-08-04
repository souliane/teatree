# Stranded worktree change sets, preserved as patches

**These are unreviewed third-party WIP snapshots preserved for recovery. They are NOT proposed
changes.** Nothing here has been reviewed, tested, or judged fit to merge. Each patch is a verbatim
capture of what was sitting uncommitted in someone else's working tree, taken so that a worktree
sweep could not destroy it. Do not apply any of these expecting green CI.

## Why they are patches and not commits

Each of these ten change sets was refused a direct `git commit` by this repo's own pre-commit
gates — the unfinished code does not pass `ruff`, `ty-check`, `module-health`, or `codespell`.
Editing a stranger's half-finished work purely to satisfy a linter would alter the very content
being preserved, so the work was captured as patches instead. Patches are not `.py`, so the
source-lint gates do not apply to them.

## Why they are inside a tarball rather than loose files

Committing the patches as loose `.patch` files does not work: the `trailing-whitespace` pre-commit
hook rewrites them, and stripping trailing whitespace from a diff is **corruption** — a blank context
line in a unified diff is a single space, and removing it makes the patch fail to apply. Nine of the
ten were altered that way on the first attempt. Storing them in a gzipped tar keeps the bytes exactly
as captured, because the text hooks do not rewrite binary files.

`--no-verify` was not used anywhere.

## How to recover one

```bash
tar xzf salvage-patches/stranded-worktree-patches.tar.gz
```

Then, from a worktree at the same base commit:

```bash
git apply <name>.tracked.patch
```

Each patch was produced with `git diff HEAD` against the worktree's own HEAD at capture time, so it
includes both staged and unstaged tracked changes. Files that were untracked had already been staged
at capture time, so they appear in the patch as new-file diffs; no separate untracked archive was
needed for any of the ten.

## Contents

| Patch | Source worktree | Original branch | Files | Gate that refused the direct commit |
|---|---|---|---|---|
| `4185-preset-admitted-chain-test` | `~/wt/4185-loop-admission` | `4185-timer-chain-admission` | 1 | `ruff-check` — `F401` unused `uuid`, `PLC0415` non-top-level import, `PLC2801` dunder `__enter__` call |
| `3848-structural-test-running-contract` | `~/teatree-deploy/.claude/worktrees/agent-a823b02a97e057170` | `3848-fix-structural-test-running-contract` | 11 | `ty-check` exit 1 |
| `3855-headless-first-question-contract` | `~/teatree-deploy/.claude/worktrees/agent-abfbc580c6d635e49` | `fix/3855-headless-first-slack-question-contract` | 26 | `ruff-check` exit 1 |
| `clickable-references-eval-scenarios` | `~/teatree-deploy/.claude/worktrees/agent-a214cb58e60b0abeb` | `worktree-agent-a214cb58e60b0abeb` | 24 | `codespell` exit 65 |
| `3865-lane-status-integrity` | `~/teatree-deploy/.claude/worktrees/agent-a1f84b0b4a01b61da` | `worktree-agent-a1f84b0b4a01b61da` | 22 | `ruff-check` exit 1 |
| `3854-provenance-vocabulary-dedup` | `~/teatree-deploy/.claude/worktrees/agent-a711436c3d63307ec` | `fix/3854-provenance-vocabulary-dedup` | 24 | `ruff-check` exit 1 |
| `dash-live-sessions-tests` | `~/teatree-deploy/.claude/worktrees/agent-a3bd46f65d266d9d7` | `feat/dash-live-sessions-view` | 16 | `ty-check` exit 1 |
| `3570-short-describe-headless-wiring` | `~/teatree-deploy/.claude/worktrees/agent-a207c168c7b24cc15` | `3570-short-describe-headless-wiring` | 12 | `module-health` exit 1 |
| `eval-scenario-regressions` | `~/teatree-deploy/.claude/worktrees/agent-ac4c2578d7717b58c` | `fix-eval-scenario-regressions` | 8 | `ruff-check` — `PT018` at `tests/test_eval_ci_require_executed_gate.py:428` |
| `post-3757-config-surface` | `~/workspace/t3-workspaces/t3-teatree/post-3757-config-cleanup` | `fix/post-3757-config-cleanup` | 3 | `ruff-check` exit 1 |

## Two caveats on fidelity

1. **The capture is of the CURRENT on-disk state, which is not byte-identical to the original audit
   snapshot.** Auto-fixing hooks (`ruff-format`, `ruff --fix`, `codespell`) ran and rewrote some files
   before refusing the commit. Nothing was lost, but formatting and a few spellings differ from what
   the audit first saw.
2. **The archive is the authoritative copy.** Verify it before trusting a recovery:
   `tar tzf salvage-patches/stranded-worktree-patches.tar.gz` lists all ten patches.
3. **`3865-lane-status-integrity` deliberately excludes `.scratch-probe/`** (build noise including
   `__pycache__/*.pyc`). Those files remain on disk in the source worktree; they are simply not in
   the patch.

The source worktrees were left intact — none was deleted, reset, or cleaned.
