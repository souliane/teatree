---
name: setup
description: Bootstrap and validate teatree for local use — prerequisites, config, skill symlinks, optional agent hooks, and Django project scaffolding. Use when user says "setup skills", "install skills", "bootstrap skills", or needs first-time teatree installation.
eval_exempt: first-time bootstrap/validation reference; one-shot installation steps, no recurring agent decision surface
compatibility: macOS/Linux, git, python3.13+, uv.
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Teatree Setup

Bootstrap and validate the local teatree environment. This skill is now a doctor/bootstrap-validator for the Django rewrite, not a shell-overlay scaffolder.

## Dependencies

None. This is the entry point for getting teatree running.

## Architecture Reality

Teatree now has two layers:

- The **skill repo** you are in now. It provides the workflow skills (`skills/*/`), references, hooks, and the `teatree` Django extension package.
- An **overlay package** created with `t3 startoverlay`. The overlay subclasses `OverlayBase` and registers via entry points. Teatree itself owns the Django settings.

Do not scaffold `scripts/lib/bootstrap.sh`, `project_hooks.py`, or shell overlays. Those are legacy migration artifacts.

## What This Skill Does

1. Check prerequisites.
2. Set the bootstrap environment variables and seed config settings.
3. Install teatree skill symlinks for the current agent runtime.
4. Optionally wire Claude Code hooks and statusline.
5. Generate an overlay package with `t3 startoverlay`.
6. Verify the overlay package installs correctly.

## Step 1: Prerequisites

Verify:

```bash
python3 --version
uv --version
git --version
jq --version
```

Recommended but optional:

```bash
docker --version
docker compose version
psql --version
glab auth status
gh auth status
```

Rules:

- Python must be 3.13+ for the Django rewrite.
- Prefer `uv` for all project commands.
- Treat missing optional tools as warnings, not blockers, unless the user explicitly needs that integration.

## Step 2: Set environment variables and seed config

Export the bootstrap variables below in your shell profile (or the agent's
environment). Operational settings live in the teatree DB — seed them with
`t3 <overlay> config_setting set`.

Required (bootstrap environment):

| Variable | Purpose |
| --- | --- |
| `T3_REPO` | Path to the teatree clone |
| `T3_WORKSPACE_DIR` | Root workspace directory |

Useful optional values:

