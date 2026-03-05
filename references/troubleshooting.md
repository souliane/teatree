# Troubleshooting

> Load when diagnosing worktree setup failures, DB errors, port conflicts, or DSLR issues.

---

## "Database Already Exists"

- **Cause:** Previous `t3_setup` created the DB but was interrupted before completing.
- **Fix:** Run `t3_db_refresh` to drop and reimport cleanly.

## Port Already in Use

- **Cause:** Stale process from a previous session holds the port.
- **Fix:** `lsof -i :<port>` to identify the process, then kill it. Or run `t3_setup` again — it allocates free ports automatically.
- **Docker variants:** If using Docker-based services, port conflicts can also come from stale containers or compose project name collisions. Check `docker ps -a` for conflicting containers. Project overlays may document additional Docker-specific failure modes in their own troubleshooting references.

## "Worktree Is Already Checked Out"

- **Cause:** A worktree for this branch already exists elsewhere.
- **Fix:** Run `git worktree list` to find it. Remove with `git worktree remove <path>` if no longer needed.

## DSLR Restore Fails Silently

- **Cause:** `dslr` not installed or the snapshot is from an incompatible Postgres version.
- **Fix:** Run `uv tool install dslr` to install. If version mismatch, delete the snapshot (`dslr delete <name>`) and let `t3_db_refresh` reimport from dump.

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

## Statusline Blank or Missing Repo/Worktree Data

- **Symptom:** Statusline shows `model=... | cwd=...` but no repo branches, no worktrees, no dirty markers. Or shows `0>` immediately after a truncated line.
- **Cause:** `#!/usr/bin/env bash` resolved to `/bin/bash` 3.x (macOS system bash). `declare -A` silently degrades — creates indexed arrays instead of associative arrays, so all repo lookups return empty.
- **Fix:** Install Bash 4+ (`brew install bash` on macOS). The statusline script includes a version guard that auto re-execs with a modern bash from well-known locations, but if none is found it exits with an error.
- **Prevention:** When modifying shell scripts, always test with the system bash (`/bin/bash --version`). If a script uses `declare -A`, `${!array[@]}` on associative arrays, or `declare -n`, it needs either Bash 4+ on PATH or a version guard with re-exec fallback. Also audit for macOS-only commands (`md5 -q`, `stat -f`, `open`, `pbcopy`) — use platform detection or provide Linux alternatives (`md5sum`, `stat -c`, `xdg-open`, `xclip`).

## Skill Scripts Use System Python Instead of pyenv 3.12

- **Symptom:** `TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'` on `str | None` type hints, or other 3.10+ syntax errors.
- **Cause:** `_t3_py` / `_t3_python` resolve `python3` via pyenv shim, which uses `cwd` to find `.python-version`. If the shell function runs from a directory without `.python-version` in its ancestry, pyenv falls back to `system` (3.9).
- **Fix:** `_t3_python` runs in a subshell `cd`'d to `$_T3_SCRIPTS_DIR` so pyenv picks up `$T3_WORKSPACE_DIR/.python-version`. If the issue recurs, verify the file exists: `cat $T3_WORKSPACE_DIR/.python-version` (should say `3.12.6`).
- **Prevention:** Never patch syntax to accommodate older Python (e.g. `from __future__ import annotations`). Fix the Python resolution instead.

## Issue Tracker CLI Quirks

See your [issue tracker platform reference](platforms/) § "Known CLI Quirks" for platform-specific CLI issues. Common gotcha: some CLIs cannot serialize nested JSON — use `curl` instead for complex payloads.

## Pre-Commit Hooks Stage Unrelated Files

- **Symptom:** After running `prek run --all-files` (or `pre-commit run --all-files`), a subsequent `git commit` includes unexpected file changes (deletions, formatting fixes) that weren't explicitly staged.
- **Cause:** Hooks like `end-of-file-fixer`, `trailing-whitespace`, and `ruff-format` modify files and stage them as part of their fix. If there are pending deletions or unstaged changes, the hook run can stage those too.
- **Fix:** After running pre-commit hooks, always check `git diff --cached --stat` before committing to verify only intended files are staged. Unstage anything unrelated with `git restore --staged <file>`.
- **Prevention:** Commit or stash all unrelated changes before running pre-commit on the full repo. When using pre-commit as a verification step (not a commit step), review the staging area before any commit.

## direnv Not Loading `.envrc`

- **Cause:** direnv not hooked into the shell or `.envrc` not allowed.
- **Fix:** Run `direnv allow` in the worktree directory. Verify `eval "$(direnv hook zsh)"` is in `.zshrc`.
