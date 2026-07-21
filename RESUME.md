# RESUME — `fix/setup-installs-git-hooks`

Handover note. Delete this file before merging the PR.

Worktree: `/tmp/wt-git-hooks` (NOT `~/.local/share/teatree-worktrees/wt-git-hooks`).
Branch: `fix/setup-installs-git-hooks`, based on `origin/main` @ `9ce01693`.

## Root cause (verified, not hypothetical)

`deploy/entrypoint.sh` / `teatree-init` DOES install the prek hooks, and correctly — but
into `$HOME/teatree`, the CONTAINER clone. The HOST checkout
`$HOME/teatree-deploy` held only `*.sample` files with `core.hooksPath` unset, so
every push from it ran with the leak gate
(`scripts/hooks/refuse-public-push-with-leak.sh`), the banned-terms gate and
`dev/push-gate.sh` absent. Every worktree under `~/.local/share/teatree-worktrees/`
shares that git dir (`git rev-parse --git-common-dir` → `$HOME/teatree-deploy/.git`),
so all of them were ungated too.

The bug is therefore NOT "setup forgets to install hooks" but "setup installs into one
clone while work happens in another". A doctor check that inspects only the installed
clone would have reported healthy all day while host pushes went ungated.

## Host checkout status — GATED

`$HOME/teatree-deploy/.git/hooks` now holds executable `pre-commit`, `pre-push`
and `commit-msg`. They were installed through the sanctioned mechanism
(`teatree.core.prek_hook.install` → `prek install -f`) and are confirmed firing: the
amend commit on this branch ran the full pre-commit gate and passed.

