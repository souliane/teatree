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

- The **skill repo** you are in now. It provides the `t3-*` workflow skills, references, hooks, and the `teetree` Django extension package.
- A **generated Django host project** created with `t3 startproject`. That project owns the real Django settings, declares `TEATREE_OVERLAY_CLASS`, and installs the overlay app that customizes TeaTree for one workspace.

Do not scaffold `scripts/lib/bootstrap.sh`, `project_hooks.py`, or shell overlays. Those are legacy migration artifacts.

## What This Skill Does

1. Check prerequisites.
2. Create or validate `~/.teatree`.
3. Install teatree skill symlinks for the current agent runtime.
4. Optionally wire Claude Code hooks and statusline.
5. Generate a Django host project with `t3 startproject`.
6. Verify the generated project with `manage.py check`.

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

Do not require `T3_OVERLAY`. The active overlay now lives in the generated Django host project, not in a shell-sourced overlay repo.

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

## Step 5: Generate a Django Host Project

Use the TeaTree bootstrap CLI, not shell overlay scaffolding.

Basic form:

```bash
uv run t3 startproject <project-root-name> <destination-dir> --overlay-app <overlay_app>
```

When the filesystem directory and Django package should differ, also pass:

```bash
uv run t3 startproject my-project ~/workspace/my-org --project-package my_project --overlay-app myapp
```

Generated shape:

```text
my-project/
├── manage.py
├── my_project/
│   ├── settings.py
│   ├── urls.py
│   ├── asgi.py
│   └── wsgi.py
└── myapp/
    ├── apps.py
    ├── overlay.py
    ├── models.py
    └── SKILL.md
```

Rules:

- `overlay.py` must subclass `OverlayBase`.
- `settings.py` must define `TEATREE_OVERLAY_CLASS`.
- The generated project is the place where project-specific Django customisation lives.

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
