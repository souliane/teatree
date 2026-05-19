# BLUEPRINT Appendix — Skills, Plugin Architecture, Testing, Quality Gates

Detail behind [BLUEPRINT.md](https://github.com/souliane/teatree/blob/main/BLUEPRINT.md) §11–§13. Consumer cross-references such as `BLUEPRINT §11.4` (Bash permissions, classifier protocol) resolve here.

## 11. Skills & Plugin Architecture

### 11.1 Skills

Skills live in `skills/*/`. Each skill is a `SKILL.md` file with optional `references/` directory. When installed as a plugin, skills are namespaced under `t3:` (e.g., `/t3:code`).

**Skills drive the development work — coding methodology, debugging process, review standards, retro learning. The CLI handles infrastructure (worktrees, databases, ports, CI).**

| Skill | Purpose |
|-------|---------|
| `code` | TDD methodology, coding guidelines |
| `contribute` | Push improvements to fork, open upstream issues |
| `debug` | Troubleshooting and fixing |
| `followup` | Daily follow-up, batch tickets, PR reminders |
| `handover` | Transfer in-flight tasks to another runtime |
| `next` | Session wrap-up: retro, structured result, pipeline handoff |
| `platforms` | Platform-specific API recipes (GitLab, GitHub, Slack) |
| `retro` | Conversation retrospective and skill improvement |
| `review` | Code review (self, giving, receiving) |
| `review-request` | Batch review requests |
| `rules` | Cross-cutting agent safety rules |
| `setup` | Bootstrap and validate teatree for local use |
| `ship` | Committing, pushing, PR creation, pipeline |
| `test` | Testing, QA, CI |
| `ticket` | Ticket intake and kickoff |
| `workspace` | Worktree creation, setup, servers, cleanup |

Skills declare dependencies via `requires:` in YAML frontmatter. The skill bundle resolver performs topological sort for correct load order. Skills can also declare `companions:` — optional dependencies that are included when available but only warn (not fail) when missing.

#### Third-Party Skill Integration

Teatree integrates with third-party skill frameworks (notably [superpowers](https://github.com/obra/superpowers)) via the `companions:` mechanism and APM dependency management. The approach is:

- **Absorb, don't delegate.** When a third-party skill covers a universal concern (skill-loading discipline, verification before completion), the best content is distilled into teatree's own `rules` skill — which is always loaded via `requires:`. This avoids context waste from loading both teatree and third-party versions of the same guidance.
- **Companion for domain skills.** Third-party skills that cover specific domains (TDD methodology, plan execution, brainstorming) are declared as `companions:` on the relevant lifecycle skill. They load alongside teatree skills when installed, adding depth without duplication.
- **Exclude conflicting skills.** Skills that duplicate teatree's core infrastructure (worktree management, skill loading) are excluded during `t3 setup` via `CORE_EXCLUDED_SKILLS`. This prevents conflicting instructions — teatree's `t3 workspace` subsystem replaces generic worktree skills entirely.

Attribution: the `rules` skill's "Invoke Skills Before ANY Response" and "Verification Before Completion" sections are adapted from superpowers' `using-superpowers` and `verification-before-completion` skills respectively.

### 11.2 Sub-Agent Architecture

Eight phase agents live in `agents/` (the plugin directory, shipped as part of the local clone, loaded via symlink). Each is a thin YAML+description wrapper that references skills via `skills:` frontmatter — no content duplication. Phase agents are invoked via the standard Task tool by lifecycle skills, by the headless executor (§ 5.2) when a phase task is claimed, and by the loop tick (§ 5.6) when a scanner signal calls for agent judgment.

| Agent | Skills | Role |
|-------|--------|------|
| `orchestrator` | rules, workspace | Routes phase tasks to specific agents |
| `coder` | rules, workspace, code | Implements features with TDD |
| `tester` | rules, workspace, test, platforms | Runs tests, analyzes CI |
| `e2e` | rules, workspace, test, e2e, platforms | Playwright E2E tests and visual QA |
| `reviewer` | rules, platforms, review, code | Read-only code review |
| `shipper` | rules, workspace, platforms, ship, review-request | Delivery workflow |
| `debugger` | rules, workspace, debug | Troubleshooting and fixes |
| `followup` | rules, platforms, followup | PR/issue sync and reminders |

The loop ships no additional agents — its scanners (§ 5.6) are pure Python, and its dispatch stage delegates to these same eight agents. This keeps the agent surface small enough to audit and works identically whether teatree is installed editable or via `uv tool install`.

Interactive-only skills (no agent): `retro`, `next`, `contribute`, `setup`.

### 11.3 Distribution

Two install paths, one source of truth:

- **APM**: `apm install souliane/teatree` — deploys to any supported agent
- **CLI-first**: `git clone … && uv tool install --editable . && t3 setup` — bootstraps from a local clone (runs APM install, syncs skill symlinks, and creates the plugin symlink `~/.claude/plugins/t3 → <clone>` in one step). `t3 setup` also auto-runs `uv tool install --editable <repo>` when the global `t3` binary is missing, so `uv run t3 setup` from a fresh checkout upgrades itself in-place. On every run it additionally reads `[project].dependencies` from the resolved main clone, compares against `importlib.metadata.distributions()`, and — when an editable install is missing a declared dep — re-runs `uv tool install --editable <source> --reinstall` and `execv`-restarts itself against the refreshed venv (see `teatree.utils.dep_drift` and `cli/setup.py:_repair_dep_drift`). Closes the catch-22 where adding a new top-level teatree dep used to break every existing editable install until the user manually reinstalled.

The agent-facing hook layer (`hooks/scripts/hook_router.py`) blocks `uv run t3` Bash invocations and directs agents to call the globally installed `t3` instead.

`UserPromptSubmit` skill detection (`scripts/lib/skill_loader.py`) enriches the prompt with linked PR/issue titles before keyword matching via `teatree.url_title_fetcher`. This lets a domain skill auto-load when the prompt contains only a bare PR URL whose *title* — not its URL — carries the trigger keyword. Titles are fetched in parallel via `glab`/`gh` (1.5s per fetch, 4.0s total budget) and cached at `~/.cache/teatree/url-titles.json`. Disable with `T3_HOOK_FETCH_TITLES=0`.

### 11.4 Bash Permissions

The plugin's `settings.json` ships a **comprehensive** `permissions.allow` list so every command teatree and its overlays legitimately invoke matches a static rule — the auto-mode classifier is never consulted for normal workflow. This keeps day-to-day work friction-free: no surprise prompts, no classifier false-denials on routine operations.

The design is **broad allow, narrow deny**:

- **Allow** — every tool family the workflow touches:
  - **Core t3 / Python / packaging:** `t3`, `uv`, `uvx`, `pip`, `pipenv`, `python`, `python3`, `pytest`, `ruff`, `mypy`, `ty`, `prek`, `pre-commit`, `make`, `black`, `isort`, `flake8`.
  - **Git & hosting:** `git`, `gh`, `glab`.
  - **Node / frontend:** `node`, `npm`, `npx`, `yarn`, `pnpm`, `nx`, `ng`.
  - **Infra:** `docker`, `docker compose`, `docker-compose`, `docker exec`, `psql`, `createdb`, `dropdb`, `pg_dump`, `pg_restore`, `pg_isready`, `redis-cli`.
  - **POSIX utilities & file ops:** `ls`, `cat`, `head`, `tail`, `grep`, `rg`, `find`, `sed`, `awk`, `jq`, `yq`, `xargs`, `wc`, `tree`, `file`, `which`, `env`, `cp`, `mv`, `ln`, `mkdir`, `rmdir`, `touch`, `chmod`, `chown`, `tar`, `gzip`, `zip`, `rm`, plus `readlink`, `realpath`, `basename`, `dirname`, `cut`, `sort`, `uniq`, `diff`, `date`, `df`, `du`, `tee`, `mktemp`.
  - **Network & process:** `curl`, `wget`, `ps`, `pkill`, `kill`, `lsof`, `nc`, `fuser`.
  - **Platform:** `launchctl`, `systemctl`, `brew`, `open`, `osascript`, `pass show`.
- **Deny** — the load-bearing non-negotiables that take precedence over any allow wildcard:
  - `git push` to default branches (`main`/`master`/`development`/`develop`/`release`/`trunk`)
  - `git push --force` / `-f` / `--force-with-lease` (any branch)
  - `--no-verify` on any git command
  - `git config --global` / `--system`, `git filter-branch`, `git update-ref -d`
  - `gh/glab repo delete`, `release delete`, `gist delete`, `auth logout`
  - `curl/wget | bash/sh`
  - `rm -rf` rooted at `/`, `~`, `$HOME`, `.`, `..`

**Why this shape.** The `t3` CLI is the workflow's safety wrapper — it enforces worktree isolation, branch naming, ticket gates, and push gates. Blocking commands inside the CLI is the wrong layer; we allow tool families broadly and let `t3` decide which invocations are legitimate. The classifier stays available for novel patterns that neither list covers, but in the common case a teatree session runs end-to-end without a single classifier prompt.

**Users still get the final say.** A user's own `~/.claude/settings.json` (or equivalent) can expand this further or tighten it — nothing in the plugin prevents an individual from locking down their environment. To make a session friction-free without the plugin ever shipping a classifier whitelist, teatree documents a generic, parameterized **recommended** auto-mode set and detects (read-only — never applies) which entries are absent: `t3 doctor authorizations` (also surfaced by `t3 doctor check` and `t3 setup`) prints the paste-ready sentence for each missing rule. The set lives in `teatree.cli.recommended_authorizations` and `skills/setup/references/recommended-automode-authorizations.md`; user-specific items (hosts, creds, paths) are deliberately the user's to add.

The full config surface for instance-specific agent behaviour — operating mode (`[teatree] mode` vs. `[overlays.<name>] mode` vs. `T3_MODE`), the `auto`-mode training wheels, how overlays declare their MCP/messaging integration via `OverlayConfig`, and why no `mcp__*` / `defaultMode` block ships in the plugin — is consolidated, with each knob mapped to its owning module, in `skills/setup/references/agent-mode-and-mcp-config.md`.

**Plugin config is not self-modifiable by the agent.** Claude Code's autonomy guardrail rejects edits to the plugin's `settings.json` allow-list — and to standing pre-authorization clauses in `CLAUDE.md` — as "Self-Modification / classifier bypass". This is by design: an agent that can grant itself standing high-impact permissions (e.g. `Bash(gh pr merge:*)`, "merge auth carries through") would defeat the purpose of the classifier. When per-call confirmation on `gh pr merge` / `gh pr update-branch` is too noisy for a session, the right knob is the **user's own** `~/.claude/settings.json` (user-scoped, not plugin-scoped) — or a single compound bash invocation that bundles the status check and merge into one intent.

**Classifier denial = immediate session blocker.** When the classifier denies a tool call mid-workflow (Bash rejected, MCP call refused, etc.), the agent must stop, inform the user, and use `AskUserQuestion` to ask whether to relax the classifier or proceed differently. If the user opts to relax, the agent **attempts the edit to `~/.claude/settings.json` itself** (zero manual steps for the user); only if the harness self-modification guardrail blocks the write does the agent fall back to a paste-ready snippet for the user to apply. Silent workarounds (alternate command shape, alternate tool, decomposed invocations) are forbidden. The full agent-facing protocol lives in `skills/rules/SKILL.md` § "Classifier Denial Protocol (Non-Negotiable)" — that section is the canonical source; this paragraph is just a pointer. Teatree defines the protocol but never modifies the user's classifier permissivity itself.

---

## 12. Testing

### 12.1 Coverage Gate

**>90% branch coverage, non-negotiable.** Enforced by pytest-cov with `fail_under = 93, branch = true`. Omits only migrations.

### 12.2 Django Test Settings

- In-memory SQLite (`:memory:`) for isolation and speed
- `django_tasks.backends.immediate` for synchronous task execution

### 12.3 Test Isolation

- `conftest.py` monkeypatches `HOME`, `XDG_CACHE_HOME`, `XDG_CONFIG_HOME`, `XDG_DATA_HOME` to `tmp_path`
- `_strip_git_hook_env()` removes `GIT_*` env vars to prevent index corruption
- Auto-use fixtures: `_clean_registry` (admin), `_no_system_port_checks`, `_isolate_env`
- `reset_overlay_cache()` and `reset_backend_caches()` prevent cross-test contamination

### 12.4 Test Organization

```
tests/
  conftest.py             # XDG isolation, port stubs, overlay env strip, immediate task backend
  django_settings.py      # In-memory SQLite test settings
  assets/                 # Test fixtures (sample dumps, snapshots, ...)
  teatree_core/           # Core model, transition, manager, runner, selector tests
  teatree_agents/         # Agent execution + prompt + skill bundle tests
  teatree_backends/       # Backend integration tests (GitHub, GitLab, Slack, Notion)
  teatree_loop/           # Loop tick + scanner + dispatch + statusline tests
  test_config.py / test_cli_*.py / test_skill_*.py / test_*_hook.py / ... — top-level
                          # cross-cutting suites for config, CLI, hooks, schemas, contrib
```

`pytest -k <pattern>` is the usual filter; `pytest tests/teatree_loop/` runs the loop suite alone.

### 12.5 E2E Tests

Core has no Playwright suite — there is no UI to E2E-test. Overlays may declare their own Playwright suites via `get_e2e_config()` (typically pointing at the application's own UI), and `t3 <overlay> e2e {run,external,project}` runs them.

---

## 13. Quality Gates

| Tool | What it checks | Config |
|------|----------------|--------|
| pytest + pytest-cov | >90% branch coverage (`fail_under = 93`) | `pyproject.toml [tool.coverage]` |
| ruff | ALL rules enabled, specific ignores justified | `pyproject.toml [tool.ruff]` |
| ty | Static type checker with `error-on-warning = true` | `pyproject.toml [tool.ty]` |
| import-linter | Dependency boundaries | `pyproject.toml [tool.importlinter]` |
| codespell | Spell check | `pyproject.toml [tool.codespell]` |
| prek | Runs all above on commit | `.pre-commit-config.yaml` |

**Key ruff decisions:**

- ALL rules selected, then specific ignores with justification
- D1xx disabled (no docstrings — self-documenting code)
- `from __future__ import annotations` banned (use native 3.13 syntax)
- Per-file ignores for tests, scripts, management commands, migrations, views, overlay
