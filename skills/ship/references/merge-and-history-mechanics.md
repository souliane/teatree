# Merge and history-rewrite mechanics

The mechanics behind `/t3:ship` § "Merging the Default Branch into a PR" and § "Git History Rewriting". Those sections carry the rules; this file carries the 3-way merge reasoning, the JSON conflict-resolution semantics, and the filter-branch details.

## Commit index and prek stash recovery

**What the commit gate commits is the INDEX, not the working tree.** prek stashes unstaged
changes, runs the hooks against the index, and restores the stash afterwards — once for the
pre-commit stage and again for commit-msg. An edit made AFTER `git add` is therefore not in
the resulting commit, and the working tree still shows it, so a `cat` of the file confirms a
fix that did not ship. Verify with `git show HEAD:<path>`, never the working tree.

prek announces this itself, on stdout, every time it applies:

```text
Unstaged changes detected. Temporarily saving them to `~/.cache/prek/patches/<id>.patch`
...
Restored unstaged changes from `~/.cache/prek/patches/<id>.patch`
```

No hook can add to that notice, which is why the behaviour is documented rather than gated.
A hook runs after the stash, so `git diff --name-only` inside one reports nothing — the
information is already gone by the time any hook could look. And prek prints a hook's output
only when the hook FAILS, so a warn-only hook stays invisible unless it blocks every commit
that carries an unstaged change. The two lines above are the signal; `git commit -a` is the
way to avoid the split in the first place — on a checkout only you write to.

**On a SHARED checkout that stash is a data hazard, and `git commit -a` is the wrong escape.**
prek stashes every unstaged change in the tree, another writer's included, so your commit
decides the fate of work you never touched. Three ways it goes wrong, all observed: `-a`
commits their half-finished edits under your message; a run killed between the stash and the
restore leaves the tree without them; and a write landing DURING the hook window is discarded
whole (`Hook changes conflicted with the saved unstaged changes. Reverting the hook changes`).
So where more than one writer shares a checkout:

```bash
git add <path>                                    # right after EVERY edit — prek stashes only what is UNSTAGED
git commit -o -F <msg-file> -- <your paths only>  # never bare, never -a
```

prek keeps every patch it writes rather than deleting it on restore, so
`~/.cache/prek/patches/` is a durable backup: recover a stash the tree never got back with
`git apply ~/.cache/prek/patches/<ts>-<pid>.patch`, newest timestamp first.

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

**A public-repo privacy refusal is judged per commit, never on the tip.** The `refuse-public-push-with-leak` pre-push gate scans every commit the remote does not have yet — each one's own patch and message — so a fix-up commit that deletes or annotates the flagged line does not clear it; the refusal labels such a finding `(earlier than the pushed tip)`. Cut a fresh branch from the remote's tip and re-create the unpushed commits on it with generic placeholders (one combined commit is fine): that fast-forwards the remote, so nothing pushed or reviewed is rewritten. A deliberate fake value (a test-fixture email, path, address or key) carries the inline `privacy-scan:allow <reason>` marker in the commit that introduces it; the marker never silences a registry term or an opaque id.

When rewriting commit messages, use `filter-branch --msg-filter` (matches by full hash). Do NOT use `git rebase -i` with `GIT_SEQUENCE_EDITOR="sed"` — the short hash may differ from the one-line log, causing a silent no-op.

**Post-rewrite verification (Non-Negotiable):** After ANY rebase or filter-branch, verify the hash changed. Same hash = no-op.

**Rebase todo shorthand:** When automating `git rebase -i` with `GIT_SEQUENCE_EDITOR`, the todo list uses single-letter shorthand (`p` not `pick`, `f` not `fixup`). Match on `^p` not `^pick`. Use `sed -e '/^p <hash>/s/^p/f/'` for fixup squashing.
