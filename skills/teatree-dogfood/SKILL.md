---
name: teatree-dogfood
description: Dogfooding checklist for teatree CLI, loop, and statusline changes — verify fresh behavior by running the command yourself, exercising the full task lifecycle, and watching the rendered statusline before declaring a change done. Also lists the known worktree/uv/git-stash pitfalls that trip up local validation.
metadata:
  version: 0.0.1
  subagent_safe: true
triggers:
  priority: 85
  keywords:
    - '\b(dogfood|dogfooding)\b'
    - '\b(cli change|dashboard change|server startup|manage\.py change)\b'
  exclude: '\b(dogfood the dashboard|bug hunt|self[- ]qa)\b'
search_hints:
  - dogfood
  - dogfooding
  - cli change
  - dashboard change
  - server startup
  - validation checklist
---

# TeaTree — Dogfooding Checklist (CLI/Server Changes)

Apply this checklist whenever you modify CLI commands, dashboard views, or server startup. Unit tests alone are insufficient — teatree's failure modes are cwd-, process-, and install-sensitive.

1. **Run the command yourself** — `t3 <command>` from a worktree (not the main clone) to catch cwd-dependent bugs.
2. **Verify HTTP 200** — for dashboard/server changes: `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:<port>/` must return 200.
3. **Run E2E tests** — dashboard changes require Playwright E2E tests in `e2e/test_dashboard.py`. **Pre-flight:** kill any zombie servers first (`pkill -9 -f "uvicorn teatree.asgi"; pkill -9 -f "chrome-headless"; pkill -9 -f "playwright/driver"`). Then: `DJANGO_SETTINGS_MODULE=e2e.settings uv run pytest e2e/ --ds e2e.settings --no-cov -v`. Each timed-out run leaves zombie processes — kill before retrying. For full-suite validation prefer CI (clean environment, ~seconds/test vs. 7+ min locally on a loaded machine).
4. **Test the full flow** — if the change involves task execution, create a task and verify the worker picks it up. Don't declare "auto-start works" without observing a task transition from PENDING to CLAIMED.
5. **Check overlay resolution from worktrees** — `discover_active_overlay()` uses cwd-based discovery. Worktree directory names don't match overlay names. Always test from a worktree path, not the main clone.

## Known Pitfalls

**`discover_active_overlay()` returns the wrong name in worktrees.** The function walks cwd looking for `manage.py` and returns the directory name. In worktrees, this gives names like `move-dashboard-to-general-cli` instead of `t3-teatree`. The `_resolve_overlay_for_server()` function in `cli/__init__.py` works around this by preferring entry-point overlays.

**`uv run` silently reverts uncommitted edits.** It rebuilds editable installs on every invocation. See `workspace/references/troubleshooting.md` § "uv run Silently Reverts Edits". Commit changes before running `uv run pytest`, or re-read the file after the run to verify content survived.

**`git stash` + `git checkout <other-branch>` silently loses edits.** Stash pop can restore a stale file version that appears current (inode mtime doesn't change) but isn't. Symptom: edits look gone, or tests run against an in-memory version while disk has older content. Fix: always use separate worktrees, never `git stash`.
