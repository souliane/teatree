# BLUEPRINT Appendix — Architecture & Package Structure

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §2–§3.

## 2. Architecture Principle: Code-First, Not Skills-First

Infrastructure and orchestration are Python code; development methodology is skill-guided prose. The split is load-bearing:

1. **Skills are prose, not code.** Prose produces different results depending on the model, context pressure, and what else is loaded. Python code handles edge cases correctly every time. Anything that must be deterministic — state transitions, port allocation, provisioning, sync, the `/loop` tick — is Python.
2. **Coordination needs transactional guarantees.** Django's FSM + ORM provide atomic transitions and row-locked workers. Coordination through JSON files cannot.
3. **Code is testable; prose is not.** Core logic must reach >90% branch coverage. Anything that requires that coverage lives in Python, not in SKILL.md.
4. **One ABC with a handful of methods beats thirty thin extension points.** Overlay customization goes through `OverlayBase` — typed methods with defaults, no priority system, no plugin registries.

**The split:**

- **Deterministic code** (Django app): state machines, port allocation, provisioning, task routing, code-host + messaging Protocols, sync, `/loop` tick, statusline rendering, CLI
- **Agent skills** (SKILL.md files): development methodology, guardrails, and domain knowledge — TDD discipline, debugging process, review checklists, retro learning, verification rules, coding standards. Skills drive the actual work; they use the CLI for infrastructure.

---

## 3. Package Structure

```
Package name: teatree (double-e)
Repo/CLI name: teatree / t3
Python: >=3.13
License: MIT
Build: uv
Entry point: t3 = teatree.cli:main
```

