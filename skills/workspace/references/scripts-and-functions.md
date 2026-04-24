# Scripts and CLI Commands

> Load when you need to find available `t3` commands.

---

## CLI Entry Point

```bash
t3 --help                      # from any project with teatree installed
t3 <overlay> --help            # overlay-specific commands (from overlay project)
```

## Global Commands (no overlay needed)

| Command | What it does |
|---------|-------------|
| `t3 startoverlay` | Scaffold a new overlay package |
| `t3 start-ticket <URL>` | Zero to coding — create ticket, provision worktree, start services |
| `t3 ship <TICKET_ID>` | Code to MR — create merge request |
| `t3 daily` | Daily followup — sync MRs, check gates, remind reviewers |
| `t3 agent` | Launch agent with project context |
| `t3 full-status` | Show ticket, worktree, and session state summary |
| `t3 info` | Show binary, source paths, editable status, and installed overlays |
| `t3 doctor check` | Verify imports and editable-install sanity |
| `t3 config autoload` | List skill auto-loading rules |
| `t3 ci cancel` | Cancel stale CI pipelines |
| `t3 ci divergence` | Check fork divergence from upstream |
| `t3 ci trigger-e2e` | Trigger E2E tests on CI |
| `t3 ci fetch-errors` | Fetch error logs from CI |
| `t3 ci fetch-failed-tests` | Extract failed test IDs from CI |
| `t3 ci quality-check` | Run quality analysis |
| `t3 review-request discover` | Discover open MRs awaiting review |
| `t3 tool privacy-scan` | Scan for privacy-sensitive patterns |
| `t3 tool analyze-video` | Decompose video into frames |
| `t3 tool bump-deps` | Bump pyproject.toml deps from uv.lock |
| `t3 tool sonar-check` | Run local SonarQube analysis via Docker |

## Overlay Commands (`t3 <overlay> ...`)

| Command | What it does |
|---------|-------------|
| `t3 <overlay> dashboard` | Migrate DB + start dashboard server |
| `t3 <overlay> resetdb` | Drop and recreate SQLite DB |
| `t3 <overlay> worker` | Start background task workers |
| `t3 <overlay> worktree provision [VARIANT]` | Provision worktree: ports, env, symlinks, DB |
| `t3 <overlay> worktree start` | Start dev servers, then verify |
| `t3 <overlay> worktree status` | Show worktree state |
| `t3 <overlay> worktree teardown` | Tear down a worktree |
| `t3 <overlay> worktree teardown` | Teardown — stop services, drop DB, clean state |
| `t3 <overlay> worktree diagram` | Print state diagram as Mermaid |
| `t3 <overlay> workspace ticket` | Create ticket workspace with git worktrees |
| `t3 <overlay> workspace finalize` | Squash commits + rebase on default branch |
| `t3 <overlay> workspace clean-all` | Prune merged/gone worktrees |
| `t3 <overlay> run backend` | Start backend dev server |
| `t3 <overlay> run frontend` | Start frontend dev server |
| `t3 <overlay> run build-frontend` | Build frontend app |
| `t3 <overlay> run tests` | Run project tests |
| `t3 <overlay> run verify` | Verify dev services respond via HTTP |
| `t3 <overlay> e2e trigger-ci` | Trigger E2E tests on CI |
| `t3 <overlay> e2e external` | Run Playwright from external test repo |
| `t3 <overlay> e2e project` | Run E2E tests from project's test directory |
| `t3 <overlay> db refresh` | Re-import database from dump/DSLR |
| `t3 <overlay> db restore-ci` | Restore database from CI dump |
| `t3 <overlay> db reset-passwords` | Reset all user passwords |
| `t3 <overlay> pr create` | Create merge request |
| `t3 <overlay> pr check-gates` | Check transition gates for ticket status |
| `t3 <overlay> pr fetch-issue` | Fetch issue context from tracker |
| `t3 <overlay> pr detect-tenant` | Detect tenant variant |
| `t3 <overlay> pr post-evidence` | Post test evidence as MR comment |
| `t3 <overlay> followup sync` | Sync followup data from MRs |
| `t3 <overlay> followup refresh` | Return counts of tickets and tasks |
| `t3 <overlay> followup remind` | Return list of pending tasks |

## Standalone Scripts (`scripts/`)

Scripts that are not part of the CLI — run directly:

| Script | Purpose |
|--------|---------|
| `scripts/privacy_scan.py` | Privacy-sensitive pattern scanner (also available as `t3 tool privacy-scan`) |
| `scripts/analyze_video.py` | Video frame decomposition (also available as `t3 tool analyze-video`) |
| `scripts/bump-pyproject-deps-from-lock-file.py` | Bump deps (also available as `t3 tool bump-deps`) |
| `scripts/check_skill_versions.py` | Sync SKILL.md versions with pyproject.toml (pre-commit hook) |

## Pre-commit Hooks (`scripts/hooks/`)

These run via `prek`, not via the CLI:

- `check-banned-terms.sh` — reject banned terms in public repos
- `check_skill_versions.py` — sync SKILL.md versions with pyproject.toml
- `update_readme_skills.py` — regenerate skill index in README
- `update_dashboard_screenshot.py` — update dashboard screenshot
