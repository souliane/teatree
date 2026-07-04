# Audit — teatree vs. Anthropic Claude Code best practices

- **Date:** 2026-05-19
- **Reference:** [How Claude Code works in large codebases — best practices and where to start](https://claude.com/blog/how-claude-code-works-in-large-codebases-best-practices-and-where-to-start)
- **Umbrella issue:** [#1019](https://github.com/souliane/teatree/issues/1019)
- **Method:** every claim cites `file:line` per [/t3:rules § "Grep Before Claiming Cross-Reference Coverage"](../plugins/t3/skills/rules/SKILL.md). No claim is asserted from skill-name pattern-matching alone.

## Scope read

- `BLUEPRINT.md` (2163 lines)
- `CLAUDE.md` root (34 lines)
- `plugins/t3/skills/*/SKILL.md` — 28 skills, sizes 22 → 641 lines
- `hooks/hooks.json` + `hooks/scripts/hook_router.py` (entry points and gates)
- `src/teatree/core/overlay.py` (overlay extension surface)
- `plugins/t3/settings.json` (permissions allow/deny)
- `~/.claude/CLAUDE.md` (user routing)

## Findings

| # | Anthropic recommendation | teatree status | Evidence | Action |
|---|---|---|---|---|
| 1 | CLAUDE.md layered — "root file for the big picture, subdirectory files for local conventions" | **PARTIAL** | Root `CLAUDE.md` (34 lines) is well-shaped as pointers; no subdir files exist — `find . -maxdepth 4 -name CLAUDE.md` returns only the root + an `apm_modules` import. | Add subdir `CLAUDE.md` in `src/teatree/core/`, `hooks/`, `plugins/t3/skills/`, `tests/`, `e2e/`. Tracked in [#1020](https://github.com/souliane/teatree/issues/1020). |
| 2 | "Root file should be pointers and critical gotchas only; everything else drifts into noise" | **DONE** | Root `CLAUDE.md:1-34` — code-quality bar (7 bullets) + architecture pointer + running things. No drift. | None. |
| 3 | Hooks for continuous improvement — reflect on sessions and propose updates "while the context is fresh" | **DONE** | `hooks/hooks.json` registers `PreCompact` and `Stop`; `hooks/scripts/hook_router.py:1224` (PreCompact retro snapshot) + `/t3:retro` skill (`plugins/t3/skills/retro/SKILL.md`, 497 lines) + `/t3:next` chain. | None. |
| 4 | "Automated checks like linting and formatting, hooks enforce the rules deterministically" | **DONE** | `.pre-commit-config.yaml` (9.9KB) wires ruff/markdownlint/codespell; `hook_router.py:73-165` blocks workaround commands; `hook_router.py:437-470` `protect-default-branch` deny. | None. |
| 5 | "A start hook can load team-specific context dynamically" | **DONE** | `hook_router.py:200-264` `handle_user_prompt_submit` → `skill_loader.suggest_skills()` with `teatree_home`/`source_root`/`active_repos`. SessionStart bootstrap at `hooks/hooks.json:5-13`. | None. |
| 6 | Skills use progressive disclosure — "offload specialized workflows and domain knowledge that would otherwise compete" | **PARTIAL** | Skills do separate concerns (`/t3:code`, `/t3:test`, `/t3:ship`), but `/t3:rules` is 641 lines and explicitly forbids extraction (`plugins/t3/skills/rules/SKILL.md:631-634` § "Never Slim Skills"). Top-level context for a coding session loads ~1000+ lines. | Re-test progressive disclosure on the current model and either split or document the disagreement. [#1024](https://github.com/souliane/teatree/issues/1024). |
| 7 | "Skills can also be scoped to specific paths so they only activate in the relevant part of the codebase" | **MISSING** | `grep -l 'allowed-paths\|^path:\|^paths:' plugins/t3/skills/*/SKILL.md` returns nothing. Activation is `keywords` / `urls` / `end_of_session` only. | Add `triggers.paths` glob list to skill frontmatter and `skill_loader`. [#1021](https://github.com/souliane/teatree/issues/1021). |
| 8 | "Security review skill loads when Claude is assessing code for vulnerabilities … task-specific loading" | **DONE** | Per-phase skills with keyword triggers — `plugins/t3/skills/code/SKILL.md:9-20`, `plugins/t3/skills/ticket/SKILL.md:9-24`, `plugins/t3/skills/test/SKILL.md:9-15`. Priority + exclude regex resolves overlaps. | None. |
| 9 | "A plugin bundles skills, hooks, and MCP configurations into a single installable package" | **DONE** | `plugins/t3/.claude-plugin/plugin.json` + `marketplace.json`; `hooks/hooks.json` registers 8 events; 28 skills under `plugins/t3/skills/`. | None. |
| 10 | "Plugin updates can be distributed across the organization through managed marketplaces" | **DONE** | `plugins/t3/.claude-plugin/marketplace.json` declares `souliane` marketplace; APM also documented at `BLUEPRINT.md:1361-1368`. | None. |
| 11 | LSP — "Running LSP servers so Claude searches by symbol, not by string" | **MISSING** | `grep -i 'lsp\|language server\|symbol search' BLUEPRINT.md plugins/t3/skills/*/SKILL.md` returns zero hits. Only `tach.toml` (module-graph constraint config) exists. | Recommend installing pyright + ts-language-server; add `t3 doctor lsp` advisory check. [#1022](https://github.com/souliane/teatree/issues/1022). |
| 12 | Sub-agent exploration-editing split — "read-only subagent to map a subsystem and write findings to a file, then have the main agent edit" | **DONE** | `BLUEPRINT.md:1340-1357` § 11.2 documents 8 phase agents; `agents/` holds YAML+description wrappers; `subagent_safe: true` flag (`plugins/t3/skills/rules/SKILL.md:7`); orchestrator-execution-boundary guard at `hook_router.py` PreToolUse (BLUEPRINT.md:2014). | None. |
| 13 | "MCP servers expose structured search as a tool Claude can call directly" | **PARTIAL** | Teatree consumes Slack/Notion/glab/chrome MCPs but exposes none. Its internal `Ticket`/`Worktree`/`Task`/`PullRequest`/`IncomingEvent` models are only reachable via `t3` CLI + Bash text parsing. | Add a read-only `teatree.mcp` server exposing ticket/worktree/PR queries. [#1023](https://github.com/souliane/teatree/issues/1023). |
| 14 | "CLAUDE.md files at the subdirectory level should specify the commands that apply to that part of the codebase" | **PARTIAL** | Root `CLAUDE.md:30-34` lists `uv run pytest`/`ruff check` but does so globally. `tests/` and `e2e/` directories have no `CLAUDE.md`. | Covered by [#1020](https://github.com/souliane/teatree/issues/1020). |
| 15 | "Using .ignore files to exclude generated files, build artifacts, and third-party code" | **MISSING** | No `.claudeignore` / `.ignore` exists. `.claude/worktrees/` (subagent sidechain checkouts) holds 17 duplicates of `hook_router.py` that pollute every search. | Ship `.claudeignore` excluding `.venv/`, `.claude/worktrees/`, `node_modules/`, `sbom.json`, etc. [#1027](https://github.com/souliane/teatree/issues/1027). |
| 16 | "Committing permissions.deny rules in .claude/settings.json means the exclusions are version-controlled" | **DONE** | `plugins/t3/settings.json` (committed) ships `permissions.deny` for default-branch push, force-push, `--no-verify`, `rm -rf` rooted at `/`/`~`/`.`/`..`, repo deletes, `curl \| sh`. See file lines 100-137. | None. |
| 17 | Codebase mapping — "lightweight markdown file at repo root listing each top-level folder" | **PARTIAL** | `BLUEPRINT.md:54-228` § 3 "Package Structure" exists but is 54 lines into a 2163-line file; README is product-shaped, not navigational. | Add a 50-line `MAP.md` at repo root. [#1025](https://github.com/souliane/teatree/issues/1025). |
| 18 | "Teams should expect to do a meaningful configuration review every three to six months" | **MISSING** | `grep -rIn 'configuration review\|three to six\|stale rule' BLUEPRINT.md plugins/t3/skills/*/SKILL.md` returns zero. `/t3:rules` is 641 lines with no review/expiry process. | Add `/t3:config-review` (or extend `/t3:retro`) with quarterly self-trigger via `t3 schedule`. [#1026](https://github.com/souliane/teatree/issues/1026). |
| 19 | "CLAUDE.md rules … may either become unnecessary or actively constraining" as models evolve | **MISSING** | Same evidence as #18 — no last-reviewed timestamps on any skill, no audit cadence. | Covered by [#1026](https://github.com/souliane/teatree/issues/1026). |
| 20 | DRI — "one person with ownership over the Claude Code configuration, the authority to make calls" | **DONE** | `souliane` is sole owner; CLAUDE.md routing + `~/.teatree.toml` + skill ownership all centralized. | None. |
| 21 | Centralized assembly — "an individual or a team assemble and evangelize the right Claude Code conventions" to prevent fragmentation | **DONE** | `BLUEPRINT.md` is the single source of truth (`BLUEPRINT.md:7` "every code change to teatree must be reflected here"); skill loading + the requires chain resolve conflicts (`BLUEPRINT.md:1330-1338`). | None. |
| 22 | Governance — "starting with a defined set of approved skills, required code review processes, and limited initial access" | **DONE** | `BLUEPRINT.md:1309-1326` lists 16 approved skills with purpose; `/t3:review` (442 lines) defines self-review + give-review + receive-review processes; `t3 setup` bootstraps. | None. |

## Summary

- **DONE:** 12 / 22 recommendations
- **PARTIAL:** 5 / 22
- **MISSING:** 5 / 22

The five MISSING items concentrate in three themes:

1. **Path-based context narrowing** — no `.claudeignore`, no path-scoped skills, no subdirectory `CLAUDE.md`. Closes by [#1020](https://github.com/souliane/teatree/issues/1020), [#1021](https://github.com/souliane/teatree/issues/1021), [#1027](https://github.com/souliane/teatree/issues/1027).
2. **Symbol-level tooling** — no LSP, no structured-search MCP. Closes by [#1022](https://github.com/souliane/teatree/issues/1022), [#1023](https://github.com/souliane/teatree/issues/1023).
3. **Aging configuration** — no review cadence on accumulated rules. Closes by [#1026](https://github.com/souliane/teatree/issues/1026).

## Top-3 highest-priority gaps

1. **[#1027](https://github.com/souliane/teatree/issues/1027) — ship `.claudeignore`.** Lowest cost (one file), highest signal-to-noise gain. Today a grep for any hook_router symbol returns 18 duplicates from `.claude/worktrees/agent-*/`. Fix: add 12 lines.
2. **[#1026](https://github.com/souliane/teatree/issues/1026) — document and run the 3–6 month config-review cadence.** `/t3:rules` is 641 lines of accumulated guardrails written against several model generations. Without a review pass, the standing context drifts toward "constraining" — the article's own diagnosis. The fix surfaces stale rules and lets the user decide what to drop.
3. **[#1021](https://github.com/souliane/teatree/issues/1021) — add path-scoped skill activation.** `/t3:rules` § 42 ("Skill Auto-Loading Must Work") and BLUEPRINT § 5.4-5.5 already mandate "the user should never have to manually call a skill" — but the loader has no path dimension, so cross-repo sessions get false positives. This is the highest-leverage skill-system improvement.

## Recommendations not yet evaluated against newer model versions

The article emphasizes that rules age. Two teatree positions deserve a controlled re-test on the current frontier model:

- **`/t3:rules` § 40 "Never Slim Skills"** — written when references-on-demand were unreliable. The article presumes they now are. See [#1024](https://github.com/souliane/teatree/issues/1024).
- **`/t3:rules` § 26 "Sub-Agent Limitations"** — written when sub-agents lost all skills/MCP. CC has shipped `subagent_safe` and `Skill` invocation inside sub-agents since. Confirm whether the constraint still binds.

Both should be folded into the quarterly review proposed by [#1026](https://github.com/souliane/teatree/issues/1026).
