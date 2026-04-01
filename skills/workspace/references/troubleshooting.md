# Troubleshooting

> Load when diagnosing worktree setup failures, DB errors, port conflicts, or DSLR issues.

---

## "Database Already Exists"

- **Cause:** Previous `t3 lifecycle setup` created the DB but was interrupted before completing.
- **Fix:** Run `t3 db refresh` to drop and reimport cleanly.

## Port Already in Use

- **Cause:** Stale process from a previous session holds the port.
- **Fix:** `lsof -i :<port>` to identify the process, then kill it. Or run `t3 lifecycle setup` again — it allocates free ports automatically.
- **Docker variants:** If using Docker-based services, port conflicts can also come from stale containers or compose project name collisions. Check `docker ps -a` for conflicting containers. Project overlays may document additional Docker-specific failure modes in their own troubleshooting references.

## Setup Reports "provisioned" But DB Is Missing or Empty

- **Symptom:** `t3 lifecycle setup` completes with state "provisioned" and `db_name` in facts, but `psql` shows the database does not exist. Or DB exists but seed tables are empty (row count is 0).
- **Cause:** Two known scenarios:
  1. `.env.worktree` was stale from a previous worktree (different ticket/variant). The setup used the old `DATABASE_URL` for migrations — connecting to an existing DB instead of creating a new one. Fixed in `lifecycle.py` (adds `_force_load_env_worktree` after env generation).
  2. The active project overlay was not configured in the overlay package, so project-layer DB hooks never ran.
- **Verification after setup:** Always check: `psql -h localhost -p <port> -U <db_user> -d <db_name> -c "SELECT count(*) FROM <seed_table>"` — must be > 0.
- **Fix:** Delete `.env.worktree` (both ticket-dir and repo-level) + `.state.json`, drop the DB if it exists, and re-run `t3 lifecycle setup`.

## "Worktree Is Already Checked Out"

- **Cause:** A worktree for this branch already exists elsewhere.
- **Fix:** Run `git worktree list` to find it. Remove with `git worktree remove <path>` if no longer needed.

## DSLR Restore Fails Silently

- **Cause:** `dslr` not installed or the snapshot is from an incompatible Postgres version.
- **Fix:** Run `uv tool install dslr` to install. If version mismatch, delete the snapshot (`dslr delete <name>`) and let `t3 db refresh` reimport from dump.

## Remote `pg_dump` Times Out or Produces Truncated Dump

- **Cause:** Slow internet or unstable VPN. Large tenant dumps (100MB+) need sustained bandwidth. Consecutive timeouts on the same day indicate a bandwidth problem, not a transient VPN glitch. A truncated dump can also look valid (`pg_restore -l` may succeed on the TOC header) but fail during actual restore.
- **Symptoms:** 0-byte dump file, or non-zero file that fails `pg_restore` with "could not read from input file: end of file". Downstream: migrations succeed but every API call returns 400/500 (`KeyError` on enum lookups) — the DB has schema but no data.
- **Fix:** Do not retry automatically. Ask the user whether to retry now or defer. Delete the corrupt/truncated dump from `.data/` before retrying.
- **Prevention rules:**
  1. NEVER assume a dump file is valid without checking its size — 0-byte is always corrupt, fail loudly.
  2. NEVER try to manually seed a migration-only DB. If the dump is bad, fix the dump — do not reverse-engineer seed inserts across dozens of interdependent tables.
  3. Treat "migrations succeeded but app errors on every request" as a dump/seed-data problem until proven otherwise.
  4. Always verify network/VPN connectivity before remote DB operations.
  5. Monitor `pg_dump` progress — if file hasn't grown in several minutes, connection is stalled.
- **Diagnostic checklist:** check dump file size (`ls -lh .data/*.dump`), check VPN, check `pg_restore -l <dump>` stderr, spot-check a known seed table (`SELECT COUNT(*) FROM <table>`).
  6. **Compare sizes across dates** for the same variant — a dump that is drastically smaller than a known-good one (e.g. 90MB vs 704MB) is almost certainly broken. Flag it immediately.

## Statusline Blank or Missing Repo/Worktree Data

- **Symptom:** Statusline shows `model=... | cwd=...` but no repo branches, no worktrees, no dirty markers. Or shows `0>` immediately after a truncated line.
- **Cause:** `#!/usr/bin/env bash` resolved to `/bin/bash` 3.x (macOS system bash). `declare -A` silently degrades — creates indexed arrays instead of associative arrays, so all repo lookups return empty.
- **Fix:** Install Bash 4+ (`brew install bash` on macOS). The statusline script includes a version guard that auto re-execs with a modern bash from well-known locations, but if none is found it exits with an error.
- **Prevention:** When modifying shell scripts, always test with the system bash (`/bin/bash --version`). If a script uses `declare -A`, `${!array[@]}` on associative arrays, or `declare -n`, it needs either Bash 4+ on PATH or a version guard with re-exec fallback. Also audit for macOS-only commands (`md5 -q`, `stat -f`, `open`, `pbcopy`) — use platform detection or provide Linux alternatives (`md5sum`, `stat -c`, `xdg-open`, `xclip`).

