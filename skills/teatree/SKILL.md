---
name: teatree
description: TeaTree agent lifecycle platform — core architecture, lifecycle phases, CLI reference, overlay API, skill loading, and plugin hooks. Use when working on teatree itself or when understanding how teatree orchestrates agent workflows. Mode-specific skills (dogfooding, planning, batch, bug hunt) are separate — see the "Related skills" section below.
eval_exempt: architecture/CLI reference for working on teatree itself; behavioural invariants live in the rules skill and the regression corpus, not in this overview
metadata:
  version: 0.0.1
triggers:
  priority: 90
  keywords:
    - '\b(teatree|t3)\b'
    - '\b(overlay|provision|headless)\b'
  exclude: '\b(t3:code|t3:test|t3:ship|t3:debug|t3:review|batch mode|bug hunt|dogfood|plan(ning)?|backlog|prioriti[zs]e)\b'
search_hints:
  - teatree
  - lifecycle
  - overlay
  - provision
  - headless
  - skill loading
  - agent workflow
---

# TeaTree — Agent Lifecycle Platform

TeaTree is a personal code factory for multi-repo projects — it turns a ticket URL into a merged pull request by driving AI agents through lifecycle phases. Under the hood it's a Django project; overlays are lightweight Python packages that extend it for specific projects.

## Architecture

- **TeaTree IS the Django project.** Requires a local clone; installed via `uv tool install --editable .`.
- **Overlays** register via `teatree.overlays` entry points and provide project-specific configuration.
- **Skills** live in `skills/` and are loaded by the agent's skill system.
- **Hooks** in `hooks/scripts/` run on agent lifecycle events (e.g., prompt submit, pre/post tool use).

## Lifecycle Phases

```
ticket → code → test → review → ship → review-request
```

