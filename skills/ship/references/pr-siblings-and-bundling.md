# Sibling PRs — the content sweep and the bundling procedure

The procedures behind `/t3:ship` § "Also sweep by content for ticketless PRs" and § "Bundle Into an Existing Open PR". Those sections carry the rules; this file carries the sweep commands, the match signals, the eligibility conditions, and the bundling steps.

## Content sweep for ticketless PRs

The ticket-ref query in `/t3:ship` § "One Open PR Per Ticket" misses **retro fixes, skill edits, and other PRs without a ticket reference**. Before opening any such PR, also run a content sweep against open and recently-merged PRs on the same repo and look for overlap on title keywords or touched files:

```bash
# Open PRs (parallel work in flight)
gh pr list --repo <repo> --state open --json number,title,headRefName

# Recently-merged PRs (work that landed minutes ago — same risk)
gh pr list --repo <repo> --state merged --limit 10 --json number,title,mergedAt
```

Match against:

- **Title keywords** that overlap with the about-to-be-pushed PR's title (e.g., "rules", "worktree", "anti-fabrication"). Synonyms count.
- **Touched files** that overlap with `git diff --name-only origin/main..HEAD` on the local branch — for skill PRs especially, multiple agents/users converge on the same `skills/<topic>/SKILL.md` file.

Treat a hit on either signal as a sibling and apply the same options (wait, stack, or bundle per § "Bundling into a sibling PR" below). If the hit is in the recently-merged list, run `git fetch origin main && git log origin/main..HEAD` — if the local diff is now empty, abandon the branch instead of pushing an empty PR.

## Bundling into a sibling PR

When a session uncovers a small unique commit on a now-stale branch (typical during cleanup or retro), and opening a dedicated PR for that one commit would be more ceremony than the change deserves, **bundle it into a sibling open PR** instead. This trades a little PR-scope discipline for delivery speed.

**Eligibility — all must hold:**

1. The commit is small and self-contained (single concern, no cross-cutting impact).
2. The target PR is **still open** and **not yet approved** (bundling into an approved PR forces re-review).
3. The target PR is on the same repo and the change is at least loosely thematically adjacent. Strictly unrelated bundles are still better than abandoning the work, but explain it in the PR description.
4. The bundled commit doesn't depend on or contradict anything in the target PR's diff.

**Procedure:**

1. Fetch the target PR's worktree (or create one with `t3 <overlay> workspace ticket <issue-url>` — use the same issue as the target PR).
2. Cherry-pick the commit: `git cherry-pick <sha>`. Resolve any conflicts surgically.
3. Run lint + the affected tests locally.
4. Push to the target PR's branch (regular push, no rebase).
5. **Update the target PR's title and description** to reflect both commits. Title format becomes `type(scope1): X + type(scope2): Y` if the two are heterogeneous. Body explains both fixes.
6. Notify the reviewer in the PR comments that the scope grew, with a one-line rationale.
7. Force-remove the original worktree and delete the now-empty branch (`git worktree remove --force <path>` + `git branch -D <branch>`).

**Anti-pattern:** bundling into a PR that's already passed review. The reviewer's approval covered the original scope, not the bundled commit.
