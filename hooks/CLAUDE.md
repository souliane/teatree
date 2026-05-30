# hooks — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `hooks/`.

- **One router, many events.** `scripts/hook_router.py` dispatches on `--event`. Registered events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `TaskCreated`, `Stop`, `SubagentStop`, `PreCompact`, `SessionEnd`, `InstructionsLoaded`. Wiring lives in `hooks.json`. `SubagentStop` carries `session_id` / `cwd` / `transcript_path` and fires once per sub-agent termination — `handle_subagent_stop_no_commit` (#1205) records a `terminated_without_commit` signal when the sub-agent's worktree (the harness `cwd`) is a work branch with 0 commits, so the orchestrator does not assume lost work landed.
- **Adding a gate** = a new handler branch in `hook_router.py` for an existing event (usually `PreToolUse` or `Stop`). To **deny** a `PreToolUse` tool call, call the shared helper and return its `True`:

  ```python
  return emit_pretooluse_deny(reason)
  ```

  The helper writes the modern nested `hookSpecificOutput` schema that Claude Code 2.1.146+ actually reads, alongside the legacy flat keys for back-compat. `main()` translates the helper's `True` return into `sys.exit(2)` — exit 0 deny payloads are silently ignored by the harness (changelog: "Fixed `PreToolUse` hooks that emit JSON to stdout and exit with code 2 not correctly blocking the tool call"). See #1447 for the diagnosis.

- **`TaskCreated` is the only non-`PreToolUse` deny path, with a DIFFERENT schema (#1488).** The harness Workflow/Task fan-out vehicle (what `ultracode` and any teammate spawn use) **bypasses `PreToolUse`/`PostToolUse` hooks** — so a gate that must govern the fan-out rides the `TaskCreated` event instead (no matcher, fires on every task creation; stdin schema `session_id`, `task_id`, `task_subject`, `task_description`, optional `teammate_name`/`team_name`). It does NOT use `emit_pretooluse_deny`. To block task creation, emit the teammate-stop envelope and return `True`:

  ```python
  return emit_task_create_deny(reason)  # writes {"continue": false, "stopReason": reason}
  ```

  `continue: false` is what the harness honours to block (it sets `preventContinuation`); `main()` translates the `True` return into `sys.exit(2)` the same as a `PreToolUse` deny. `handle_enforce_skill_loading_on_task_create` is the only `TaskCreated` gate today — it forces the matching teatree skill onto a fanned-out task so the `PreToolUse`-only `handle_enforce_skill_loading` gate is no longer bypassable. The block is fail-open: kill-switch `[teatree] skill_loading_gate_enabled = false`, per-call `[skip-skill-gate: <reason>]` token, and `t3 <overlay> gate skill-loading disable` self-rescue.

- **Skill-loading PreToolUse gate narrows to task-intent + has a per-call escape (#1567).** `handle_enforce_skill_loading` (matcher `Bash|Edit|Write`) hard-blocks until every *resolvable* unloaded skill in `<session>.pending` loads. The demand set must derive from genuine task-intent text only: `_build_skill_loader_input` runs the prompt through `_strip_ambient_context` first, removing the harness-injected `<system-reminder>` / `<command-*>` wrappers (the CLAUDE.md body, the MEMORY.md index, the skills listing). Without that, an ambient MEMORY.md index line containing a topic keyword (e.g. `blog`) keyword-matched the supplementary `~/.teatree-skills.yml` map and hard-blocked an unrelated autonomous loop. The gate also honours a per-call `[skill-load-ok: <reason>]` token in the current tool call's command/args (`command` for Bash, `new_string`/`content`/`file_path` for Edit/Write, first 512 chars; empty reason rejects) — modelled on `[skip-skill-gate:]` and `[fg-ok:]` — so a false trigger can never wedge the loop, while a genuine intent match still blocks every call lacking the token.
- **Hooks must be crash-proof and fast.** `hooks.json` sets tight timeouts (3–5s); a hook that raises or hangs blocks the user. Fail open and stay silent on the happy path.
- **Statusline state** is regenerated, not authored: `/tmp/claude-statusline/<session>.<suffix>` (override dir via `TEATREE_CLAUDE_STATUSLINE_STATE_DIR`). Never read it as a source of truth.
