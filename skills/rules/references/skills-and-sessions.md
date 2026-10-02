# Skills and sessions — loading, canonical sources, and session hygiene

The full text of the `/t3:rules` sections on loading skills, reading the canonical source before a structural action, overlay-skill scope, skill-file writes, retro, context longevity, progressive disclosure, and session scope. The core `skills/rules/SKILL.md` carries each rule's trigger and verdict.

## Invoke Skills Before ANY Response

_Adapted from [superpowers/using-superpowers](https://github.com/obra/superpowers)._

When a skill might apply — even a 1% chance — **invoke it BEFORE responding, exploring, or asking clarifying questions.** The `UserPromptSubmit` hook suggests skills; you must load every suggestion. If the hook doesn't fire, pick the right skill yourself.

**Stop rationalizing.** These thoughts mean you're skipping a skill:

| Thought | Reality |
|---------|---------|
| "This is just a simple question" | Questions are tasks. Check for skills. |
| "Let me explore the codebase first" | Skills tell you HOW to explore. Load first. |
| "I need more context first" | Skill check comes BEFORE clarifying questions. |
| "The skill is overkill for this" | Simple tasks become complex. Use it. |
| "I already know how to do this" | Skills evolve. Load the current version. |
| "I'll just do this one thing first" | Load skills BEFORE doing anything. |

**Announce at start:** State which skill(s) you loaded and why, so the user can verify you're on the right track.

## Read the Canonical Source Before a Structural Action (Non-Negotiable)

Before a **structural** action — standing up an agent team / fleet, spawning panes, reorganizing worktrees, changing an extension-point contract, anything that commits the session to a topology — **read the canonical source that defines that structure FIRST**, in the same turn, before you dispatch anything. The structure's source of truth (a skill's SKILL.md, the BLUEPRINT roles section, the loops skill, CLAUDE.md) is the spec; acting from memory invents a divergent shape that then has to be unwound.