Each phase maps to a skill (`t3:ticket`, `t3:code`, etc.). The `Session` model tracks visited phases and enforces quality gates (e.g., can't ship without testing).

## CLI Reference

Top-level commands (no overlay needed): `t3 startoverlay`, `t3 docs`, `t3 agent`, `t3 sessions`, `t3 cost`, `t3 speak`, `t3 ui`, `t3 admin`, `t3 info`, `t3 config`, `t3 banned-terms`, `t3 ci`, `t3 codex`, `t3 review`, `t3 review-request`, `t3 eval`, `t3 doctor`, `t3 tool`, `t3 setup`, `t3 update`, `t3 assess`, `t3 overlay`, `t3 infra`, `t3 loop`, `t3 slack`, `t3 task`, `t3 recover`, `t3 dogfood`, `t3 dream`, `t3 mutation`. (This list is kept honest by `tests/teatree_skill_support/test_teatree_skill_cli_reference.py`, which asserts every name is a registered `t3` command; the in-sync full reference with descriptions is `docs/generated/cli-reference.md`.)

Overlay-scoped commands require `t3 <overlay> <subcommand>` (e.g., `t3 teatree`):

```bash
t3 loop start                         # Spawn Claude Code session with the fat loop
t3 loop tick                          # Run one loop tick (scans all overlays)
t3 loop status                        # Show the loop's last-rendered statusline
t3 <overlay> resetdb                  # Drop and recreate the SQLite database
t3 <overlay> worktree provision          # Provision worktree (ports, DB, overlay steps)
t3 <overlay> worktree start          # Start dev servers
t3 <overlay> worktree status         # Show worktree state
t3 <overlay> worktree teardown       # Stop services, clean up
t3 <overlay> tasks work-next-sdk      # Claim/execute next headless task; refuses loop-dispatched phases unless LOOP_ALLOW_HEADLESS_DISPATCH
t3 <overlay> tasks work-next-user-input  # Claim and launch next interactive task
t3 <overlay> followup sync            # Daily ticket/PR sync
```

## Key Models

- **Ticket** — issue URL, overlay, variant, repos
- **Worktree** — repo path, branch, ports, state (FSM: created → provisioned → services_up → ready)
- **Session** — agent session with visited phases, repos modified/tested
- **Task** — claimable work unit with lease, heartbeat, parent chain
- **TaskAttempt** — execution result with exit code, structured output

## Overlay API

Overlays subclass `OverlayBase` and override methods:

- `get_repos()` — repo list for worktree creation
- `get_provision_steps(worktree)` — setup steps (migrations, fixtures)
- `get_run_commands(worktree)` — dev server commands
- `get_db_import_strategy(worktree)` — DSLR/dump import config
- `get_services_config(worktree)` — Docker services
- `get_visual_qa_targets(changed_files)` — URL paths the pre-push browser sanity gate should load (default: `[]` — opt in by mapping diff paths to URLs)
- `get_e2e_env_extras(env_cache)` — overlay-specific env vars merged into the Playwright environment (e.g. map `WT_VARIANT` → `CUSTOMER`); default `{}`
- `get_e2e_preflight(customer, base_url)` — pre-Playwright gates that fail fast on auth/SSO/network issues; default `[]`

## Skill Loading

TeaTree's UserPromptSubmit hook detects intent from user prompts using `triggers:` patterns in skill frontmatter. The hook suggests loading the matching skill. A PreToolUse hook blocks Bash/Edit/Write until suggested skills are loaded.

The `SkillLoadingPolicy` class resolves which skills to load based on intent, overlay, and current phase. For headless tasks, `search_hints` in frontmatter provide keyword matching.

## Plugin Hooks Architecture

Hooks are registered in `hooks/hooks.json` (shipped with the plugin). This is the **sole source** for hook registrations — do NOT duplicate hooks in the user's `~/.claude/settings.json`. When migrating hooks to the plugin, remove the `settings.json` equivalents in the same change to avoid double execution.

## Management Command Patterns

Teatree's CLI groups (`t3 <overlay> <group> <sub>`) are django-typer `TyperCommand` classes invoked via Django's `call_command` (see `src/teatree/cli/overlay.py:430` → `managepy(...)`). To propagate a non-zero exit code from a subcommand, **use `raise SystemExit(N)` — NOT `raise typer.Exit(code=N)`**.

`typer.Exit` is designed for the typer CLI runner; when it's raised inside a TyperCommand reached via `call_command`, the exception is silently swallowed and the process exits 0 even though the failure was raised. `SystemExit` bubbles up through Django management → `subprocess.run(check=True)` → CLI exit code.

- Canonical example: `src/teatree/core/management/commands/tasks.py:19` — `raise SystemExit(1)` after `self.stderr.write(...)`.
- Tests: `with pytest.raises(SystemExit) as exc_info: call_command(...)` then assert `exc_info.value.code == N`. `pytest.raises(typer.Exit)` reports `DID NOT RAISE` even though the source did raise — call_command eats it before pytest sees it.
- `typer.Exit` is still correct in `src/teatree/cli/*.py` files that go through the typer runner directly (different call site).
- Anti-pattern: returning an error string from a management command instead of raising. The CLI exits 0 and CI reports green on real failures.

### Annotated typer options must have defaults for `call_command`

`Annotated[str, typer.Option(help="...")]` parameters without a default value make the command unusable via Django's `call_command` — it raises `Missing parameter: <name>` even when the caller passes the kwarg. Give every `typer.Option`-annotated parameter a default (e.g. `= ""`) and validate at runtime (`if not phase.strip(): raise SystemExit(1)`). This keeps both CLI and `call_command` call sites happy.

Canonical example: `src/teatree/core/management/commands/tasks.py` `create` subcommand — `phase: Annotated[str, typer.Option(...)] = ""` + runtime non-blank check.

## Configuration

`~/.teatree` sourced by hooks:

```bash
T3_REPO="$HOME/workspace/<your-username>/teatree"  # teatree repo path
T3_CONTRIBUTE=true                           # allow retro to modify core skills
T3_PUSH=false                                # gate pushes behind an explicit prompt
T3_AUTO_PUSH_FORK=false                      # auto-push to fork when T3_PUSH=true and origin ≠ T3_UPSTREAM
T3_AUTO_SHIP=false                           # when true, the in-session shipper pushes without waiting for approval; default gates push on user approval
T3_PRIVACY=strict                            # block commits with PII
```

## Interactive vs Headless Output

The `{"summary":..., "files_modified":...}` JSON result block from `/t3:next` is consumed by the headless pipeline. In interactive sessions it's noise — skip it and only show the text summary.

## Related Skills

This skill holds the core. Load the mode-specific skill for the task in hand — none `require:` this one, to keep per-invocation context small.

| Skill | When to load |
|-------|--------------|
| `/teatree-dogfood` | Validating a CLI, loop, or statusline change |
| `/teatree-plan` | Prioritizing the backlog via the GitHub Projects v2 board |
| `/teatree-batch` | Working the prioritized backlog unattended, one ticket at a time |
| `/teatree-bughunt` | Self-QA on the loop and statusline — find, file, and fix bugs in one session |
