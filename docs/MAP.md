# Codebase Map

The file to read first. One line per directory, so a fresh contributor or
sub-agent can find where something lives without reading the 2000-line
`BLUEPRINT.md` or grepping blindly. Each entry links to the relevant
`BLUEPRINT.md` section for the full design rationale.

TeaTree **is** a Django project (`manage.py` at the root); overlays are
lightweight entry-point packages. The Python source lives under
`src/teatree/`.

## Python source — `src/teatree/`

| Directory | Purpose | BLUEPRINT |
|---|---|---|
| `src/teatree/` | Package root: `__main__.py` entry point, `config.py` (`UserSettings` + the DB `ConfigSetting` store), `identity.py`, `paths.py` (XDG + worktree-aware DB isolation), `project.py`, `notify.py`, `on_behalf_gate.py`, `outbound_claim.py` | [§3](../BLUEPRINT.md#3-package-structure) |
| `src/teatree/cli/` | The `t3` CLI command tree — Typer apps for the Django-free bootstrap commands plus per-overlay subapp registration | [§8](../BLUEPRINT.md#8-command-tiers) |
| `src/teatree/core/` | The heart of teatree: the Django app with the FSM models, scanners, sync, cleanup, reconcile, signals, and provisioning | [§4](../BLUEPRINT.md#4-domain-models) |
| `src/teatree/core/models/` | FSM and supporting models (`Ticket`, `Worktree`, `Session`, `Task`, `TaskAttempt`) split into domain modules, plus shared errors/types | [§4](../BLUEPRINT.md#4-domain-models) |
| `src/teatree/core/runners/` | Transition runners — the long I/O for each lifecycle transition (provision, ship, retro, teardown, worktree start/verify), run by `@task` workers | [§4](../BLUEPRINT.md#4-domain-models) |
| `src/teatree/core/selectors/` | Read-only queries for tickets, sessions, tasks, and worktrees, consumed by loop scanners and the CLI without bypassing the FSM | [§4](../BLUEPRINT.md#4-domain-models) |
| `src/teatree/core/views/` | Django views for the inbound webhook receivers (GitHub, GitLab, Slack) with per-source rate limiting | [§4](../BLUEPRINT.md#4-domain-models) |
| `src/teatree/core/management/` | django-typer management commands (lifecycle, workspace, worktree, db, ticket, pr, followup, loop_tick, ...) — the DB-touching command tier | [§8](../BLUEPRINT.md#8-command-tiers) |
| `src/teatree/core/management/commands/` | The command modules themselves, one file per command group | [§8](../BLUEPRINT.md#8-command-tiers) |
| `src/teatree/core/migrations/` | Django schema migrations for the core app | [§4](../BLUEPRINT.md#4-domain-models) |
| `src/teatree/agents/` | Headless executor runtime: session handover, `claude -p` invocation, prompt building, skill-bundle resolution, model tiering, structured result schema | [§5](../BLUEPRINT.md#5-agent-execution) |
| `src/teatree/loop/` | Background `/loop` tick orchestration: scan in parallel, dispatch to phase agents, render the statusline | [§5](../BLUEPRINT.md#5-agent-execution) |
| `src/teatree/loop/scanners/` | Pure-Python signal collectors (one file each) feeding the loop tick — active tickets, assigned issues, PRs, approvals, pending tasks, ... | [§5](../BLUEPRINT.md#5-agent-execution) |
| `src/teatree/loop/self_improve/` | Self-improving monitor — cheap detectors (dispatch gaps, forgotten merges) on a tier-dispatched cadence | [§5](../BLUEPRINT.md#57-self-improving-monitor-loop-phase-1) |
| `src/teatree/loop/self_improve/detectors/` | Individual self-improve detectors | [§5](../BLUEPRINT.md#57-self-improving-monitor-loop-phase-1) |
| `src/teatree/loop/slack_answer/` | Reactive Slack-answer loop — classifier and answer cycle for inbound chat | [§5](../BLUEPRINT.md#58-reactive-slack-answer-loop-loop-phase-2--the-third-slot) |
| `src/teatree/backends/` | Pluggable external service adapters: GitHub/GitLab code-host clients and sync, Slack messaging, Notion, Sentry, plus the per-overlay loader | [§7](../BLUEPRINT.md#7-backend-protocols-and-abcs) |
| `src/teatree/utils/` | Pure utility modules — git, ports, db, diff coverage, compose contract, dependency drift, postgres secret helpers | [§3](../BLUEPRINT.md#3-package-structure) |
| `src/teatree/overlay_init/` | `t3 startoverlay` scaffold generation logic | [§6](../BLUEPRINT.md#6-overlay-system) |
| `src/teatree/contrib/` | First-party overlays shipped with the package | [§6](../BLUEPRINT.md#6-overlay-system) |
| `src/teatree/contrib/t3_teatree/` | TeaTree's own overlay (the dogfood overlay) | [§6](../BLUEPRINT.md#6-overlay-system) |
| `src/teatree/docker/` | Shared docker base-image build helpers (base-image sharing across worktrees) | [§6](../BLUEPRINT.md#6-overlay-system) |
| `src/teatree/templates/` | Django templates plus the `overlay/` cookiecutter-style scaffold used by `t3 startoverlay` | [§6](../BLUEPRINT.md#6-overlay-system) |
| `src/teatree/templates/overlay/` | The overlay scaffold rendered by `t3 startoverlay` | [§6](../BLUEPRINT.md#6-overlay-system) |
| `src/t3_bootstrap/` | Minimal entry-point shim that locates the teatree source and hands off to the CLI | [§8](../BLUEPRINT.md#8-command-tiers) |

## Top-level directories

| Directory | Purpose |
|---|---|
| `skills/` | Workflow skills loaded as `/t3:*` (`SKILL.md` + `references/`) — `code`, `ship`, `review`, `workspace`, `rules`, ... |
| `agents/` | Phase sub-agent definitions (`orchestrator`, `coder`, `reviewer`, `tester`, `shipper`, `debugger`, `e2e`, `e2e-review`, `planner`, `followup`, `answerer`, `scanning-news`) |
| `hooks/` | Plugin hooks: `hooks.json` event→script mapping and the `scripts/` hook router (UserPromptSubmit, PreToolUse, PreCompact, Stop) |
| `.claude-plugin/` | Plugin manifest — `plugin.json` (identity) and `marketplace.json` |
| `tests/` | Pytest suite, mirroring the `src/` module path (`teatree_core/`, `teatree_cli/`, `integration/`, ...). E2E lives here, not in a separate top-level dir |
| `docs/` | User-facing documentation (mkdocs site) plus `generated/` auto-generated reference |
| `scripts/` | Standalone build, install, lint, and hook helper scripts (`privacy_scan.py`, `ai_signature_scan.py`, `hooks/`, `lib/`) |
| `dev/` | Local dev/test infrastructure — docker compose, test/e2e Dockerfiles, the test matrix script |
| `docs/audits/` | Periodic codebase audit notes |

## Related navigation

- `BLUEPRINT.md` — full architecture (§3 is the authoritative, detailed
  package structure this map summarizes).
- `README.md` — product overview and getting started.
- `AGENTS.md` — agent instructions and test-writing doctrine.
- `CLAUDE.md` — the code-quality standard applied to every change.
