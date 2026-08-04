# Personal config hygiene — discovery, actions, and the promotion classes

The procedures behind `/t3:retro` § "7. Clean Personal Config" and § "9. Consolidation over Drift". Those sections carry when each scan runs; this file carries how memory files are discovered, the four actions, and the three promotion classes.

## Discovering memory files and the four actions

**Discovery:** Memory files are platform-specific. Discover them dynamically:

- **Claude Code:** glob `~/.claude/projects/*/memory/MEMORY.md` — each match is an index file; read it to find individual memory files in the same directory.
- **Repo-level:** check for `CLAUDE.md`, `.cursorrules`, `AGENTS.md`, or similar agent config in the project root.
- If no memory files are found, skip this step and note it in the retro output.

**Actions:**

1. **Promote to skills:** Any guardrail, pattern, or "do this not that" entry that would help other users → move to the appropriate skill file. Leave a one-line safety-net reminder if the rule is critical enough to need early loading.
2. **Scan for promotable entries:** Read the discovered memory/config files for entries marked `(Also in: ...)` or containing domain knowledge that belongs in a skill file. Propose promoting them — the `(Also in: ...)` marker indicates the entry was intentionally duplicated as a safety net, but the authoritative source should be verified and kept current.
3. **Remove stale entries:** If a memory entry references old paths, deleted features, or outdated patterns — update or remove it.
4. **Deduplicate:** If the same rule appears in both a skill AND memory/config, verify the skill version is current, then trim the config copy to a one-line reference.

## The three promotion classes

**Classification (apply to every candidate found):**

| Class | Criteria | Action |
|---|---|---|
| **(P) Promote to framework** | Framework behavior every teatree installation should get out of the box (e.g., a hook `t3 setup` should wire automatically) | Promote: open a teatree issue or submit the code/docs change |
| **(C) Model as documented config** | Legitimately instance-specific, but teatree should expose a documented config surface so users don't solve it ad-hoc | Create a teatree issue to add the config knob; document the expected pattern in BLUEPRINT.md or a skill |
| **(K) Keep personal** | Genuine user preference with no cross-instance value (theme, voice settings, personal path shortcuts) | Leave it; no action |

**Decision rule:** If different instances genuinely need different behavior, that difference **must** be modelled as a documented teatree setting or config option — not left as divergent ad-hoc config. Undocumented divergence silently drifts; documented variation is an explicit choice other users can make too.

This scan complements § 7 "Clean Personal Config". Section 7 covers memory-entry hygiene (promoting guardrails, removing stale entries, deduplicating). This section covers *behavioral* promotion: hooks wired by hand, permission patterns added manually, automation scripts in personal dotfiles that should be first-class teatree features.