`prek` was also installed onto the host PATH (`uv tool install prek` → `~/.local/bin/prek`).
This was REQUIRED, not incidental: prek-generated hooks are PATH-resolved by design
(#1462 `harden_hooks`), so before this the freshly-installed hooks failed every commit
with `exec: prek: not found`. A host without `prek` on PATH has hooks that exist but
cannot run.

## What is implemented

- `src/teatree/core/gates/git_checkouts.py` — `discover_checkouts()` enumerates the
  checkouts nothing else covers: the installed clone, every checkout under the
  auto-isolated worktrees root (including ad-hoc `git worktree add` ones), and the
  owning clone behind each (`owning_clone`, via `rev-parse --git-common-dir`). Django-free
  so setup/doctor can call it pre-`ensure_django`.
- `src/teatree/core/gates/git_hooks_preflight.py` — `probe_git_hooks` (per checkout) and
  `probe_checkouts` (one verdict per git hooks dir, so a worktree family collapses onto
  its clone; a checkout with no `.pre-commit-config.yaml` is skipped). A `*.sample` file
  and a non-executable hook both count as missing. A `core.hooksPath` resolving anywhere
  other than the default hooks dir is reported, never judged and never installed over.
- `src/teatree/cli/setup/git_hooks_installer.py` — `GitHooksInstaller`, wired into
  `t3 setup` (`command.py`, after `ApmInstaller`). Installs into every discovered
  unprotected checkout, delegating to `prek_hook.install` (idempotent by overwrite).
- `src/teatree/cli/doctor/checks_bootstrap.py` — `_check_git_hooks_installed`, a hard
  FAIL per unprotected checkout naming the path, the missing hooks, the gates each
  carries, and `t3 setup`. Added to `run_bootstrap_checks`' verdict.
- `BLUEPRINT.md` — "Git-hook install completeness" paragraph in §10.

## Anti-vacuity — BOTH obtained

1. Disabling the install step (`prek_hook.install` replaced by a success stub) turned
   the fresh-checkout tests RED: `test_fresh_checkout_ends_with_both_hooks_installed`,
   `test_an_unprotected_second_clone_is_installed_into_as_well`, `test_rerun_is_a_no_op`,
   `test_run_installs_into_every_discovered_checkout` — 4 failed, 26 passed. Restored.
2. Narrowing the doctor check to the installed clone only (`discover_checkouts()[:1]`)
   turned `test_a_protected_clone_does_not_mask_an_unprotected_one` RED. Restored.

## Test results

- New/changed tests: `tests/teatree_core/gates/test_git_hooks_preflight.py`,
  `tests/teatree_core/gates/test_git_checkouts.py`,
  `tests/teatree_cli/setup/test_git_hooks_installer.py`,
  `tests/teatree_cli/doctor/test_bootstrap_checks.py`.
  All green together with the core-architecture ratchets: **628 passed**.
- `uv run ruff check` / `ruff format --check` / `uv run tach check` / `uv run prek run
  ty-check --all-files` — all pass.
- `dev/ci-parity-fast.sh` initially FAILED on two real architecture ratchets
  (`test_no_flat_core_regrowth`, `test_intra_core_deferred_import_ratchet`). Both were
  fixed properly, not by bumping pegs — see dead ends below. Not re-run since.
- Full `uv run pytest --no-cov`: **29642 passed, 52 skipped, 1 failed**. The failure is
  `tests/teatree_core/management_commands/test_outer.py::TestOuterTickWhenEnabled::
  test_tick_ok_line_names_the_experiment` — a `sqlite3` unraisable-exception warning under
  xdist contention, unrelated to this change and **green when re-run in isolation**.

## PR — OPEN, non-draft, but the BODY IS STALE

PR #3537 <https://github.com/souliane/teatree/pull/3537> is open and non-draft. The branch
is pushed (the pre-push leak gate passed on the second attempt, after scrubbing absolute
home paths out of this file — see "Push" below).

**Its body is the auto-generated commit message from BEFORE the rework, so two statements
in it are now WRONG:** it cites `teatree.core.git_checkouts` (the module now lives at
`teatree.core.gates.git_checkouts`) and claims discovery reads "each tracked worktree's
on-disk path" (the ORM source was dropped — see dead ends). It also lacks the
`## Architecture pre-check`, anti-vacuity and test-evidence sections.

The correct body is committed at `pr-body.txt` in this worktree, already validated
(`t3 tool validate-mr` exits 0 on it). **Posting it is blocked by a teatree bug, NOT by
anything wrong with the body:**

- `gh pr edit --body-file pr-body.txt` fails on this box with a gh/GitHub API error
  (`GraphQL: Projects (classic) is being deprecated … repository.pullRequest.projectCards`)
  and never applies the change.
- `gh api -X PATCH repos/souliane/teatree/pulls/3537 -F body=@pr-body.txt` is denied by the
  MR-metadata gate with "the overlay validator is not resolvable or crashed". Root cause
  measured: `hook_router._run_mr_validator` gives the validator a **10s timeout**, and
  `t3 tool validate-mr` takes **13.2s** here (cold `t3` + Django bootstrap). The
  `TimeoutExpired` becomes `None`, which the gate reads as broken-env and fails closed.

This is a real bug worth its own fix (raise the timeout, or warm the validator). It was NOT
bypassed — `T3_MR_VALIDATE_ALLOW_BROKEN_ENV=1` is a deliberate safety bypass a sub-agent
must not self-authorize.

## What the next person must do

1. Post the correct PR body. Either fix the validator timeout first, or have the owner
   authorize the documented bypass:
   `cd <this worktree> && gh api -X PATCH repos/souliane/teatree/pulls/3537 -F body=@pr-body.txt`.
2. Delete `RESUME.md` and `pr-body.txt`, commit, push. Neither belongs in the merge.
3. Re-run `bash dev/ci-parity-fast.sh` — it must now pass the two ratchets it caught.
4. Watch CI on #3537.
5. The implementation itself is complete; nothing else is outstanding.

## Dead ends / decisions not worth revisiting

- Do NOT put the discovery module at `src/teatree/core/git_checkouts.py`. That is a new
  flat core leaf and `tests/quality/test_no_flat_core_regrowth.py` pins the count at 79.
  It belongs in the `core/gates/` subpackage, where it now lives.
- Do NOT source checkouts from the `Worktree` ORM rows. It requires a function-scoped
  `from teatree.core.models...` import (a top-level one breaks
  `test_module_import_does_not_eager_load_orm_models` for the setup command path), and
  that trips `tests/quality/test_intra_core_deferred_import_ratchet.py`. Bumping the peg
  would be tech debt. The isolated-worktrees-root scan covers the population that
  actually exposed the bug; teatree-provisioned worktrees already get hooks from
  `worktree provision`.
- `Worktree.repo_path` is a slug (`souliane/teatree`), not a filesystem path — the
  on-disk path is `extra["worktree_path"]`. Do not read `repo_path` as a path.
- Tests MUST pin the checkout set explicitly (`GitHooksInstaller(repo, checkouts=[...])`)
  or patch `discover_checkouts`. An early version did not, and a test run installed hooks
  into the developer's real host clone as a side effect. That is how the host clone got
  its hooks, before they were re-verified deliberately.
