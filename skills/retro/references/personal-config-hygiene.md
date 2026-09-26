# Personal config hygiene — what retro touches, and the three promotion classes

The procedures behind `/t3:retro` § "7. The Memory Corpus Belongs to `memory_skim`, Not to Retro" and § "9. Consolidation over Drift". Those sections carry when each scan runs; this file carries what retro may edit, and the three classes that decide where a finding goes.

## What retro edits, and what it leaves alone

Retro's durable homes are an editable skill file and `t3 <overlay> retro finding`. A repo-level agent config file (`CLAUDE.md`, `AGENTS.md`, `.cursorrules`) in the project root is editable and stays a legitimate destination for a project-specific rule.

The assistant's own memory corpus is never retro's to touch — not to discover, not to add to, not to prune. What already exists there stays where it is; retro's job is to stop adding to it. The corpus's own lanes are `t3 tool audit-memory` (manual, read-only, always available) and the `memory_skim` mini-loop (one owner-facing promote-or-drop question per ISO week — default-OFF, so it sweeps nothing until enabled).

## The three promotion classes

**Classification (apply to every candidate found):**

| Class | Criteria | Action |
|---|---|---|
| **(P) Promote to framework** | Framework behavior every teatree installation should get out of the box (e.g., a hook `t3 setup` should wire automatically) | `t3 <overlay> retro finding --destination <the code or skill path>` — a deduped umbrella checkbox plus a scheduled coding fix |
| **(C) Model as documented config** | Legitimately instance-specific, but teatree should expose a documented config surface so users don't solve it ad-hoc | `config_setting set` where a knob already exists; where none does, the missing knob is itself a (C) finding — emit it the same way |
| **(K) Keep personal** | Genuine user preference with no cross-instance value (theme, voice settings, personal path shortcuts) | Leave what already exists alone; retro creates nothing new here |

**Decision rule:** If different instances genuinely need different behavior, that difference **must** be modelled as a documented teatree setting or config option — not left as divergent ad-hoc config. Undocumented divergence silently drifts; documented variation is an explicit choice other users can make too.

This scan complements § 7. Section 7 says who owns the memory corpus. This section covers *behavioral* promotion: hooks wired by hand, permission patterns added manually, automation scripts in personal dotfiles that should be first-class teatree features.
