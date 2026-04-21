---
name: setup
description: Bootstrap and validate teatree for local use — prerequisites, config, skill symlinks, optional agent hooks, and Django project scaffolding. Use when user says "setup skills", "install skills", "bootstrap skills", or needs first-time teatree installation.
compatibility: macOS/Linux, git, python3.13+, uv.
triggers:
  priority: 80
  keywords:
    - '\b(setup skills|configure claude|install skills|bootstrap skills|configure hooks)\b'
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
2. Create or validate `~/.teatree`.
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

## Step 2: Create `~/.teatree`

Create or update `~/.teatree` as a simple `KEY=VALUE` shell file.

Required values:

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
| `T3_AUTO_SHIP` | Create shipping tasks as headless instead of interactive. When `false`, the pipeline pauses at shipping for user approval before push. | `false` |
| `T3_UPSTREAM` | Upstream repo for PRs (empty = PR on origin, set = PR on upstream) | empty |
| `T3_PRIVATE_TESTS` | Private QA repo path | empty |
| `T3_BRANCH_PREFIX` | Branch prefix for generated worktrees | derived from git user |
| `T3_ISSUE_TRACKER` | `gitlab` or `github` | detected |
| `T3_CHAT_PLATFORM` | `slack`, `teams`, or `none` | `none` |
| `T3_SKILL_OWNERSHIP_FILE` | Ownership config for skill editing | `$HOME/.ac-reviewing-codebase` |

Do not require `T3_OVERLAY`. The active overlay is discovered via entry points.

Example:

```bash
cat > ~/.teatree <<'EOF'
T3_REPO="$HOME/workspace/teatree"
T3_WORKSPACE_DIR="$HOME/workspace"
T3_ISSUE_TRACKER="gitlab"
T3_CHAT_PLATFORM="none"
T3_CONTRIBUTE=false
T3_PUSH=false
T3_AUTO_PUSH_FORK=false
T3_AUTO_SHIP=false
T3_UPSTREAM=""
T3_PRIVATE_TESTS=""
T3_BRANCH_PREFIX="ac"
T3_SKILL_OWNERSHIP_FILE="$HOME/.ac-reviewing-codebase"
EOF
```

## Step 3: Install Skills

`t3 setup` creates symlinks from each supported agent runtime's skills
directory to the teatree skills.  Claude is always targeted (the directory is
created if missing); other runtimes listed in
`teatree.cli.setup.AGENT_SKILL_RUNTIMES` are targeted only when their home
directory already exists.  Contributor-mode symlinks that point inside the
configured `workspace_dir` are preserved.

Inspect the result with `t3 info` — the "Skills installed to" section lists
every runtime dir teatree detected along with the count of managed symlinks.

## Step 4: Claude Code Plugin Hooks

Teatree ships a `hooks.json` that Claude Code loads automatically when the plugin is installed. All hooks route through `hook_router.py`, a unified Python router that handles event dispatch.

If the user installed via `apm install -g souliane/teatree`, hooks are already configured. For manual installs or troubleshooting, verify the plugin is registered in `~/.claude/plugins.json`.

The hooks cover these events:

| Event | Matcher | Purpose |
| --- | --- | --- |
| `SessionStart` | *(none)* | Bootstrap CLI availability |
| `UserPromptSubmit` | *(none)* | Detect intent from prompt keywords and suggest skills to load |
| `PreToolUse` | `Bash\|Edit\|Write` | Branch protection, skill enforcement |
| `PostToolUse` | `Bash\|Write\|Edit\|Read\|Grep\|Glob` | Track which repos are touched |
| `PostToolUse` | `Skill` | Track loaded skills |
| `InstructionsLoaded` | *(none)* | Track loaded skills (belt-and-suspenders fallback) |
| `PostCompact` | *(none)* | Restore state after context compaction |
| `SessionEnd` | *(none)* | Session cleanup |

All hook scripts live in `$T3_REPO/hooks/scripts/`. The `hooks.json` at `$T3_REPO/hooks/hooks.json` defines the routing table.

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
  --with django-htmx \
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
