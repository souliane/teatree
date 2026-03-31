# Skill Triggers

Skills can declare when they should be auto-loaded by adding a `triggers:` block to their `SKILL.md` frontmatter. The `UserPromptSubmit` hook matches the user's prompt against these patterns and injects a directive to load matching skills before the LLM processes the prompt.

This is a deterministic routing layer — not LLM-based semantic matching.

## Frontmatter Schema

```yaml
---
name: my-skill
triggers:
  priority: 50
  exclude: '\breview\b'
  keywords:
    - '\b(commit|push)\b'
    - '^fix '
  urls:
    - 'https?://gitlab\.[^\s]+/-/issues/\d+'
  end_of_session: true
---
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `priority` | int | `50` | Lower = matched first. Use 10-30 for specific intents, 50-70 for generic, 100+ for fallbacks. |
| `keywords` | list[str] | `[]` | Regex patterns matched against the lowercased prompt via `re.search`. Patterns starting with `^` anchor to the start of the prompt. |
| `urls` | list[str] | `[]` | Regex patterns for URL detection. Checked before keywords across all skills (URLs always win). |
| `exclude` | str | `""` | If this pattern matches the prompt, skip this skill entirely. Useful for disambiguation (e.g., ship excludes `\breview\b`). |
| `end_of_session` | bool | `false` | When `true`, this skill only triggers on end-of-session phrases ("done", "lgtm") and only if other lifecycle skills were loaded in the session. |

### Matching Order

1. **URL pass**: All skills sorted by priority — first URL pattern match wins.
2. **Keyword pass**: All skills sorted by priority — first keyword match (after exclude check) wins.
3. **End-of-session pass**: If no URL/keyword matched and the prompt is an end-of-session phrase, skills with `end_of_session: true` are checked.

### Priority Guidelines

| Range | Use for |
|-------|---------|
| 10-20 | Highly specific intents that should override others (e.g., ship, test) |
| 30-40 | Moderately specific (e.g., review-request before review) |
| 50-70 | Generic intents (e.g., debug, ticket, code) |
| 80-100 | Rare or fallback intents (e.g., setup, retro) |
| 110+ | Catch-all intents (e.g., workspace, followup) |

## How It Works

1. At startup (or via `t3 config write-skill-cache`), teatree scans `~/.claude/skills/*/SKILL.md`, extracts `triggers:`, and writes a trigger index to `~/.local/share/teatree/skill-metadata.json`.
2. On every user prompt, the `UserPromptSubmit` hook reads the cached index, matches patterns, resolves `requires:` dependencies, and injects `"LOAD THESE SKILLS NOW: /skill1, /skill2"`.
3. If no cache exists, the hook falls back to scanning `skill_search_dirs` on the fly.

## Adding Triggers to a New Skill

Add the `triggers:` block to your `SKILL.md` frontmatter, then rebuild the cache:

```bash
t3 config write-skill-cache
```

Skills without `triggers:` are not matched by the hook — they can still be loaded manually via `/skill-name` or as dependencies of other skills via `requires:`.