## TeaTree CLI Uses the Wrong Python Environment

- **Symptom:** `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` on `str | None` type hints, or other 3.10+ syntax errors.
- **Cause:** The shell wrapper or direct command resolved a Python outside the TeaTree `uv` environment.
- **Fix:** Run `uv run t3 ...` from the TeaTree repo, or source the legacy bootstrap wrapper which now delegates to `uv run t3`.
- **Prevention:** Never patch syntax to accommodate older Python (e.g. `from __future__ import annotations`). Fix the Python resolution instead.

## Issue Tracker CLI Quirks

See your [issue tracker platform reference](../../t3:platforms/references/) § "Known CLI Quirks" for platform-specific CLI issues. Common gotcha: some CLIs cannot serialize nested JSON — use `curl` instead for complex payloads.

## Pre-Commit Hook Failure + Stash Cycle Destroys Uncommitted Work

- **Symptom:** After a pre-commit hook fails, the agent runs `git stash` / `git checkout -- .` / `git clean -fd` to "fix" the working tree state. All uncommitted changes from the session are lost.
- **Cause:** `prek` (pre-commit) stashes uncommitted changes before running hooks, then unstashes after. Running `git stash` on top of prek's internal stash creates a nested stash. Then `git checkout -- .` wipes the working tree, and `git stash pop` creates merge conflicts because the stash was made from a different state. The result: hours of work destroyed.
- **Fix:** When a pre-commit hook fails, the ONLY safe actions are:
  1. Fix the specific issue the hook reported (lint error, test failure, etc.)
  2. Re-stage the fixed files
  3. Commit again
  4. If the user says to skip hooks: `git commit --no-verify` immediately
- **Prevention:**
  1. NEVER run `git stash`, `git checkout -- .`, `git clean -fd`, or `git reset --hard` when there are uncommitted changes you need to keep
  2. When the user says `--no-verify`, do it immediately — do not keep retrying with hooks
  3. `git diff` and `git status` are always safe; `git checkout` and `git stash` are not
  4. If the working tree is in a confusing state, create a backup branch FIRST: `git branch backup-$(date +%s)`

## Pre-Commit Hooks Stage Unrelated Files

- **Symptom:** After running `prek run --all-files` (or `pre-commit run --all-files`), a subsequent `git commit` includes unexpected file changes (deletions, formatting fixes) that weren't explicitly staged.
- **Cause:** Hooks like `end-of-file-fixer`, `trailing-whitespace`, and `ruff-format` modify files and stage them as part of their fix. If there are pending deletions or unstaged changes, the hook run can stage those too.
- **Fix:** After running pre-commit hooks, always check `git diff --cached --stat` before committing to verify only intended files are staged. Unstage anything unrelated with `git restore --staged <file>`.
- **Prevention:** Commit or stash all unrelated changes before running pre-commit on the full repo. When using pre-commit as a verification step (not a commit step), review the staging area before any commit.

## Integration Tests Corrupt `.git/config` When Run Under Pre-Commit Hooks

- **Symptom:** `git status` fails with `fatal: Invalid path '/private/.../pytest-of-.../pytest-NNN'`, or `user.name`/`user.email` are silently overwritten to test values like "Test User".
- **Cause:** Tests that spawn `git` subprocesses (e.g., `subprocess.run(["git", "init", ...])`) inherit `GIT_*` environment variables from the parent process. When pre-commit hooks run pytest, prek sets `GIT_INDEX_FILE`, `GIT_DIR`, etc. The test's git commands then operate on the **real repo's** config/index instead of the temp repo's — writing `core.worktree`, `user.name`, and other settings to the wrong `.git/config`.
- **Fix:** Strip ALL `GIT_*` env vars from subprocess calls in tests:

  ```python
  import os
  _GIT_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}

  def _git(repo, *args):
      return subprocess.run(["git", "-C", str(repo), *args], env=_GIT_ENV, ...)
  ```

- **Recovery:** If already corrupted, edit `.git/config` directly (git commands may fail). Remove the stale `core.worktree` line and any overwritten `[user]` section.
- **Prevention:** Every test helper that calls git subprocesses must use a sanitized env. `GIT_CONFIG_GLOBAL=/dev/null` alone is insufficient — `GIT_INDEX_FILE` and `GIT_DIR` also leak.

## Pre-Commit Fails with `ImportError` When Committing to a Different Repo

