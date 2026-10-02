# The simplification pass — what qualifies, what never goes, and how

The catalogues behind `/t3:retro` § "3b. Simplification Pass (Auto-Cleaning)". That section carries the question every retro asks; this file carries what qualifies for removal, what is never removed, how to simplify, and the commit convention.

## Qualifies for removal / consolidation

- **Duplicate rules** — the same guardrail stated in multiple skills, memory files, or `CLAUDE.md`. Keep one canonical home; replace others with a one-line cross-reference.
- **Stale instructions** — steps describing a workflow the CLI now handles automatically, or referencing removed commands/flags/paths.
- **Procedural sprawl** — step-by-step commands where `t3` already does the work (see `/t3:retro` § "4. Quality Rules" → "Never write CLI procedures into skills").
- **Unused checks** — verification steps that slowed the session down but did not catch a real issue, and never fired across prior retros.
- **Over-verbose prose** — multi-paragraph explanations where a one-line rule suffices.

## Never remove

- **Destructive-action rules** — push confirmations, force-push gates, `--no-verify` bans, deletion approvals. Cost is ~0 tokens per turn; blast radius is real.
- **Rules that prevented a real failure** (this session or a prior retro). When uncertain, leave it.
- **Rules backed by an explicit user preference** (saved feedback memory, `CLAUDE.md` entry). Ask before removing.

## How to simplify

- **Prefer consolidation over deletion.** Move the rule to one canonical home (typically `rules/SKILL.md` or the most relevant dedicated skill); replace duplicates with one-line pointers (`See <skill>/SKILL.md § <anchor>`). Keep anchors stable so cross-references don't break.
- **Delete only when the rule is stale or unused.** A deletion must be justified in the commit message: either "handled by `t3 <command>`" (stale) or "never triggered across N retros" (unused).
- **Measure the change.** Include the before/after line count delta for touched files in the commit message.

## Commit convention

Use `refactor(<skill>): simplify <what>` (not `fix(<skill>)`). One commit per coherent simplification so reverts stay surgical. Example: `refactor(ship): drop duplicate push-confirmation rule — canonical in rules/SKILL.md`.

## When in doubt, ask

If a rule looks like overhead but you cannot confirm it is unused, ask with `AskUserQuestion`. Show the rule, show grep evidence of recent invocations, and propose remove vs. keep. The cost of asking is low; the cost of removing a load-bearing rule is high.
