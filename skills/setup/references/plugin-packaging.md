# Plugin Packaging Checklist

Verified failures from issue #3 shipping. Every item below caused a real breakage.

## Claude Code Plugin Format

- `enabledPlugins` in `settings.json` is a **record** `{"path": true}`, not an array
- Plugins are NOT discovered from `~/.claude/plugins/` symlinks alone — must register via `claude plugin marketplace add <path>` then `claude plugin install name@marketplace`
- `marketplace.json` requires top-level `name` and `owner` fields; `description` at root is invalid
- `source: "."` is invalid; use `source: "./"`
- Plugin skills appear as `/skillname` with `(pluginname)` tag, not `/pluginname:skillname`
- Plugin agents are NOT slash commands — they're `subagent_type` values for the Agent tool
- `claude plugin validate <path>` checks the manifest — always run before shipping
- Marketplace install creates a **copy** in `~/.claude/plugins/cache/` — edits to the source repo require `claude plugin update` or `--plugin-dir` for live reloading

## Post-Rename Verification

When renaming directories (e.g., `skills/t3:code/` → `skills/code/`):

- Check for **symlinks outside the repo** (e.g., `~/.claude/skills/t3:*`) — these break silently
- Use `command rm` (not bare `rm`) on macOS/zsh — aliases may prevent deletion
- Grep ALL consumers: settings.json hook paths, skill symlinks, tests, scripts

## External State Checklist

Changes that affect external state (outside the git repo) require manual verification:

- `~/.claude/settings.json` hook paths
- `~/.claude/skills/` symlinks
- `~/.claude/plugins/` registrations
- Dashboard URL namespace (`ROOT_URLCONF` must use `include()` for `app_name` to work)
- Runtime dependencies (`uvicorn` must be in `pyproject.toml` if used by dashboard)

## Development vs Production Install

| Mode | Command | Behavior |
|------|---------|----------|
| Dev (live edits) | `claude --plugin-dir <repo>` | Loads from source, no copy |
| Dev via `t3 agent` | Set `T3_CONTRIBUTE=true` | Adds `--plugin-dir` automatically |
| Production | `t3 setup` | Runs APM install, syncs skills, registers marketplace + installs copy |
