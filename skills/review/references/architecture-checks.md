# Architecture checks

## Module-Level Architectural Check (Non-Negotiable)

After verifying repo rules, **check the full file** (not just changed lines) of every file touched by the diff against the loaded coding skills' **"Architectural Health"** review checklist.

1. **Identify loaded coding skills.** TeaTree auto-detects `ac-*` skills from the repo shape (e.g., `ac-python`, `ac-django`). If they have an "Architectural Health" review checklist section, apply it.
2. **For each touched file**, evaluate the FULL file against those checklists. Key checks (skill-specific details are in the skill itself):
   - Module size (LOC)
   - Module-level function count and justification
   - God-module detection (unrelated concerns in one file)
   - Complexity rule suppressions in `pyproject.toml` — any `C901`/`PLR09xx` per-file-ignores beyond the project's boilerplate baseline are findings
3. **When a threshold is crossed**, never suppress the lint rule. On **your own** change, refactor to comply in this same PR — the module is one this diff already touches, so `AGENTS.md` First Principles 8-10 put the fix here, not in a follow-up ticket. When reviewing **someone else's** change, post it as a finding and leave the fix to the author (maker ≠ checker).
4. **Check `pyproject.toml` per-file-ignores** for the touched files. If any suppress complexity rules that are not in the project's boilerplate baseline, flag them as findings.

This step prevents architectural drift. Each diff looks fine in isolation — this check catches the cumulative effect by examining the full module.

## File-Hierarchy & Module-Placement Check (Non-Negotiable)

The Module-Level Architectural Check above asks *what's inside* each touched file. This one asks *where the changed files live* — but **scoped strictly to the diff**, never a whole-tree audit. Examine only files the change adds, moves, or renames, plus the directories they land in:

1. **New files in the wrong directory or module.** For each added file, confirm it sits in the package whose concern it shares. A scanner belongs under the scanners package, a CLI command under the CLI package, a model under the models package — flag a file dropped beside unrelated neighbors with a concrete "this new file should live at `X`" suggestion.
2. **Should the change have created or moved into a subpackage?** When a diff adds the third or fourth sibling file all serving one new concern into an already-crowded directory, flag that the cohesive set should become its own subpackage (with the proposed path).
3. **Files added at the repo root that belong under a directory.** A new script, config, or module dropped at the repo root is a finding unless the repo's conventions place it there — name the directory it should move under.
4. **Diffs that worsen module cohesion or scoping.** Flag a change that widens a module's responsibility (an unrelated concern bolted onto an existing file), leaks a private helper across a package boundary, or imports across a layer the architecture keeps separate — point at the boundary the change crosses.
5. **Obvious reorg opportunities the change reveals.** When implementing the change makes a misplacement plain (e.g. the file you just edited clearly belongs next to the collaborators it now calls), surface the concrete move — but only for files this diff touches.

Each finding must name the suggested target path so the implementer can act without re-deriving it. **Full-tree reorganization audits are out of scope here** — sweeping the entire repository's layout for misplaced modules is the `ac-reviewing-codebase` skill's job (the periodic holistic review dispatched by the architectural-review loop). Keep this per-change check scoped to the diff so the two surfaces complement rather than duplicate each other.

## The One-Place Test — Count the Files (Non-Negotiable)

The two checks above ask what is *inside* a touched file and *where* the changed files live. This one asks **who owns the concept**, and the reviewer asks it out loud of every concept the diff touches:

> If this concept changes shape tomorrow, how many files do I touch?

The answer must be **ONE**. Any higher count is a finding — state the count and name the files. "No code duplication introduced" is the weaker test, and three shapes pass it while failing this one:

1. **A module-level helper called from N call sites.** It removes the duplicated *lines* and leaves N places to edit, so the count is unchanged. The fix is an owner the call sites hand the concept to — a serializer, a renderer, a component — not a function they each re-invoke.
2. **"These two are different, so leave that one out."** Interrogate the difference before accepting it. A difference of **presentation** — a value rendered as a locale string, a date stringified for an encoder, an extra flag appended — is what a serializer field or a composed renderer is for, and exempts nothing. Only a difference of **behaviour or data shape** does.
3. **Deduplicating 3 of 5 sites.** Four remaining edits is still not one.

**Centralising is not flattening.** The shared owner holds the *invariant* and may keep deliberately-distinct *strategies* distinct — it takes the block, it never picks the strategy. In the change that produced this rule one resolver returned `None` rather than falling back to a live reading **on purpose**, its reason recorded in the code ("showing today's number beside a rate computed from an unknown one is the same defect with less excuse"), so unifying the resolvers would have reintroduced a defect that had been deliberately removed; the shared component owned the invariant — an absent key never reaches the payload as a null, an unpriced payload stays byte-identical — and took the block without picking a strategy. Over-unification is this finding pointed the other way: flag N owners for one concept, and flag one owner that erased a deliberate difference.
