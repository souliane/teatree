# hooks — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This file adds only what is specific to `hooks/`.

- **One router, many events.** `scripts/hook_router.py` dispatches on `--event`. Registered events: `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse`, `Stop`, `PreCompact`, `SessionEnd`, `InstructionsLoaded`. Wiring lives in `hooks.json`.
- **Adding a gate** = a new handler branch in `hook_router.py` for an existing event (usually `PreToolUse` or `Stop`). To **deny** a `PreToolUse` tool call, call the shared helper and return its `True`:

  ```python
  return emit_pretooluse_deny(reason)
  ```

  The helper writes the modern nested `hookSpecificOutput` schema that Claude Code 2.1.146+ actually reads, alongside the legacy flat keys for back-compat. `main()` translates the helper's `True` return into `sys.exit(2)` — exit 0 deny payloads are silently ignored by the harness (changelog: "Fixed `PreToolUse` hooks that emit JSON to stdout and exit with code 2 not correctly blocking the tool call"). See #1447 for the diagnosis.

- **Hooks must be crash-proof and fast.** `hooks.json` sets tight timeouts (3–5s); a hook that raises or hangs blocks the user. Fail open and stay silent on the happy path.
- **Statusline state** is regenerated, not authored: `/tmp/claude-statusline/<session>.<suffix>` (override dir via `TEATREE_CLAUDE_STATUSLINE_STATE_DIR`). Never read it as a source of truth.
