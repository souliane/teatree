# Merge and history-rewrite mechanics

The mechanics behind `/t3:ship` § "Merging the Default Branch into a PR" and § "Git History Rewriting". Those sections carry the rules; this file carries the 3-way merge reasoning, the JSON conflict-resolution semantics, and the filter-branch details.

## Merging the default branch into a PR

Before touching the PR branch to "prepare" it for a merge, reason through what a clean 3-way merge would produce on its own:

- **Default branch removed keys/lines the PR still has?** The merge will apply those removals automatically — no preemptive cleanup commit needed. Adding one creates noise and risks side effects (e.g., `json.dumps` round-tripping normalizes unrelated formatting).
- **Both branches independently added the same key/line with different values?** That is a true add/add conflict. But verify the merge result first — the merge may have already resolved it correctly. Only surface it to the user if the result actually needs to change.

**Merge conflict resolution for JSON files:**

- Use proper 3-way semantics: `result = theirs + (ours_keys − base_keys)`. This correctly applies the default branch's removals while keeping the PR's own additions.
- Do NOT use `json.dumps` to serialise back — it normalises indentation and whitespace across the entire file, producing a noisy diff far beyond the intended change. Remove keys surgically (line-by-line) to preserve original formatting.
- Do NOT use `git checkout --ours` on whole files — this discards the default branch's removals and reintroduces whatever it had cleaned up.

**After resolving conflicts, verify before asking anything:**

1. Check that all PR-own additions (keys in ours but not in the merge base) are present in the result.
2. Check that any values that differ between ours and theirs are already at the correct value per the merge strategy. If the result is already correct, do not ask the user — they made no decision to make.

## Git history rewriting

When rewriting commit messages, use `filter-branch --msg-filter` (matches by full hash). Do NOT use `git rebase -i` with `GIT_SEQUENCE_EDITOR="sed"` — the short hash may differ from `git log --oneline`, causing a silent no-op.

**Post-rewrite verification (Non-Negotiable):** After ANY rebase or filter-branch, verify the hash changed. Same hash = no-op.

**Rebase todo shorthand:** When automating `git rebase -i` with `GIT_SEQUENCE_EDITOR`, the todo list uses single-letter shorthand (`p` not `pick`, `f` not `fixup`). Match on `^p` not `^pick`. Use `sed -e '/^p <hash>/s/^p/f/'` for fixup squashing.