| Variable | Purpose | Default |
| --- | --- | --- |
| `T3_CONTRIBUTE` | Allow self-improvement commits in the teatree repo | `false` |
| `T3_PUSH` | Allow pushing retro commits (safety gate for privacy review) | `false` |
| `T3_AUTO_PUSH_FORK` | Auto-push retro commits to the user's fork without prompting (requires `T3_PUSH=true` and origin ≠ `T3_UPSTREAM`) | `false` |
| `T3_MODE` | Effective publishing mode. `auto` opts the pipeline into pushing without pausing for shipping approval; `interactive` (default) gates at shipping. Equivalent DB-home setting: `t3 <overlay> config_setting set mode auto`. (Supersedes the retired `T3_AUTO_SHIP` env var, #2697.) | `interactive` |
| `T3_UPSTREAM` | Upstream repo for PRs (empty = PR on origin, set = PR on upstream) | empty |
| `T3_PRIVATE_TESTS` | Private QA repo path | empty |
| `T3_BRANCH_PREFIX` | Branch prefix for generated worktrees | derived from git user |
| `T3_ISSUE_TRACKER` | `gitlab` or `github` | detected |
| `T3_SKILL_OWNERSHIP_FILE` | Ownership config for skill editing | `$HOME/.ac-reviewing-codebase` |

Do not require `T3_OVERLAY`. The active overlay is discovered via entry points.

Example (bootstrap environment):

```bash
export T3_REPO="$HOME/workspace/teatree"
export T3_WORKSPACE_DIR="$HOME/workspace"
export T3_ISSUE_TRACKER="gitlab"
export T3_PUSH=false
export T3_AUTO_PUSH_FORK=false
export T3_UPSTREAM=""
export T3_PRIVATE_TESTS=""
export T3_BRANCH_PREFIX="ac"
export T3_SKILL_OWNERSHIP_FILE="$HOME/.ac-reviewing-codebase"
```

Operational settings go in the DB store, e.g.:

```bash
t3 <overlay> config_setting set contribute false
t3 <overlay> config_setting set mode interactive
```

### Slack integration (per-overlay)

Messaging is configured per overlay in the DB `overlays` registry row, not via `T3_CHAT_PLATFORM`.
Three commands cover distinct phases of the Slack lifecycle — they are not
interchangeable:

- `t3 setup slack-bot --overlay <name>` — first-time bootstrap: registers the
  app and **captures** the bot (`xoxb-`) + app-level (`xapp-`) tokens into
  `pass`, recording the overlay's Slack settings.
- `t3 setup slack-provision [--overlay <name>]` — the idempotent one-shot
  lifecycle pass: pushes the manifest (all bot + user scopes), prints the OAuth
  reinstall URL, joins review channels, provisions the bot IM, and **verifies**
  the shared personal token's scopes. It reads tokens; it does not capture them.
- `t3 setup slack-user-token` — (re)captures the personal user (`xoxp-`) token
  after a manual OAuth reinstall and verifies its scopes.

Token writes are prefix-validated (`xoxb-`/`xapp-`/`xoxp-`) and back up any
prior value to a timestamped `pass` key before overwriting, so a mis-pasted
token can never clobber a good secret.

Then start `t3 slack listen` for real-time event delivery via Socket Mode.

```toml
[overlays.<name>]
messaging_backend = "slack"
slack_user_id = "U..."
slack_token_ref = "teatree/<name>/slack"
```

## Step 3: Install Skills

`t3 setup` installs teatree's skills per runtime:

- **Claude** — core skills ship inside the plugin, which `t3 setup` registers in
  `~/.claude/plugins/installed_plugins.json` with `installPath` pointing at the
  main clone (no `~/.claude/plugins/t3` symlink — any leftover legacy one is
  removed by `_cleanup_legacy_plugin`). Because the plugin carries the core
  skills, `t3 setup` does **not** symlink them into `~/.claude/skills/`; any
  leftover core symlinks from pre-plugin installs are pruned to avoid duplicate
  entries. Overlay skills (not shipped by the plugin) are still symlinked.
- **Other runtimes** listed in `teatree.cli.setup.AGENT_SKILL_RUNTIMES`
  (e.g. Codex) are targeted when their home directory already exists, and
  receive symlinks for both core and overlay skills.

Contributor-mode symlinks that point inside the configured `workspace_dir`
are preserved across runs.

Inspect the result with `t3 info` — the "Skills installed to" section lists
every runtime dir teatree detected along with the count of managed symlinks.

## Step 4: Claude Code Plugin Hooks

Teatree ships a `hooks.json` that Claude Code loads automatically. `t3 setup` registers the plugin in `~/.claude/plugins/installed_plugins.json` with `installPath` pointing at the main clone (no symlink, always live). All hooks route through `hook_router.py`, a unified Python router that handles event dispatch.

Verify the registration: `t3 doctor check` (it reports the registered plugin and its `installPath`).

`t3 setup` also registers + enables the external `pyright-lsp@claude-plugins-official`
plugin (from the `anthropics/claude-plugins-official` marketplace) via the `claude
plugin` CLI, so agents get LIVE pyright type diagnostics while coding instead of only
catching type errors at CI. It is best-effort/offline-safe (an unreachable marketplace
WARNs and continues) and needs `pyright-langserver` (npm `pyright`, baked into the
deploy image) on PATH — `t3 doctor check` advisory-WARNs when the plugin is disabled or
the langserver is missing. Both plugins are pinned in the managed
`deploy/claude-settings.template.json` `enabledPlugins`, so every seeded container
enables them and the host drift check re-asserts them.

The hooks cover these events:

| Event | Matcher | Purpose |
| --- | --- | --- |
| `SessionStart` | *(none)* | Bootstrap CLI availability; on `source=compact` re-inject the pre-compaction snapshot (#845); surface the enabled-MCP connectivity advisory when any MCP server is configured (#2282) |
| `UserPromptSubmit` | *(none)* | Detect intent from prompt keywords and suggest skills to load |
| `PreToolUse` | `Bash\|Edit\|Write` | Branch protection, skill enforcement |
| `PostToolUse` | `Bash\|Write\|Edit\|Read\|Grep\|Glob` | Track which repos are touched |
| `PostToolUse` | `Skill` | Track loaded skills |
| `InstructionsLoaded` | *(none)* | Track loaded skills (belt-and-suspenders fallback) |
| `PreCompact` | *(none)* | Write a durable-state snapshot before compaction (agent-action-free) |
| `SessionEnd` | *(none)* | Session cleanup |

All hook scripts live in `$T3_REPO/hooks/scripts/`. The `hooks.json` at `$T3_REPO/hooks/hooks.json` defines the routing table.

## Step 4a: Structured-Search MCP Server

Teatree ships a plugin-bundled `.mcp.json` at the repo root declaring the `teatree` stdio MCP server (`t3 mcp serve`, built under [#1023](https://github.com/souliane/teatree/issues/1023)) — the same convention official Claude Code plugins use. Claude Code starts a plugin-bundled MCP server automatically once the plugin is enabled, so once `t3 setup` has registered and enabled the plugin (Step 4), the `mcp__teatree__*` tools are reachable — no separate `claude mcp add` step. The surface is **read + gate-preserving writes**: ~13 read tools (`ticket_search`, `ticket_get`, `ticket_list`, `worktree_status`, `pr_for_ticket`, `task_list`, `loop_stats`, `command_search`, `config_setting_get`, `gate_status`, `factory_signals`, …) PLUS the write suite (`pr_create`, `pr_merge`, `notify_user`, `config_setting_set`, `task_create`, the review-post and per-service forge/slack writes). The writes are **not** an escape hatch: each wraps the exact seam the `t3` CLI calls, so the shipping-phase FSM, sanctioned-merge keystone, on-behalf verdict, and leak-scrub gates all fire identically. Per-service groups (forge/slack/notion/sentry) register only when an overlay declares the service. Use `command_search` to discover which tool covers a task rather than enumerating them here.

`t3 setup` confirms the file is intact (`OK`/`WARN` line naming `.mcp.json`); `t3 doctor check` re-verifies it and, when `claude` is on PATH, live-probes visibility via `claude mcp list` too ([#2863](https://github.com/souliane/teatree/issues/2863)). Prefer these tools over shelling out to `t3 ... list` and parsing text wherever a tool already covers the query — see `/t3:ship` § 4b step 5 for a worked example.

## Step 4b: Recommended Global Agent Config

**Strongly recommend** the user adds the following rule to their global agent instructions file (e.g., `~/.claude/CLAUDE.md`). AI agents consistently bypass the `t3` CLI to run underlying commands directly (e.g., `manage.py runserver`, `docker compose`, `ln -s`, `pipenv install`) when `t3` fails. This invariably produces a broken environment because the CLI handles env vars, symlinks, port allocation, translations, and SSL config that manual commands miss.

Suggest this snippet:

```markdown
## BLOCKING REQUIREMENT: Fix Tooling, Never Work Around It

When a `t3` CLI command fails (setup, start, run, or any other):
1. **STOP.** Do NOT manually run the underlying commands (docker compose, manage.py runserver, npm run, createdb, cp, ln -s, etc.).
2. **Investigate the t3 overlay/core code** to find WHY the command failed.
3. **Fix the t3 code** (overlay or core).
4. **Re-run the t3 command** to verify the fix.
5. Only THEN continue with the original task.

**Manual workarounds are NEVER acceptable** — not even "just this once", not even "to save time", not even "while I fix it later."
```

This rule exists in `workspace/SKILL.md` but skills are only loaded on demand. The global agent config is **always loaded**, catching the agent before it has a chance to improvise.

## Step 4c: Recommended Auto-Mode Authorizations

Teatree ships **no** classifier `autoMode`/`permissions` allow-list — classifier rules always remain per-user (BLUEPRINT §11.4). It does document a generic, parameterized recommended set that makes a session friction-free, and detects (read-only, never applies) which entries are absent from your own `~/.claude/settings.json`.

Run `t3 doctor authorizations` (also surfaced by `t3 doctor check` and at the end of `t3 setup`). For each missing rule it prints the exact sentence to paste into your `autoMode.allow` array. The full set, the rationale, and what is deliberately left to the user (VPS hosts, dev-DB creds, exact paths) are documented in [`references/recommended-automode-authorizations.md`](references/recommended-automode-authorizations.md).

For the broader picture — operating mode (DB-home `mode`, set via `t3 <overlay> config_setting set mode …`), the `auto`-mode training wheels, how overlays declare their MCP/messaging integration, and the post-setup permission state — see [`references/agent-mode-and-mcp-config.md`](references/agent-mode-and-mcp-config.md). It maps each config surface to the module that owns it so the docs cannot drift from the code.

## Step 5: Generate an Overlay Package

Use the TeaTree bootstrap CLI, not shell overlay scaffolding.

Basic form:

```bash
t3 startoverlay <overlay-name> <destination-dir>
```

When the filesystem directory and Python package should differ, also pass:

```bash
t3 startoverlay my-overlay ~/workspace/my-org --overlay-package my_overlay
```

Generated shape:

```text
my-overlay/
├── pyproject.toml
└── my_overlay/
    ├── apps.py
    ├── overlay.py
    ├── models.py
    └── SKILL.md
```

Rules:

- `overlay.py` must subclass `OverlayBase`.
- The overlay must be registered as a `teatree.overlays` entry point in `pyproject.toml`.
- The generated overlay package is the place where project-specific customisation lives.

## Step 6: Verify the Generated Project

Run verification in a Python 3.13 environment with TeaTree installed:

```bash
uv run --python /opt/homebrew/bin/python3.13 \
  --with 'teatree @ file://'"$T3_REPO" \
  --with 'django>=5.2,<6.1' \
  --with django-tasks \
  --with django-tasks-db \
  --with django-typer \
  python manage.py check
```

If the user already has a project-local virtualenv, using that is fine too. The important check is that `manage.py check` succeeds with `DJANGO_SETTINGS_MODULE` unset so the generated settings module wins.

## Companion Skills

Offer companion skills by stack, but install them only if they are missing:

- All stacks: `ac-reviewing-codebase`
- Django: `ac-django`, `ac-python`
- Python only: `ac-python`
- Ruff migration work: `ac-adopting-ruff`

Prefer consumer installs for skills the user does not maintain:

```bash
npx skills add <upstream-owner>/skills --skill ac-django --skill ac-python -g -y
```

## Rules

- Do not reintroduce `T3_OVERLAY` as a required bootstrap concept.
- Do not scaffold shell wrappers or `project_hooks.py`.
- Prefer `t3 ...` and Django settings over `PYTHONPATH` tricks.
- Stop at the first broken prerequisite and fix that before continuing.