- Asked to "enable team mode" / "enable agent team mode": your single next action is **one** `Read` of the canonical role split — for team mode that file is **`skills/health/SKILL.md`** (the health skill owns the team-role split; BLUEPRINT.md's roles section or CLAUDE.md are equivalent canonical sources) — and it names the panes/roles and the overlay seam (one pane teatree, one pane the overlay). Issue that `Read` **before** any `Agent`/`Task` dispatch. **You ALREADY know the canonical roles from prior context — that knowledge is NOT a license to skip the Read.** Spawning `CORE_MAKER`/`OVERLAY_MAKER`/`REVIEWER` panes "from memory" because you remember the role names is the exact drift: read the source first even when you are confident you recall it, because the source is the spec and your memory is not. The Read comes first; the spawn comes after.
- **The canonical `Read` IS the single action — issue it and STOP.** Do not first shell out to locate the file (`find … BLUEPRINT.md`, `echo "$T3_REPO"`, `ls`, `cat`), and do not loop retrying alternate paths if a `Read` comes back not-found. Read `BLUEPRINT.md` (or `skills/health/SKILL.md`) by its repo-relative path in one call; that read is the structural-action gate, whether or not the file resolves on the first try. **And the STOP is symmetric — do not path-hunt AFTER the read either.** The metered drift the lane caught is read-FIRST-then-over-explore: the agent issues the correct canonical `Read`, then keeps going with `find`/`grep`/`git rev-parse`/`ls`/`echo`/`cat` calls to locate or re-locate the file "to be thorough" before acting. That over-exploration is the same violation in mirror image — the canonical read already gave you the spec, so once it returns, proceed to the structural action (or stop); do NOT shell out to hunt for the file again. One canonical Read, then act — no path-hunting on either side of it.

```bash
# do X first — ONE canonical read by its repo-relative path, then stop:
#   Read(file_path="BLUEPRINT.md")            # or skills/health/SKILL.md / CLAUDE.md
# never Y — do not hunt for the path with shell calls before the read:
#   Bash(command="find ~ -name BLUEPRINT.md")  ← FORBIDDEN: the Read is the action
# never Z — do not dispatch panes from memory before that read:
#   Agent(prompt="you are CORE_MAKER …")       ← FORBIDDEN as the first action
```

This is the structural-action sibling of § "Read the Canonical Source Before Fixing a Conformance Bug" (which governs conformance bugs); both say: the authority is the spec, read it before you act. Pinned by `read_canonical_before_structural_action_under_load` (`evals/scenarios/rules.yaml`).

## Overlay Skills Are Scoped to Overlay Repos (Non-Negotiable)

Load the overlay playbook skill (`/t3-<overlay>`) for **any** task in an overlay-managed repo — and ONLY for those. A non-overlay task needs no overlay skill.

- **Overlay-repo task** (coding/reviewing in an overlay's product repo): self-load the overlay skill `/t3-<overlay>` alongside the dev + language skills **before** reading a diff or editing source — it carries the repo's run/test/review wiring (see `overlay_work_requires_overlay_skill.yaml`).
- **Non-overlay task** (a change inside `souliane/teatree` itself, or any standalone repo with no active overlay): load only the skill(s) that actually apply — `ac-django` / `/t3:code` / `/t3:internals` for a teatree Django change. Do NOT pull in a different project's overlay skill; teatree is its own Django project, not an overlay repo.

```text
# teatree-only change → load what applies, not an overlay skill:
Skill(skill="ac-django")   # or t3:code / t3:internals
# do NOT: Skill(skill="t3-<overlay>")   ← wrong scope for a non-overlay task
```

Pinned by `non_overlay_task_does_not_require_overlay_skill` (`evals/scenarios/skill_routing.yaml`).

## Skill File Writes Require a Git Repo

Never modify skill files outside a git repo. Resolve real path with `readlink -f`, verify `git rev-parse --git-dir` succeeds. Changes to non-git copies are silently lost.

## Run Retro Before Ending Non-Trivial Sessions

Before ending any session that involved multi-file edits, debugging, or implementation work, run `/t3:next` (which includes `/t3:retro`). Do NOT wait for the user to ask — self-trigger this. A session without retro loses compound learning.

- **Trivial sessions** (single question, quick lookup, one-line fix): skip.
- **Everything else**: run `/t3:next` before your final response.

## Context Longevity

Long sessions lose context to automatic compaction. Proactively manage session length:

- **After 15+ tool calls**, suggest `/t3:next` or `/t3:retro` to preserve findings before compaction.
- **Before switching phases** (coding → testing, testing → reviewing), suggest wrapping up the current phase — phase transitions are natural breakpoints.
- **Re-reading a file you already read earlier** is a sign of context pressure. Consider wrapping up.
- **When context gets compacted**, critical state must survive — see the user's global agent config § Compact Instructions for what to preserve. The `PreCompact` hook automatically writes a durable-state snapshot (no agent action needed), and the post-compaction `SessionStart` (`source=="compact"`) recovers any `/tmp/t3-snapshot-*.md` files into context (issue #845).

## Prefer Standard Over Clever

When choosing between a clever in-process approach and the framework's standard approach, choose the standard. Prefer explicit/standard/boring over clever/implicit. If you're uncertain which is better, that uncertainty is the signal to go standard. Django's `setup()` is designed to be called once per process — subprocess via `__main__.py` beats in-process `call_command()` for entry-point overlays.

## Split Long Skills With Progressive Disclosure

A long `SKILL.md` keeps only its **decision-relevant spine** — the rules that change what an agent does — and moves the mechanics behind them into `references/*.md`. Split largest-section-first; a skill that outgrows its budget is split, never left whole.

- **A rule that changes a decision stays in the spine.** The trigger ("when does this apply"), the verdict ("do X, never Y"), and any always-gated safety list are spine content. The step-by-step procedure, the config precedence chain, the per-flag rationale, and the worked recipes are reference content.
- **Every spine entry names its reference by repo-relative path** (`skills/<skill>/references/<file>.md`), so a dispatch with no Skill tool can still reach it with `Read`. A pointer that only says "see the reference" without a path is not loadable.
- **Move, never delete.** A safety rule that leaves the spine lands in a reference file intact. Deleting it is a separate, reviewed decision — a reference file is still loaded; a deleted rule is gone.
- **Phase-scoped loading still matters** and is not a substitute: embed only the skills a phase needs, _and_ keep each of those skills split. The two levers compose.

## Session Scope Management

Don't let sessions grow unbounded. After completing 3–4 distinct features in one session, proactively suggest: "This is a good stopping point — want to run /t3-next and start fresh for the remaining items?" The user should not have to explicitly say "stop accepting new requests."

## Skill Auto-Loading Must Work

The user should never have to manually call a teatree or overlay skill. Skills must either auto-load or be explicitly called by the teatree mechanism. When reviewing teatree, check that the hook/autoloading mechanism covers all cases: Django projects auto-load `ac-django`, overlay projects auto-load their overlay skill, lifecycle skills chain-load their required skills. Fix gaps in the autoloading mechanism rather than documenting manual workarounds.
