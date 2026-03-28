# Investigation: Decouple t3-retro as Standalone Skill

**Status:** Open
**Issue:** #16

## Questions

1. What still couples t3-retro to teatree? (T3_CONTRIBUTE, T3_REPO, overlay awareness, cross-skill references)
2. Is it worth decoupling? The retro pattern (audit -> root cause -> fix skills -> commit) is broadly useful.
3. Could it be distributed via Claude Code marketplace or auto-install?

## Coupling Audit

- `T3_CONTRIBUTE`, `T3_REPO`, `T3_PUSH` — teatree config but generic patterns
- Overlay awareness (editability checks) — could generalize to "project-specific config"
- References to other `t3-*` skills — dependency chain
- `t3 tool privacy-scan` CLI usage — teatree-specific CLI

## Rough Approach

1. Extract generic `retro` skill with no teatree imports
2. Make teatree behavior load conditionally (when `~/.teatree.toml` exists)
3. Generic version works with any git repo + any agent config
4. Teatree-enhanced version adds overlay awareness and `/t3-contribute` chaining

## Recommendation

_Pending investigation._
