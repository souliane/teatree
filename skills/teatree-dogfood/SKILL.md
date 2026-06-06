---
name: teatree-dogfood
description: Dogfooding checklist for teatree CLI, loop, and statusline changes — verify fresh behavior by running the command yourself, exercising the full task lifecycle, and watching the rendered statusline before declaring a change done. Also lists the known worktree/uv/git-stash pitfalls that trip up local validation.
eval_exempt: manual dogfooding checklist run by the human operator; no autonomous agent trajectory to grade
requires:
  - teatree
metadata:
  version: 0.0.1
  subagent_safe: true
triggers:
  priority: 85
  keywords:
    - '\b(dogfood|dogfooding)\b'
    - '\b(cli change|loop change|statusline change|scanner change|manage\.py change)\b'
  exclude: '\b(dogfood the loop|bug hunt|self[- ]qa)\b'
search_hints:
  - dogfood
  - dogfooding
  - cli change
  - loop change
  - statusline change
  - scanner change
  - validation checklist
---

# TeaTree — Dogfooding Checklist (CLI / Loop / Statusline Changes)

Apply this checklist whenever you modify CLI commands, loop scanners, dispatch logic, or statusline rendering. Unit tests alone are insufficient — teatree's failure modes are cwd-, process-, install-, and overlay-sensitive, and the statusline only hits the user's eyes once it has been rendered to disk.

## Run-it-yourself checklist

1. **Run the CLI from a worktree** — `cd $T3_WORKSPACE_DIR/<branch>/teatree && t3 teatree <command>`. Worktree directory names don't match overlay names, so cwd-based discovery exercises the entry-point fallback (see § Pitfalls).
2. **Tick the loop and read the file** — for any change touching `loop/`, `scanners/`, `dispatch/`, or `statusline/`:

   ```bash
   t3 loop tick --statusline-file /tmp/sl.txt --json | jq .
   cat /tmp/sl.txt          # what the user sees
   od -c /tmp/sl.txt | head # what's actually in the bytes (ANSI / OSC 8)
   ```

   The JSON is the structured contract; the file is the rendered contract. Both must match the change you intended.
3. **Exercise both color paths** — `NO_COLOR=1 t3 loop tick --statusline-file /tmp/sl-nc.txt && grep -c $'\033' /tmp/sl-nc.txt` must return `0`. Without `NO_COLOR`, the file must contain `\033[2;37m` (anchors), `\033[1;31m` (action_needed), or `\033[1;36m` (in_flight) when the matching zone is non-empty.
4. **Exercise both overlay paths** when you have more than one overlay registered:
   - `t3 loop tick` (no flag) → multi-overlay; rendered lines prefixed with `[<overlay>]`.
   - `t3 loop tick --overlay <name>` → single overlay; lines unprefixed.
5. **Exercise the full task lifecycle** when the change touches task execution. Create a task, run a tick, verify the worker picks it up. Don't declare "auto-start works" without observing a task transition from PENDING → CLAIMED → DONE in the DB.
6. **Verify the OSC 8 hyperlinks render** in your real terminal (iTerm2, Kitty, WezTerm, Ghostty), not just in the byte stream. A click-through that lands on the wrong URL is a render bug; a click-through that opens nothing is a terminal limitation worth noting.
7. **Confirm the Claude Code statusline hook is wired** — `cat ~/.claude/settings.json | jq .statusLine.command` should point at `hooks/scripts/statusline.sh` (either via plugin or hand-wired). The hook is just a `cat` of `${XDG_DATA_HOME:-$HOME/.local/share}/teatree/statusline.txt` — if the bottom bar is blank, the hook is the first thing to check.

## Known Pitfalls

**`uv run` silently reverts uncommitted edits.** It rebuilds editable installs on every invocation. See `workspace/references/troubleshooting.md` § "uv run Silently Reverts Edits". Commit changes before running `uv run pytest`, or re-read the file after the run to verify content survived.

**`git stash` + `git checkout <other-branch>` silently loses edits.** Stash pop can restore a stale file version that appears current (inode mtime doesn't change) but isn't. Symptom: edits look gone, or tests run against an in-memory version while disk has older content. Fix: always use separate worktrees, never `git stash`.

**`discover_active_overlay()` returns the wrong name in worktrees.** The function walks cwd looking for `manage.py` and returns the directory name. In worktrees, this gives names like `ac-teatree-541-follow-up-...` instead of `teatree`. The `_resolve_overlay_for_server()` function in `cli/__init__.py` works around this by preferring entry-point overlays. Always test from a worktree path so this code path actually runs.

**Single-overlay tick caches.** `code_host_from_overlay()` and `messaging_from_overlay()` are `lru_cache`d for the whole process. After swapping overlay credentials in tests or local config, call `reset_backend_caches()` (or restart the process) — otherwise the next tick reuses the stale client and you'll think your edit didn't take.

**OSC 8 escapes hidden by terminals.** Some terminals (Terminal.app, basic xterm) honour the OSC 8 sequence by silently absorbing it without rendering it as a hyperlink — and `cat` always shows the underlying text. If you're checking that hyperlinks were emitted, use `od -c` on the file, not eyeball the rendered output.

**`statusline.txt` is owner-readable only.** The renderer writes via `tempfile.mkstemp` + `Path.replace`, which leaves the file at the mkstemp default of `0o600`. If you copied the file to a shared path for inspection (don't), permissions will surprise you.

**`T3_OVERLAY_NAME` env override**. The anchor line appends the env var when set. If you exported it once for a debugging session, every later tick will keep showing it — `unset T3_OVERLAY_NAME` before a multi-overlay tick or you'll think the multi-overlay path is broken.
