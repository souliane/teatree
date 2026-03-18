# Project Overlay Templates

> Templates used by `t3-setup § Scaffold a Project Overlay`. Referenced from the setup skill.

---

## Questions

Ask **one question at a time** — show the default in brackets, wait for the answer, then move to the next. Auto-detect and pre-fill as many defaults as possible to minimize questions the user must actually answer.

1. **Project name** — e.g., `acme`. Used for directory name (`acme-overlay`) and skill name (`ac-acme`).
   > "What's your project name?" [default: basename of `$T3_WORKSPACE_DIR`]

2. **Overlay location** — where to create the overlay directory.
   > "Where should I create the overlay?" [default: `$T3_WORKSPACE_DIR/<project>-overlay/`]

3. **Repository names** — comma-separated list of repos the project uses.
   > "Which repos does your project use? (comma-separated)" [e.g., `acme-backend,acme-frontend`]

4. **Backend framework** — `django`, `rails`, `express`, `fastapi`, or `none`.
   > "What's your backend framework?" [default: auto-detect from repos — `manage.py` → `django`, `Gemfile` + `config/routes.rb` → `rails`, `package.json` with express → `express`, `requirements.txt` with fastapi → `fastapi`]

5. **Frontend framework** — `react`, `angular`, `vue`, or `none`.
   > "What's your frontend framework?" [default: auto-detect from `package.json` — `@angular/core` → `angular`, `react` → `react`, `vue` → `vue`]

6. **Database** — `postgres`, `mysql`, `sqlite`, `mongodb`, or `none`.
   > "Which database?" [default: `postgres`]

7. **Docker services** — multi-select from: `postgres`, `redis`, `elasticsearch`, `rabbitmq`, `custom`.
   > "Which Docker services do you need? (comma-separated)" [default: derived from database + framework]

8. **Remote database?** — yes/no. If yes, ask for host and credentials source (e.g., `pass`, env var, vault).
   > "Do you use a remote dev database?" [default: `no`]

9. **CI platform** — `gitlab-ci`, `github-actions`, `circleci`, or `none`.
   > "Which CI platform?" [default: auto-detect from `git remote -v` — `gitlab.com` → `gitlab-ci`, `github.com` → `github-actions`]

10. **Multi-tenant?** — yes/no. If yes, ask for variant detection method and example tenant names.
    > "Is your project multi-tenant?" [default: `no`]
    > If yes: "How do you detect the tenant? (env var, subdomain, config file)" and "Example tenant names? (comma-separated)"

11. **Issue tracker** — `gitlab`, `github`, `jira`, `linear`, or `none`.
    > "Which issue tracker?" [default: same as CI auto-detection]

## Generated Files

Create this directory structure using the answers:

```text
<project>-overlay/
├── SKILL.md
├── scripts/
│   └── lib/
│       ├── bootstrap.sh
│       ├── shell_helpers.sh
│       └── project_hooks.py
├── hook-config/
│   ├── context-match.yml
│   └── reference-injections.yml
└── references/
    ├── prerequisites-and-setup.md
    └── troubleshooting.md
```

### Template: `SKILL.md`

```markdown
---
name: ac-<project>
description: <project> workspace playbook for <repo-list>. Use when user works in a <project> directory, mentions a <project> ticket, or says "start session", "run backend/frontend/tests", "create MR", "push", or any <project> task.
compatibility: macOS/Linux, zsh or bash, git, docker with compose, <framework-specific-tools>, uv, python3, jq.
metadata:
  version: 0.0.1
---

# <Project> Workspace Playbook

## Overview

- Use this skill for any <project> task spanning one or more repositories under `$T3_WORKSPACE_DIR`.
- Load global rules first, then load every repository-specific reference relevant to the task.

## Dependencies (load these when ac-<project> is activated)

- **Lifecycle skills** (required) — the teatree lifecycle skills provide phase-specific workflows. Load the appropriate one for your current task:
  - `/t3-workspace` — worktree creation, setup, DB provisioning, dev servers, cleanup
  - `/t3-ticket` — ticket intake and kickoff
  - `/t3-code` — implementing features
  - `/t3-debug` — troubleshooting and fixing
  - `/t3-test` — running tests, CI interaction, quality checks
  - `/t3-review` — code review (self-review, giving, or receiving)
  - `/t3-review-request` — batch review requests
  - `/t3-ship` — committing, pushing, creating MR, pipeline
  - `/t3-followup` — batch process tickets, status checks, MR reminders
<!-- If backend framework is django: -->
- **ac-django** (required for backend work) — Django best practices. Load when backend code is in scope.

## Loading Order

1. Load the appropriate lifecycle skill for your current phase (e.g., `/t3-ticket`, `/t3-code`, `/t3-ship`)
2. `/ac-<project>` (project overrides + helpers — this skill)

Shell sourcing:

\`\`\`bash
source "$T3_REPO/scripts/lib/bootstrap.sh"
source "$T3_OVERLAY/scripts/lib/bootstrap.sh"
\`\`\`

## Repos

| Repo | Purpose |
|------|---------|
<!-- One row per repo from question 3 -->
| `<backend-repo>` | Backend (<backend-framework>) |
| `<frontend-repo>` | Frontend (<frontend-framework>) |

## Project Rules

<!-- Placeholder — user fills in project-specific conventions -->
- Branch naming: `<prefix>/<ticket>-<slug>`
- Commit message format: conventional commits
- MR template: default
```

