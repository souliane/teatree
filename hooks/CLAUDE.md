# hooks — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `hooks/`.

- **One router, many events.** `scripts/hook_router.py` dispatches on `--event`. Registered events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `PreCompact`, `SessionEnd`, `InstructionsLoaded`. Wiring lives in `hooks.json`.
- **Adding a gate** = a new handler branch in `hook_router.py` for an existing event (usually `PreToolUse` or `Stop`). To **deny** a tool call, write the harness directive to stdout and exit 0:

  ```python
  json.dump({"permissionDecision": "deny", "permissionDecisionReason": reason}, sys.stdout)
  ```

- **Hooks must be crash-proof and fast.** `hooks.json` sets tight timeouts (3–5s); a hook that raises or hangs blocks the user. Fail open and stay silent on the happy path.
- **Statusline state** is regenerated, not authored: `/tmp/claude-statusline/<session>.<suffix>` (override dir via `TEATREE_CLAUDE_STATUSLINE_STATE_DIR`). Never read it as a source of truth.
