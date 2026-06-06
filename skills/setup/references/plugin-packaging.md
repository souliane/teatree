# Plugin Packaging Checklist

Verified failures from issue #3 shipping. Every item below caused a real breakage.

## Claude Code Plugin Format

- `enabledPlugins` in `settings.json` is a **record** `{"path": true}`, not an array
- Plugin skills appear as `/skillname` with `(pluginname)` tag, not `/pluginname:skillname`
- Plugin agents are NOT slash commands — they're `subagent_type` values for the Agent tool
- `claude plugin validate <path>` checks the manifest — always run before shipping

## Plugin Install: installed_plugins.json Registration

Teatree requires a local clone. `t3 setup` registers the plugin in
`~/.claude/plugins/installed_plugins.json` with `installPath` pointing at the
main clone:

```json
{"plugins": {"t3@souliane": [{"installPath": "<teatree-clone>", ...}]}}
```

Claude Code reads hooks, skills, and agents directly from the clone — no cache,
no version pinning, always live. There is **no** `~/.claude/plugins/t3` symlink;
`t3 setup`'s `_cleanup_legacy_plugin` removes any leftover one from the old
symlink model. This replaced both the symlink approach and the older marketplace
approach (which copied to `~/.claude/plugins/cache/` and went stale).

## Post-Rename Verification

When renaming directories (e.g., `skills/t3:code/` → `skills/code/`):

- Check for **symlinks outside the repo** (e.g., `~/.claude/skills/t3:*`) — these break silently
- Use `command rm` (not bare `rm`) on macOS/zsh — aliases may prevent deletion
- Grep ALL consumers: settings.json hook paths, skill symlinks, tests, scripts

## External State Checklist

Changes that affect external state (outside the git repo) require manual verification:

- `~/.claude/settings.json` hook paths
- `~/.claude/skills/` symlinks
- `~/.claude/plugins/installed_plugins.json` — the `t3@souliane` `installPath`