### Template: `scripts/lib/bootstrap.sh`

```bash
#!/usr/bin/env bash
# bootstrap.sh — <Project> shell wrappers that delegate to Python
#
# Source this file from .zshrc AFTER teatree:
#   source "$T3_REPO/scripts/lib/bootstrap.sh"
#   source "$T3_OVERLAY/scripts/lib/bootstrap.sh"
#
# Works in both bash and zsh.

export _<PROJECT>_SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-${(%):-%x}}")/.." && pwd)"

# Guard: teatree must be sourced first
if [[ -z "${_T3_SCRIPTS_DIR:-}" ]]; then
  echo "ERROR: source teatree/lib/bootstrap.sh before <project>/lib/bootstrap.sh" >&2
  return 1 2>/dev/null || exit 1
fi

# Source shell helpers that must eval in caller's shell
source "$_<PROJECT>_SCRIPTS_DIR/lib/shell_helpers.sh"

# Python runner — PYTHONPATH includes both project and teatree scripts
function _<project>_py {
  local _py="python3"
  # Prefer project venv if available
  local _venv="$T3_WORKSPACE_DIR/<backend-repo>/.venv/bin/python"
  [[ -x "$_venv" ]] && _py="$_venv"
  PYTHONPATH="$_<PROJECT>_SCRIPTS_DIR:$_T3_SCRIPTS_DIR" "$_py" "$_<PROJECT>_SCRIPTS_DIR/$1" "${@:2}"
}

# ============================================================
# Convenience wrapper: re-source + start dev session
# ============================================================

function <project>_start {
  source "$_T3_SCRIPTS_DIR/lib/bootstrap.sh"
  source "$_<PROJECT>_SCRIPTS_DIR/lib/bootstrap.sh"

  # Auto-run setup if .env.worktree is missing (fresh worktree)
  if [[ ! -f .env.worktree && ! -f ../.env.worktree ]]; then
    echo "No .env.worktree found — running t3 lifecycle setup first..."
    t3 lifecycle setup "$@" || return $?
  fi
  t3 lifecycle start "$@"
}

# NOTE: Individual commands (run backend, run frontend, etc.) are registered
# via create_cli_group() in lib/project_hooks.py — no shell wrappers needed.
# Extension points (wt_run_backend, wt_run_frontend, etc.) are registered
# via register_<project>() in lib/<project>_hooks.py.
```

Replace `<PROJECT>` with the uppercase project name (e.g., `ACME`) and `<project>` with the lowercase name throughout.

### Template: `scripts/lib/shell_helpers.sh`

```bash
#!/usr/bin/env bash
# shell_helpers.sh — Shell helpers that must eval in the caller's shell
#
# Sourced by bootstrap.sh AFTER teatree (which provides _detect_ticket_dir
# and _source_env_file).

# Ensure worktree env vars are loaded.
function _ensure_env {
  local _td=""
  _td="$(_detect_ticket_dir 2>/dev/null || true)"

  local _repo_dir=""
  if [[ -n "$_td" && -d "$_td/<backend-repo>" ]]; then
    _repo_dir="$_td/<backend-repo>"
  else
    _repo_dir="${T3_WORKSPACE_DIR:-$HOME/workspace}/<backend-repo>"
  fi

  # Source env files in standard order
  local _env_file
  for _env_file in "$_repo_dir/.env.example" "$_repo_dir/.env" "$_repo_dir/.env.local"; do
    _source_env_file "$_env_file"
  done

  # Source .env.worktree (ticket-level first, then repo-level)
  if [[ -n "$_td" && -f "$_td/.env.worktree" ]]; then
    _source_env_file "$_td/.env.worktree"
  elif [[ -f "$_repo_dir/.env.worktree" ]]; then
    _source_env_file "$_repo_dir/.env.worktree"
  fi

  # Multi-tenant: mirror WT_VARIANT if set
  # (uncomment if multi-tenant)
  # if [[ -n "${WT_VARIANT:-}" ]]; then
  #   _source_env_file "$_repo_dir/.env.local.$WT_VARIANT"
  # fi
}
```

