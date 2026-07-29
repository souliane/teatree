---
name: interactive
description: "ENGAGES TEATREE FOR THE SESSION. Loading this skill — or any skill declaring `requires: interactive` — writes the `.teatree-active` marker, one of the two conditions in `_loop_auto_load_active()` that arm the loop and statusline (#256); a session that never loads it stays unengaged, by design. Also holds teatree's Claude Code harness wiring: how skills are selected, how plugin hooks are registered, and which output belongs to the headless pipeline rather than an interactive session. Teatree's own architecture and coding rules are `/t3:internals`; the dogfooding procedure is `/t3:dogfooding`."
eval_exempt: harness-wiring reference plus the engagement marker; the engagement behaviour is pinned by tests/test_teatree_opt_in.py, not by an agent trajectory
metadata:
  version: 0.0.1
---

# TeaTree — Interactive Session Wiring

The Claude Code side of teatree: what engages a session, how skills reach an agent, and how hooks are registered. Loading this skill is itself the engagement act — see "Engagement" below.

## Skill Loading

Skill loading is fully explicit — there is no free-text scan of the prompt. Skills load via slash commands (`/t3:code`), phase mapping (`t3 agent --phase coding`), ticket status, the transitive `requires:` dependency chain, and cwd/overlay context. TeaTree's UserPromptSubmit hook surfaces only the skills a prompt's cwd/overlay context implies — framework skills (`ac-django`/`ac-python`), the active overlay's own skill, and its `companion_skills`. A PreToolUse hook blocks Python code edits until those load.

The `SkillLoadingPolicy` class resolves which skills to load from an explicit phase / ticket-status / cwd-overlay context and expands each root's `requires:` chain transitively.

**Engagement is default-OFF ([#256](https://github.com/souliane/teatree/issues/256)).** Installing the plugin does NOT force teatree onto every session. A fresh session is *not engaged*: the UserPromptSubmit suggester (and the T3 CLI reminder) is suppressed, `<session>.pending` stays empty so the PreToolUse gate never blocks, and SessionStart shows a one-line how-to advisory instead of arming the loop. A session engages teatree when any of: the owner set `[teatree] autoload = true` (or `T3_AUTOLOAD=1`); a teatree-requiring skill loaded (the `<session>.teatree-active` marker); or **any** `t3:` skill loaded (the `<session>.t3-engaged` marker, set by `handle_track_skill_usage`). The cold-hook seam is `hook_router._teatree_engaged` = `_autoload_enabled() OR _teatree_active() OR <session>.t3-engaged`. Note the two markers differ: `.t3-engaged` engages only the suggester, while loop scheduling still gates exclusively on `.teatree-active` (so a plain lifecycle skill never arms loops). Explicitly running `/teatree` engages the session for the next prompt.

## Plugin Hooks Architecture

Hooks are registered in `hooks/hooks.json` (shipped with the plugin). This is the **sole source** for hook registrations — do NOT duplicate hooks in the user's `~/.claude/settings.json`. When migrating hooks to the plugin, remove the `settings.json` equivalents in the same change to avoid double execution.

## Interactive vs Headless Output

The `{"summary":..., "files_modified":...}` JSON result block from `/t3:next` is consumed by the headless pipeline. In interactive sessions it's noise — skip it and only show the text summary.

## Related Skills

Each skill below declares `requires: interactive`, so loading it engages the session too — that is the contract, and `tests/conformance/test_engagement_skill_requires_walk.py` keeps this table and those edges in agreement.

| Skill | When to load |
|-------|--------------|
| `/t3:dogfooding` | Validating a CLI, loop, or statusline change; or self-QA on the loop and statusline — find, file, and fix bugs in one session |

`/t3:wip` is deliberately absent: it is cross-cutting, so working a backlog does not by
itself engage teatree.

`/t3:internals` is NOT in this table on purpose: it loads from the CHECKOUT, not from a mode. Teatree's own architecture and management-command rules are needed on a teatree ticket and irrelevant on a customer ticket, so `SkillLoadingPolicy.detect_internals_skill` keys it on the worktree being a teatree checkout — which reaches a headless worker too, where a mode skill never would.