```
src/teatree/
  __init__.py
  __main__.py
  _overlay_api.py       # __overlay_api_version__ pin + import-time guard for overlays
  paths.py              # XDG-compliant DATA_DIR/CANONICAL_DB; worktree-aware: code run from a git worktree (`.git` is a file) auto-isolates onto a per-worktree DB under the sibling `~/.local/share/teatree-worktrees/<slug>/` root (never nested under the scanned canonical dir, so doctor/stale-DB checks stay clean); resolve_data_dir() returns (path, auto_isolated); an explicit canonical target from a worktree hard-fails (CanonicalDBFromWorktreeError) so unmerged migrations can never corrupt the canonical control DB
  config.py             # ~/.teatree.toml parsing, overlay discovery, UserSettings
  settings.py           # Django settings — auto-discovers overlay apps; seed_isolated_db() takes a consistent SQLite snapshot of the canonical DB into an auto-isolated worktree dir on first use, atomically (temp file + locked rename) and only for the auto-isolated case
  identity.py           # User identity + agent_signature suffix policy
  project.py            # Project root discovery
  triage.py             # Crash-report writers consumed by hooks
  timeouts.py           # Per-CLI subprocess timeouts
  types.py              # Shared TypedDicts / structural types (no deps)
  urls.py               # Django URLConf (admin only — no HTML dashboard)
  skill_map.py          # Phase → companion skills delegation map
  skill_deps.py         # Frontmatter `requires:` parser
  skill_loading.py      # Hook-side skill suggestion + cache
  skill_schema.py       # SKILL.md frontmatter schema
  trigger_parser.py     # `triggers:` regex compiler for skill auto-loading
  url_title_fetcher.py  # PR/issue title prefetch for hook trigger matching
  visual_qa.py          # Pre-push browser sanity gate (Playwright)
  cli_reference.py      # Generates docs/generated/cli-reference.md; command_paths/command_groups SSOT for the SKILL.md t3-invocation validator (#550)
  claude_sessions.py    # Resume helpers for `claude --resume`

  cli/                 # Typer CLI package — bootstrap commands (no Django needed)
    __init__.py         # Top-level `t3` app + overlay subapp registration
    agent.py            # `t3 agent`
    assess.py           # `t3 assess`
    ci.py               # `t3 ci ...`
    config.py           # `t3 config ...`
    doctor.py           # `t3 doctor ...`
    info.py             # `t3 info`, `t3 startoverlay`, `t3 docs`
    infra.py            # `t3 infra ...`
    loop.py             # `t3 loop start|stop|status|tick` (tick delegates to loop_tick mgmt cmd)
    overlay.py          # OverlayAppBuilder — builds the per-overlay subapp
    overlay_dev.py      # `t3 overlay install|uninstall|status` (dev loop)
    review.py           # `t3 review ...`
    review_request.py   # `t3 review-request ...`
    sessions.py         # `t3 sessions`
    setup.py            # `t3 setup ...`
    slack_setup.py      # `t3 setup slack-bot` walkthrough
    update.py           # `t3 update` (sync core + overlays ff-only)
    tools.py            # `t3 tool ...`

  core/                 # Django app: the heart of teatree
    apps.py             # AppConfig with auto-admin registration
    models/             # FSM + supporting models (see §4)
    managers.py         # Custom QuerySet managers
    overlay.py          # OverlayBase ABC + OverlayConfig dataclass (see §6)
    overlay_loader.py   # Entry-point overlay discovery + instantiation
    backend_factory.py  # iter_overlay_backends, OverlayBackends bundles for the loop
    sync.py             # Shared types, SyncBackend ABC, orchestrator (sync_followup) — platform-agnostic
    cleanup.py          # Shared worktree cleanup + squash-merge-aware branch classifier
    clone_paths.py      # Workspace/clone path resolution
    orphan_guard.py     # Detect orphan containers/DBs/dirs after teardown
    provisioners.py     # WorktreeProvisioner — runs overlay provision steps
    readiness.py        # HealthCheck + readiness-probe runner
    reconcile.py        # State reconciler (see §14.11)
    resolve.py          # Worktree-by-branch+repo lookup helper
    e2e_workitem.py     # #794 durable e2e recipe + env ladder + run provenance
    signals.py          # post_transition signals
    skill_cache.py      # Per-overlay skill metadata cache writer
    step_runner.py      # ProvisionStep / PostDbStep / pre-run executor
    tasks.py            # django-tasks workers (execute_provision, execute_ship, ...)
    urls.py             # Admin URLConf
    worktree_env.py     # render_env_cache + drift detection
    worktree_tasks.py   # Worker bodies: provision, ship, retrospect, teardown
    client-term-redacted.py           # Overlay/skill documentation generation
    admin.py            # Auto-registered admin
    runners/            # Phase runners (RetroExecutor, ShipExecutor, ...)
    selectors/          # Read-only query helpers used by FSM transitions
    migrations/         # Django migrations
    management/commands/ # django-typer commands (see §8)
      lifecycle.py      # Worktree provisioning
      workspace.py      # Workspace operations
      worktree.py       # Per-worktree operations (provision/start/...)
      db.py             # Database operations
      env.py            # `env set|show|check` for the env cache
      run.py            # Service runner
      followup.py       # PR sync (GitHub + GitLab via CodeHostBackend)
      pr.py             # PR creation and validation
      ticket.py         # Ticket transitions, queries, and tracker comments
      tool.py           # Overlay-declared tool subcommands
      e2e.py            # `e2e run|external|project`
      loop_tick.py      # One tick of the fat loop (scan, dispatch, statusline)
      overlay.py        # Overlay inspection (config, info)
      tasks.py          # Task claiming and execution
      generate_*.py     # generate_all_docs / generate_overlay_docs / generate_skill_docs

  agents/               # Headless executor runtime
    handover.py         # Session handover between runtimes (uses TEATREE_CLAUDE_STATUSLINE_STATE_DIR)
    headless.py         # Headless execution via `claude -p` (kept slim — future SDK swap point)
    prompt.py           # System context and task prompt builders
    skill_bundle.py     # Skill dependency resolution for agent launch
    result_schema.py    # JSON schema for structured agent output

  loop/                 # /loop topology (see §5.6)
    tick.py             # One tick: scan in parallel, dispatch to phase agents when needed, render statusline
    dispatch.py         # Signal → action mapping (statusline / agent / webhook)
    rendering.py        # Thin orchestrator: zones_for() + availability anchor. Delegates to rendering_classification / rendering_zones; re-exports the public names (#1058).
    rendering_classification.py  # Signal → typed ref, per-overlay bucketing, cross-scanner dedup (_classify_actions, _ClassifiedActions)
    rendering_zones.py  # Per-zone line builders: anchor / action / in-flight rows (_populate_overlay_zones, _render_action_line, _render_pr_group). Ready zone inlines `(!iid)` after each ticket whose parent MR is known.
    pr_ticket_index.py  # Build mr_url → parent_ticket_number index (PullRequest FK + Closes/Fixes regex)
    statusline.py       # Statusline composition (zones, formatters) and file write
    scanners/           # Pure-Python signal collectors — one file each
      active_tickets.py
      assigned_issues.py
      base.py           # Scanner Protocol + ScanSignal dataclass
      my_prs.py
      notion_view.py
      pending_tasks.py
      reviewer_prs.py
      slack_mentions.py
      ticket_completion.py
      ticket_dispositions.py

  backends/             # Pluggable external service integrations
    protocols.py        # Protocol classes (see §7)
    loader.py           # Per-overlay backend loader (code-host + messaging) with lru_cache
    types.py            # Shared API TypedDicts (RawAPIDict, etc.)
    github.py           # GitHub API client + GitHubCodeHost (implements CodeHostBackend)
    github_sync.py      # GitHubSyncBackend — consumes CodeHostBackend
    gitlab.py           # GitLab API client + GitLabCodeHost (translates MR ↔ PR)
    gitlab_api.py       # Low-level GitLab REST helpers
    gitlab_ci.py        # GitLabCIService — implements CIService
    gitlab_sync.py      # GitLabSyncBackend orchestrator — thin, delegates to modules below
    gitlab_sync_prs.py  # PR building, upserting, discussion parsing
    gitlab_sync_issues.py  # Issue fetching, label resolution, variant extraction
    gitlab_sync_approvals.py / gitlab_sync_terminal.py  # Approval and terminal-state sync helpers
    slack.py            # Slack API client (httpx wrapper for SlackBotBackend)
    slack_bot.py        # SlackBotBackend — Socket Mode messaging client (implements MessagingBackend)
    slack_receiver.py   # Socket Mode receiver — writes inbound events to JSONL queue (t3 slack listen)
    slack_reactions.py  # Reaction helpers used by transition signals
    slack_review_sync.py # Review-thread → Slack post sync
    messaging_noop.py   # NoopMessagingBackend — default for overlays that opt out
    notion.py           # Notion read-only client (page fetch + n8n webhook trigger)
    sentry.py           # Sentry error tracking

  utils/                # Pure utility modules
    git.py, ports.py, run.py, secrets.py, db.py, django_db.py,
    bad_artifacts.py, compose_contract.py, dep_drift.py, redis_container.py

  overlay_init/         # t3 startoverlay helpers
    generator.py        # Scaffold generation logic (called from cli/)

  contrib/              # First-party overlays shipped with the package
    t3_teatree/         # Teatree's own overlay (dogfood)

  docker/               # Shared docker base-image build helpers (see §6.2a)
  templates/overlay/    # Cookiecutter-style scaffold for `t3 startoverlay`

.claude-plugin/         # Plugin manifest
  plugin.json           # Plugin identity (name: t3)
agents/                 # Phase sub-agent definitions (orchestrator + 7 phase agents — see §11.2)
skills/*/               # Workflow skills (SKILL.md + references/)
hooks/                  # Plugin hooks
  hooks.json            # Event → script mapping
  scripts/              # Hook scripts (bootstrap, skill loading, statusline `cat`)
apm.yml                 # APM package manifest
settings.json           # Plugin settings (statusline + permissions allow/deny)
tests/                  # Pytest suite (>90% branch coverage)
scripts/                # Standalone utility scripts
```

---
