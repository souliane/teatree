---
name: t3-setup
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

- The **skill repo** you are in now. It provides the `t3-*` workflow skills, references, hooks, and the `teatree` Django extension package.
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
| `T3_PUSH` | Ask about pushing after retro commits | `false` |
| `T3_UPSTREAM` | Upstream repo for contribution flow | empty |
| `T3_PRIVATE_TESTS` | Private QA repo path | empty |
| `T3_BRANCH_PREFIX` | Branch prefix for generated worktrees | derived from git user |
| `T3_ISSUE_TRACKER` | `gitlab` or `github` | detected |
| `T3_CHAT_PLATFORM` | `slack`, `teams`, or `none` | `none` |
| `T3_SKILL_OWNERSHIP_FILE` | Ownership config for skill editing | `$HOME/.ac-reviewing-skills` |

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
T3_UPSTREAM=""
T3_PRIVATE_TESTS=""
T3_BRANCH_PREFIX="ac"
T3_SKILL_OWNERSHIP_FILE="$HOME/.ac-reviewing-skills"
EOF
```

## Step 3: Install Skills

Create symlinks from each agent runtime's skills directory to the teatree skills:

```bash
for root in "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.cursor/skills" "$HOME/.copilot/skills" "$HOME/.agents/skills"; do
  [ -d "$root" ] || continue
  for skill in "$T3_REPO"/skills/t3-*/; do
    name=$(basename "$skill")
    [ -L "$root/$name" ] && continue  # already symlinked
    ln -s "$skill" "$root/$name"
  done
done
```

Verify that each `t3-*` skill is symlinked into the active runtime skill directory:

```bash
for root in "$HOME/.claude/skills" "$HOME/.codex/skills" "$HOME/.cursor/skills" "$HOME/.copilot/skills" "$HOME/.agents/skills"; do
  [ -d "$root" ] || continue
  find "$root" -maxdepth 1 -type l -name 't3-*' | sort
done
```

Do not overwrite existing contributor-mode symlinks for third-party companion skills.

## Step 4: Optional Claude Code Hooks

If the user uses Claude Code, validate `~/.claude/settings.json` for the hooks below.
Show the JSON patch the user should apply, but **do not edit the file without consent**.

Required hooks (all paths relative to `$T3_REPO/integrations/claude-code-statusline/`):

| Hook | Event | Matcher | Script | Purpose |
| --- | --- | --- | --- | --- |
| Skill suggestion | `UserPromptSubmit` | *(none)* | `ensure-skills-loaded.sh` | Detect intent from prompt keywords and suggest skills to load |
| Active repo tracking | `PostToolUse` | `Read\|Edit\|Write\|Grep\|Glob\|Bash` | `track-active-repo.sh` | Track which repos are touched |
| Skill usage (PostToolUse) | `PostToolUse` | `Skill` | `track-skill-usage.sh` | Track loaded skills via tool call |
| Skill usage (InstructionsLoaded) | `InstructionsLoaded` | `skills` | `track-skill-usage.sh` | Track loaded skills via instructions (belt-and-suspenders fallback) |
| Status line | `statusLine` | — | `statusline-command.sh` | Render the status bar |

JSON patch to merge into `~/.claude/settings.json`:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "~/path/to/teatree/integrations/claude-code-statusline/ensure-skills-loaded.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "PostToolUse": [
      {
        "matcher": "Read|Edit|Write|Grep|Glob|Bash",
        "hooks": [
          {
            "type": "command",
            "command": "~/path/to/teatree/integrations/claude-code-statusline/track-active-repo.sh",
            "timeout": 5
          }
        ]
      },
      {
        "matcher": "Skill",
        "hooks": [
          {
            "type": "command",
            "command": "~/path/to/teatree/integrations/claude-code-statusline/track-skill-usage.sh",
            "timeout": 5
          }
        ]
      }
    ],
    "InstructionsLoaded": [
      {
        "matcher": "skills",
        "hooks": [
          {
            "type": "command",
            "command": "~/path/to/teatree/integrations/claude-code-statusline/track-skill-usage.sh",
            "timeout": 5
          }
        ]
      }
    ]
  },
  "statusLine": {
    "type": "command",
    "command": "~/path/to/teatree/integrations/claude-code-statusline/statusline-command.sh"
  }
}
```

Replace `~/path/to/teatree` with the actual `$T3_REPO` value from `~/.teatree`.

When validating an existing installation, check that **all five hooks** are present. The `InstructionsLoaded` hook was added as a fallback because the `PostToolUse` `Skill` matcher is intermittently unreliable in some Claude Code sessions.

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

This rule exists in `t3-workspace/SKILL.md` but skills are only loaded on demand. The global agent config is **always loaded**, catching the agent before it has a chance to improvise.

## Step 5: Generate an Overlay Package

Use the TeaTree bootstrap CLI, not shell overlay scaffolding.

Basic form:

```bash
uv run t3 startoverlay <overlay-name> <destination-dir>
```

When the filesystem directory and Python package should differ, also pass:

```bash
uv run t3 startoverlay my-overlay ~/workspace/my-org --overlay-package my_overlay
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
- Prefer `uv run t3 ...` and Django settings over `PYTHONPATH` tricks.
- Stop at the first broken prerequisite and fix that before continuing.
