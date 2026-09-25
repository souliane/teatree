# Plugin Packaging Checklist

Verified failures from issue #3 shipping. Every item below caused a real breakage.

## Portable Package Shape

TeaTree is one plugin package with two native manifest wrappers:

- `.claude-plugin/plugin.json` for Claude Code
- `.codex-plugin/plugin.json` for Codex
- `.claude-plugin/marketplace.json` and `.agents/plugins/marketplace.json` for
  each runtime's local marketplace discovery
- shared `skills/`, `.mcp.json`, hook scripts, and policy code

The wrappers are deliberately separate because the hosts use different manifest
schemas. They point at the same checkout and do not duplicate the implementation.
Claude Code registration loads TeaTree's skills, MCP server, agents, and full hook
workflow. Codex registration is also necessary for ordinary interactive Codex to
discover the shared skills and MCP server; the headless app-server harness itself
does not depend on plugin registration for transport or authentication.

The validated Codex manifest exposes skills and MCP only. Its schema rejects a
`hooks` field, so `hooks/codex.json` and the adapter are inactive experiments, not
plugin capabilities. They must not be described as parity until plugin validation
accepts them and an end-to-end Codex invocation proves their environment and
decision contracts.

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

## Codex Local Marketplace Registration

`t3 setup` builds a slim marketplace under `~/.local/share/teatree/codex-plugin`
— `.agents/plugins/marketplace.json` plus `plugins/t3/` holding `.codex-plugin/` and
every `./` path `plugin.json` declares, as real files — registers it as the
`souliane` Codex marketplace and installs `t3@souliane`. `codex plugin add` copies
its source, so pointing it at the checkout copied `.git` and every virtualenv; the
slim tree is capped at 50 MiB and refuses a declared path at or outside the manifest
root or a symlinked directory. Staging a failed or timed-out add leaves under
`plugins/cache/*/plugin-install-*` is removed. Re-running setup is idempotent; an
older registration pointing at a checkout is replaced.

## Post-Rename Verification

When renaming directories (e.g., `skills/t3:code/` → `skills/code/`):

- Check for **symlinks outside the repo** (e.g., `~/.claude/skills/t3:*`) — these break silently
- Use `command rm` (not bare `rm`) on macOS/zsh — aliases may prevent deletion
- Grep ALL consumers: settings.json hook paths, skill symlinks, tests, scripts

## External State Checklist

Changes that affect external state (outside the git repo) require manual verification:

- `~/.claude/settings.json` hook paths
- `~/.claude/skills/` symlinks
- `~/.agents/skills/` symlinks
- `~/.claude/plugins/installed_plugins.json` — the `t3@souliane` `installPath`
- Codex `souliane` marketplace and `t3@souliane` installation