### Template: `scripts/lib/project_hooks.py`

```python
"""Project hooks entry point — auto-discovered by teatree's init module.

Convention: project overlays provide ``lib/project_hooks.py`` with a
``register()`` function.  Teatree's ``lib.init.init()`` imports this
module and calls ``register()`` to install project-layer overrides.
"""

from lib.registry import register


def register_<project>() -> None:
    """Register <project>-specific extension point overrides."""

    # --- wt_env_extra: append project-specific env vars to .env.worktree ---
    def wt_env_extra(envfile: str) -> None:
        # Example: append DATABASE_URL, REDIS_URL, etc.
        pass

    register("wt_env_extra", wt_env_extra, "project")

    # --- wt_services: start Docker services ---
    # def wt_services(main_repo: str, wt_dir: str = "") -> None:
    #     import subprocess
    #     subprocess.run(["docker", "compose", "-f", f"{main_repo}/docker-compose.yml",
    #                     "up", "-d", "--no-build"], check=False)
    # register("wt_services", wt_services, "project")

    # --- wt_db_import: restore a database dump ---
    # def wt_db_import(db_name: str, variant: str, main_repo: str) -> bool:
    #     return False  # implement when you have a dump workflow
    # register("wt_db_import", wt_db_import, "project")

    # --- wt_run_backend: start the backend dev server ---
    # def wt_run_backend(*args: str) -> None:
    #     import subprocess
    #     subprocess.run(["python", "manage.py", "runserver"], check=False)  # django
    # register("wt_run_backend", wt_run_backend, "project")

    # --- wt_run_frontend: start the frontend dev server ---
    # def wt_run_frontend(*args: str) -> None:
    #     import subprocess
    #     subprocess.run(["npm", "run", "dev"], check=False)
    # register("wt_run_frontend", wt_run_frontend, "project")

    # --- wt_start_session: full dev session entrypoint ---
    # def wt_start_session(*args: str) -> int:
    #     # Self-heal: run t3 lifecycle setup if needed, then start everything
    #     return 0
    # register("wt_start_session", wt_start_session, "project")


def register() -> None:
    register_<project>()
```

Replace `<project>` with the actual project name.

### Template: `hook-config/context-match.yml`

```yaml
# Patterns that trigger this project overlay skill.
# The ensure-skills-loaded hook scans all skills for this file and matches
# patterns against $PWD and the active-repo tracker file.
#
# Any pattern that appears as a substring of the path triggers the overlay.
cwd_patterns:
  - "<backend-repo>"
  - "<frontend-repo>"
  # Add more repo name patterns as needed

# Framework skills to co-suggest when PWD or active repo matches specific
# patterns. Each key is a skill name; its list contains substring patterns.
# The hook adds these to the suggestion list alongside the phase skill.
companion_skills:
  # Uncomment and adjust based on your stack:
  #
  # --- Language & framework skills (souliane/skills) ---
  #
  # ac-python:                              # Python guidelines (style, typing, testing)
  #   - "<backend-repo>"
  # ac-django:                              # Django bible (models, DRF, migrations)
  #   - "<backend-repo>"
  # ac-adopting-ruff:                       # Progressive ruff adoption
  #   - "<backend-repo>"
  #   - "<frontend-repo>"
  #
  # --- Tooling skills (souliane/skills) ---
  #
  # ac-managing-repos:                      # Cross-repo infrastructure audit
  #   - "<skills-repo>"
  # ac-reviewing-skills:                    # Skill quality review
  #   - "<skills-repo>"
  #
  # --- Third-party framework skills ---
  #
  # angular-skills:                         # analogjs/angular-skills
  #   - "<frontend-repo>"
  # fastapi-expert:                         # Jeffallan/claude-skills
  #   - "<backend-repo>"
```

Generate the `cwd_patterns` list from the repo names (question 3). Generate `companion_skills` based on the stack (questions 4-5):

- **Django backend**: add `ac-django` + `ac-python` mapped to backend repo patterns (`souliane/skills`)
- **FastAPI backend**: add `fastapi-expert` mapped to backend repo patterns (`Jeffallan/claude-skills`) + optionally `ac-python`
- **Other Python backend**: add `ac-python` mapped to backend repo patterns
- **Angular frontend**: add `angular-skills` mapped to frontend repo patterns (`analogjs/angular-skills`)
- **Any Python project**: add `ac-adopting-ruff` mapped to all Python repo patterns (for ruff migration)
- **Skills repo**: add `ac-managing-repos` + `ac-reviewing-skills` mapped to the skills repo pattern

