---
name: workspace
description: Environment and workspace lifecycle — worktree creation, setup, DB provisioning, dev servers, cleanup. Use when user says "create worktree", "setup", "start servers", "refresh DB", "cleanup", or any infrastructure task.
requires:
  - rules
compatibility: macOS/Linux, zsh or bash, git, docker with compose plugin, PostgreSQL CLIs (psql, createdb, dropdb, pg_restore), direnv, lsof. Optional dslr, uv, jq.
triggers:
  priority: 120
  keywords:
    - '\b(worktree|setup|servers?|start session|refresh db|cleanup|clean up|reset passwords?|restore.*(db|database))\b'
    - '\b(database|start (the )?backend|start (the )?frontend)\b'
search_hints:
  - setup
  - worktree
  - create worktree
  - servers
  - cleanup
metadata:
  version: 0.0.1
  subagent_safe: false
---

# Environment & Workspace Lifecycle

The infrastructure foundation. Every other teatree skill depends on this one.

Manages **multi-repo worktree workspaces** — creating synchronized git worktrees across multiple independent repositories for a single ticket, then provisioning each with isolated ports, databases, env files, and services so they're ready to use immediately.

```mermaid
graph TD
  subgraph "$T3_WORKSPACE_DIR"
    direction TB
    subgraph "Main repos (default branch)"
      main_be["acme-backend/"]
      main_fe["acme-frontend/"]
      main_tr["acme-translations/"]
    end
    subgraph "Ticket worktrees"
      subgraph "ac/1234/"
        wt_be["acme-backend/<br/>(worktree)"]
        wt_fe["acme-frontend/<br/>(worktree)"]
        wt_tr["acme-translations/<br/>(worktree)"]
        envfile[".env.worktree<br/>(shared DB, variant)"]
      end
      subgraph "ac/5678/"
        wt2_be["acme-backend/<br/>(worktree)"]
        wt2_fe["acme-frontend/<br/>(worktree)"]
      end
    end
  end

  main_be -.->|"git worktree"| wt_be
  main_fe -.->|"git worktree"| wt_fe
  main_tr -.->|"git worktree"| wt_tr
  main_be -.->|"git worktree"| wt2_be
  main_fe -.->|"git worktree"| wt2_fe
```

Each ticket gets its own directory with one git worktree per affected repo and a shared `.env.worktree` for database name and variant configuration. Ports are ephemeral — allocated at `lifecycle start` time and passed via runtime env only. Worktrees share the `.git` directory with the main clone but have their own branch and working tree.

## Dependencies

None — this is the foundation skill.

## Configuration (`~/.teatree`)

Key variables used by this skill (see `/t3:setup` for the full config reference):

| Variable | Required | Purpose |
|----------|----------|---------|
| `T3_REPO` | Yes | Path to the teatree repo clone |
| `T3_WORKSPACE_DIR` | Yes | Root workspace directory |
| `T3_BRANCH_PREFIX` | No | Prefix for worktree branches (default: initials from `git config user.name`) |
| `T3_AUTO_SQUASH` | No | Auto-squash related unpushed commits before push (default: `false`) |
| `T3_SHARE_DB_SERVER` | No | Share one Postgres server across worktrees (default: `true`). Each worktree gets its own DB name but connects to the same server. When `false`, each worktree starts its own Postgres container. |

### Data Directory (XDG-Compliant)

Teatree stores runtime data (ticket cache, MR reminders, followup dashboard) in:

```text
$T3_DATA_DIR  (default: ${XDG_DATA_HOME:-$HOME/.local/share}/teatree)
```

`~/.teatree` is the **config file** — never use it as a data directory. Set `T3_DATA_DIR` in `~/.teatree` to override the default location.

## Setup Verification

If the environment seems incomplete (missing `uv`, hooks not firing, overlay absent), load `/t3:setup` to run the bootstrap validator.

## Commands

All workspace operations go through the `t3` CLI. Run `t3 <overlay> --help` for the full command list. Key command groups: `lifecycle` (setup/start/restart/teardown), `workspace` (ticket/finalize/clean-all), `run` (backend/frontend/tests), `db` (refresh/restore-ci/reset-passwords).

## Rules

### Plan Before Executing (Non-Negotiable)

Before starting any multi-step task, **create a TODO list** using the task tracking tools. This applies to all phases (setup, coding, testing, shipping) — not just coding. Never tackle work without a visible plan. The plan keeps the user informed and prevents forgetting steps.

- **Simple tasks** (1-2 steps): a brief bullet list in the response is sufficient.
- **Complex tasks** (3+ steps): use the agent's task tracking tools for each step, update status as you go.
- **Never skip this.** If you find yourself doing 3+ things without a plan, stop and create one.

### Fix the CLI, Never Work Around It (Non-Negotiable)

When a `t3` command fails, **fix the CLI code first** — never manually run the underlying commands (`docker compose`, `manage.py runserver`, `npm run`, `createdb`, `cp`, `ln -s`, etc.) as a workaround. Manual workarounds invariably miss steps (translations, symlinks, settings files, CORS, SSL flags) and create a broken environment that wastes more time than fixing the CLI would have.

1. **Stop** — do not run the underlying command manually.
2. **Investigate** the overlay or core code to find why the command failed.
3. **Fix** the code, add a test, and commit.
4. **Re-run** the `t3` command to verify the fix.

### Never Hand-Edit Generated Files (Non-Negotiable)

Setup tools (`t3 lifecycle setup`, etc.) generate configuration files (`.env.worktree`, docker overrides, port allocations). **Manual edits create drift** and are overwritten on the next setup run.