- **Symptom:** `git commit` in repo B fails during the pytest pre-commit hook with `ImportError: No module named '<overlay>'` — but the module belongs to repo A, not repo B.
- **Cause:** `DJANGO_SETTINGS_MODULE` (and sometimes `VIRTUAL_ENV`) from repo A's direnv leaks into repo B's pre-commit run when the shell was previously in repo A's directory. Also occurs when repo A is installed as editable into repo B's venv (via `.pth` files), causing `pytest-django` to load the wrong project's settings.
- **Fix:** Unset the leaking env vars before committing: `unset DJANGO_SETTINGS_MODULE && git commit ...`. If the issue is `.pth`-based cross-contamination, use `SKIP=pytest git commit ...` and verify tests pass separately with explicit `PYTHONPATH`.
- **Prevention:** When committing to a repo other than the current working directory (e.g., during skill reviews or retros), sanitize Django-related env vars first. The agent should detect when the target repo differs from the cwd and preemptively unset `DJANGO_SETTINGS_MODULE` and reset `VIRTUAL_ENV`. When a shared venv has editable installs from multiple Django projects, the pytest pre-commit hook may always fail — use `SKIP=pytest` and run tests manually.

## `No module named uvicorn` When Running `t3 <overlay> dashboard`

- **Symptom:** `t3 acme dashboard` fails with `No module named uvicorn`.
- **Cause:** `_uvicorn()` used `sys.executable` — the teatree venv's Python. But `uvicorn` is a dependency of the overlay project (e.g., t3-acme), not of teatree itself. The teatree venv has no `uvicorn` installed.
- **Fix:** `_uvicorn()` now uses `uv --directory <project_path> run uvicorn` to run in the project's environment, falling back to `sys.executable` otherwise.
- **Prevention:** When spawning overlay project commands from the `t3` CLI, always consider whether the command needs project-specific dependencies. If so, use `uv --directory <project_path>` to run in the correct environment.

## `Could not import module "asgi"` When Running `t3 <overlay> dashboard`

- **Symptom:** `t3 acme dashboard` runs migrations successfully but then fails with `Error loading ASGI app. Could not import module "asgi"`.
- **Cause:** `_uvicorn()` read `DJANGO_SETTINGS_MODULE` from `os.environ` to construct the ASGI module path (e.g., `acme.asgi:application`). But the overlay registration never set it in the environment — only the overlay entry object had the `settings_module`. With an empty env var, the ASGI path resolved to bare `"asgi:application"`.
- **Fix:** Thread `settings_module` from the overlay entry through `_build_overlay_app` → `dashboard` → `_uvicorn` as a parameter, falling back to `os.environ` only if not provided.
- **Prevention:** When adding CLI commands that need overlay metadata (settings module, project path), pass the metadata explicitly from the overlay entry rather than relying on environment variables that may not be set.

## `uv run` Silently Reverts Edits in Editable Installs

- **Symptom:** After editing a source file in an editable install, running `uv run <anything>` rebuilds the package and overwrites your changes.
- **Cause:** `uv run` triggers an editable install rebuild, which replaces source files with the built version from the package metadata.
- **Fix:** Re-apply the edits after the rebuild.
- **Prevention:** Never use `uv run` to verify edits in an editable install. Use `python -c "..."` directly, or verify file content with `grep`/`read`. Commit changes before running `uv run` if possible.

## Test Timeout in `sync_followup` or Other Overlay-Config-Dependent Tests

- **Symptom:** `test_creates_tickets_from_mrs` (or similar) hangs for 10s then fails with `pytest-timeout`. Stack trace shows `read_pass` → `subprocess.run(["pass", ...])` blocking.
- **Cause:** `OverlayConfig._register_secret()` used `setattr(type(self), method_name, _reader)` — setting the `get_*_token()` method on the **class**, not the instance. When any earlier test loaded a real overlay (e.g., `t3-teatree` with `GITHUB_TOKEN_PASS_KEY`), the dynamic method leaked to ALL `OverlayConfig` subclasses for the rest of the test session. The autouse `read_pass` mock couldn't intercept it because: (a) the closure captured the `read_pass` function reference at import time, bypassing `patch.object`, and (b) the method lived on the class, not re-created per test.
- **Fix (applied):** Two changes in `overlay.py`:
  1. `setattr(self, ...)` instead of `setattr(type(self), ...)` — instance-level binding prevents cross-test pollution.
  2. `from teatree.utils.secrets import read_pass` moved inside the closure body (late binding) — so `patch.object(_secrets_mod, "read_pass", ...)` works.
- **Prevention:** Never use `setattr(type(self), ...)` for per-instance dynamic methods — it mutates the class and leaks across all instances. Use `setattr(self, ...)` for instance-scoped behavior.

## direnv Not Loading `.envrc`

- **Cause:** direnv not hooked into the shell or `.envrc` not allowed.
- **Fix:** Run `direnv allow` in the worktree directory. Verify `eval "$(direnv hook zsh)"` is in `.zshrc`.
