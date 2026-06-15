# skills — local conventions

See the root [`CLAUDE.md`](../CLAUDE.md) for the code-quality bar. This is the source-of-truth skills tree (`plugins/t3/skills` is a symlink to here). This file adds only what is specific to skill authoring.

- **One skill per directory**, each with a `SKILL.md`. Frontmatter keys: `name`, `description`, `compatibility`, `metadata` (with `version`, `subagent_safe`) — plus the relationship keys:
  - `requires:` — skills auto-loaded *with* this one (hard dependency).
  - `companions:` — generic methodology skills loaded alongside (e.g. `test-driven-development`).
  - `triggers:` — phrases that auto-load the skill via the `UserPromptSubmit` hook.
- **Never slim a skill.** Don't move SKILL.md prose into `references/` to save tokens — agents don't reliably load reference files on demand. Add a `references/` file only for genuinely optional deep-dive material; keep load-bearing rules inline. (`/t3:rules` § "Never Slim Skills".)
- **`subagent_safe: true`** only for pure methodology that needs no shell functions, MCP, or cross-skill state.
- **Ship co-located evals.** A behaviour-bearing skill ships its evals beside `SKILL.md` as `skills/<name>/evals.yaml` (same `EvalSpec` schema; omit `agent_path` to default it to the owning skill) — the way a unit's tests live next to the unit (the Anthropic eval-driven-development convention). A pure-doc / methodology skill instead declares a non-empty `eval_exempt: <reason>` frontmatter key. `t3 eval coverage` reports any skill that is neither covered nor exempt as a gap. See `tests/agent_behavior/README.md` § "Co-located evals" + § "Per-skill coverage gate".
- **Reference skills by their qualified canonical name** (`t3:<skill>`): names resolve against their owning namespace and a qualifier is never stripped to force a match, so a bare name that collides across namespaces (`t3:review` vs `other:review`) stays distinct (see `architecture-design/SKILL.md` § "Identity and key normalization").
- Skill-system spec: BLUEPRINT.md § 11.