When a generated file is wrong or incomplete, **re-run the setup tool** — don't manually patch the file. If setup fails, diagnose the root cause in the setup script (see `/t3:debug`), don't work around it.

### Never Run Infrastructure Commands Directly (Non-Negotiable)

Use the `t3` CLI (`t3 lifecycle start`, `t3 run backend`, `t3 run frontend`, etc.) instead of running `docker compose`, language-specific dev servers, or build tools directly. The CLI commands handle:

- Environment variable loading from generated files
- Service ordering (data store → migrations → application)
- Port isolation between worktrees
- Health checks after startup

Direct commands bypass these safeguards, causing subtle failures (wrong DB, port collisions, missing migrations).

### Never Edit Files in the Main Clone (Non-Negotiable)

Before editing **any** project file, verify you are working in a **worktree**, not the main clone. The main repo clone (the directory directly under `$T3_WORKSPACE_DIR` with the default branch) is for `git fetch`, branch management, and worktree creation — never for code changes.

**Pre-edit check:** If the file you are about to edit lives directly under `$T3_WORKSPACE_DIR/<repo>/` (not under a ticket subdirectory like `$T3_WORKSPACE_DIR/<ticket>/<repo>/`), **stop** — you are in the main clone. Find or create the correct worktree first via `t3 workspace ticket`.

Common failure: the main clone happens to be on the MR branch (from a previous checkout). Editing there "works" but pollutes the shared clone, risks merge conflicts for other worktrees, and violates isolation.

### Full Worktree Isolation (Non-Negotiable)

Each worktree gets its own **isolated environment** — dedicated database, ports, containers, and env files. Never share infrastructure between worktrees:

- Never point one worktree's frontend at another worktree's backend
- Never use the main repo's database for worktree work
- Never manually set ports — let `t3 lifecycle setup` allocate them via `find_free_ports()`

When testing an MR, create a full worktree (`t3 workspace ticket` + `t3 lifecycle setup` + `t3 lifecycle start`).

### Validate After Provisioning (Non-Negotiable)

After importing a database or downloading an artifact, always validate it:

- **Check file sizes** — 0-byte files indicate failed downloads (often VPN/network issues)
- **Spot-check data** — empty seed/reference tables indicate a corrupt import; the application will crash on every request with lookup errors
- If validation fails, **delete the corrupt artifact and re-run provisioning**. Never try to manually fix corrupt data — interdependent reference tables make this a losing game.

### Service Startup Ordering (Non-Negotiable)

Setup tools enforce ordering: **data store → migrations → application server**. Starting the application before migrations causes "relation does not exist" errors. Always use the orchestration functions (`t3 lifecycle start`) rather than starting services individually.

### Never Delegate Skill-Dependent Work to Sub-Agents (Non-Negotiable)

See [`../t3:rules/SKILL.md`](../t3:rules/SKILL.md) § "Sub-Agent Limitations". If parallelism is needed, pass the **full skill file contents** in the sub-agent prompt — but prefer sequential main-conversation execution.

### Verify Services Before Declaring Running (Non-Negotiable)

After starting dev servers, **verify each service responds via HTTP** before reporting success. Check that frontend, backend, and API endpoints return expected status codes (2xx/3xx). If any check fails (000, 500, connection refused), diagnose before reporting — see troubleshooting docs.

Project skills define the specific endpoints to check (e.g., admin login, API version, frontend index).

## Extension Points

For the full extension points table, override chain, and project skill creation guide, see [`references/extension-points.md`](references/extension-points.md).

Key methods on `OverlayBase`: `get_repos()`, `get_provision_steps()`, `get_db_import_strategy()`, `get_env_extra()`, `get_run_commands()`, `get_services_config()`, `get_verify_endpoints()`. See the reference for the full list.

## Lifecycle State Machine

```mermaid
stateDiagram-v2
    provisioned --> provisioned : db_refresh
    services_up --> provisioned : db_refresh
    ready --> provisioned : db_refresh
    created --> provisioned : provision
    provisioned --> services_up : start_services
    created --> created : teardown
    provisioned --> created : teardown
    ready --> created : teardown
    services_up --> created : teardown
    services_up --> ready : verify
```

## Troubleshooting

Before any setup or server operation, check [`references/troubleshooting.md`](references/troubleshooting.md) for known failure modes matching the current operation.

## Skill File Locations & Symlink Chain

```text
<agent-skills-dir>/* → $T3_REPO/skills/*
                            (SOURCE OF TRUTH)
```

The agent skills directory varies by platform (for example `~/.claude/skills/`, `~/.codex/skills/`, `~/.cursor/skills/`, or `~/.copilot/skills/`).

- **NEVER** replace a symlink with a real file/directory. If unsure, run `ls -la` first.
- **Before writing to any skill file**, resolve the real path: `readlink -f <path>`.

## Reference Index

| When you need to... | Read |
|---|---|
| Check tool requirements or first-time setup | [`references/prerequisites.md`](references/prerequisites.md) |
| Find available shell functions, scripts, or COMPOSE_PROJECT_NAME details | [`references/scripts-and-functions.md`](references/scripts-and-functions.md) |
| Understand extension points, override chain, or create a project skill | [`references/extension-points.md`](references/extension-points.md) |
| Diagnose worktree setup failures, DB errors, port conflicts | [`references/troubleshooting.md`](references/troubleshooting.md) |
| Cross-cutting agent rules (clickable refs, token extraction, temp files) | [`../t3:rules/SKILL.md`](../t3:rules/SKILL.md) |
