# Contributor Guidelines

## Repo Structure

```text
t3-*/SKILL.md          Lifecycle skill definitions
t3-*/references/       Skill-specific reference docs (one level deep)
references/            Shared cross-skill references (agent-rules, platforms, etc.)
scripts/               Python CLI entry points (thin wrappers)
scripts/lib/           Core logic modules (pure functions, testable)
scripts/frameworks/    Framework plugins (auto-detected at runtime)
integrations/          Agent platform hooks (Claude Code, etc.)
```

## Skill Files

- `SKILL.md` is the entry point. Keep it focused on workflow and rules.
- Move detailed content to `references/` — one level deep only.
- Never change `version:` in YAML frontmatter — auto-managed.
- `subagent_safe: false` for all `t3-*` skills (they depend on shell functions and env state).

## Python Scripts

All scripts must follow these conventions:

1. **uv shebang:** `#!/usr/bin/env -S uv run --script`
2. **Inline metadata:** `# /// script` block with `dependencies` list (even if empty)
3. **Typer for CLI:** `typer>=0.12` in inline deps — no raw `sys.argv` or `argparse`
4. **Type annotations:** `ty-check` runs on all files — use `str | None` not `Optional[str]`
5. **4-space indentation** everywhere (matches `.editorconfig`)
6. **Make executable:** `chmod +x` the script file
7. Core logic in `scripts/lib/` (pure functions). `scripts/*.py` are thin CLI wrappers.

## Shell Scripts

- Shebang: `#!/usr/bin/env bash` — never hardcoded paths.
- Scripts using `declare -A` need a Bash 4+ version guard with re-exec.
- macOS/Linux portable: wrap `md5`, `stat`, `open` with platform detection.
- Always quote variables: `"$VAR"` not `$VAR`.

## Testing

- **100% coverage required** — enforced by `pytest-cov` in CI and pre-commit.
- Run tests: `uv run pytest`
- Install pre-commit hooks: `prek install` (runs automatically on each commit)
- Full repo check: `prek run --all-files` (useful after config changes)

## Cross-Cutting Rules

See `references/agent-rules.md` for Non-Negotiable rules that apply to all skills at runtime (verification before claims, definition of done, temp file safety, etc.). Those rules govern skill *execution*; this file governs *contribution*.

## Abstraction Boundaries

- `t3-*` skills must **never** reference a specific project or overlay by name.
- Project-specific knowledge belongs in the project overlay (`$T3_OVERLAY`).
- User-specific preferences belong in the user's memory/config files.
- If a core skill needs project context, use an extension point or config variable.