### Template: `hook-config/reference-injections.yml`

```yaml
# <Project> reference injections per lifecycle skill.
# The ensure-skills-loaded hook reads this file and tells Claude which
# references to load alongside each generic skill.
#
# "always" references are loaded every time the skill activates.
# "on-demand" references are loaded conditionally.

t3-workspace:
  always:
    - references/prerequisites-and-setup.md

t3-ticket:
  always:
    - references/prerequisites-and-setup.md

t3-code:
  always:
    - references/prerequisites-and-setup.md
  on-demand:
    - references/troubleshooting.md

t3-debug:
  always:
    - references/troubleshooting.md

t3-test:
  always:
    - references/prerequisites-and-setup.md

t3-ship:
  always:
    - references/prerequisites-and-setup.md
```

### Template: `references/prerequisites-and-setup.md`

```markdown
# <Project> — Prerequisites & Setup

## Required tools

<!-- List tools specific to your project stack -->
- Git
- Docker + Docker Compose
- Python 3.12+ / Node.js (as applicable)

## First-time setup

1. Clone all repos into `$T3_WORKSPACE_DIR`:
   ```bash
   cd $T3_WORKSPACE_DIR
   git clone <backend-repo-url>
   git clone <frontend-repo-url>
   ```

2. Set up the overlay:

   ```bash
   source ~/.teatree
   source "$T3_REPO/scripts/lib/bootstrap.sh"
   source "$T3_OVERLAY/scripts/lib/bootstrap.sh"
   ```

3. Start a dev session:

   ```bash
   <project>_start
   ```

## Environment variables

| Variable | Purpose | Source |
|----------|---------|--------|
<!-- Fill in project-specific env vars -->
```

### Template: `references/troubleshooting.md`

```markdown
# <Project> — Troubleshooting

## Common Issues

### Docker services won't start
- Check `docker compose ps` for error details
- Verify ports aren't already in use: `lsof -i :<port>`

### Database connection refused
- Verify Docker postgres is running: `docker compose ps postgres`
- Check DATABASE_URL in `.env.worktree`

### Frontend build fails
- Clear node_modules and reinstall: `rm -rf node_modules && npm install`
- Check Node.js version matches project requirements

<!-- Add project-specific issues as you encounter them -->
```

## Post-Scaffold Steps

After generating all files, run these steps automatically:

**1. Initialize git repo:**

```bash
cd "$T3_WORKSPACE_DIR/<project>-overlay"
git init
git add -A
git commit -m "Initial overlay scaffold for <project>"
```

**2. Set `T3_OVERLAY` in `~/.teatree`:**

Read the current `~/.teatree`, update or add the `T3_OVERLAY` line:

```bash
# In ~/.teatree:
T3_OVERLAY="$T3_WORKSPACE_DIR/<project>-overlay"
```

**3. Install skill symlinks:**

```bash
"$T3_REPO/scripts/install_skills.sh" "$T3_WORKSPACE_DIR/<project>-overlay"
```

This creates contributor-mode entries in the detected agent runtimes, for example:

- `~/.agents/skills/ac-<project>` → `$T3_WORKSPACE_DIR/<project>-overlay/`
- `~/.claude/skills/ac-<project>` → `$T3_WORKSPACE_DIR/<project>-overlay/`

**4. Verify hook detection:**

Test that `ensure-skills-loaded.sh` discovers the new overlay by simulating a prompt from a project directory:

```bash
cd "$T3_WORKSPACE_DIR/<backend-repo>" 2>/dev/null || cd "$T3_WORKSPACE_DIR"
echo '{"session_id":"test","prompt":"implement the feature"}' | \
  bash "$T3_REPO/integrations/claude-code-statusline/ensure-skills-loaded.sh"
```

Expected output should include `ac-<project>` in the skill suggestion. If it doesn't, verify:

- `hook-config/context-match.yml` exists in the overlay directory
- The `cwd_patterns` contain substrings that match repo directory names
- The symlinks in the detected agent runtime skills directory point to the overlay

**5. Report to user:**

Show the user a summary of what was created:

- Overlay directory path and file count
- `T3_OVERLAY` value in `~/.teatree`
- Symlink paths
- Hook detection result (pass/fail)
- Next steps: "Add project-specific rules to `SKILL.md`, fill in `references/`, and uncomment extension points in `project_hooks.py` as you build out your workflow."
