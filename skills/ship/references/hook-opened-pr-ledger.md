# A Hook-Opened PR and Its Ticket's Ledger

Mechanics for [`../SKILL.md`](../SKILL.md) § 4a1.

## How one missing row loses the PR

The no-orphan pre-push hook runs `t3 <overlay> pr ensure-pr` with no ticket handle, and `teatree.loop.scanners.pending_pr.PendingPrDrainScanner` re-runs the same path for a deferred first push. Both resolve the owning ticket through `teatree.core.management.commands._ensure_pr._ticket_for_branch` alone: the newest `Worktree` row on the branch. A checkout made by a bare `git worktree add` has no row, so the PR opens and nothing records it:

- No `PullRequest` row is written, so `mcp__teatree__pr_for_ticket` is empty.
- `teatree.core.models.ticket_introspection.TicketIntrospectionModel.has_shippable_diff` finds no worktree with commits ahead. The review step sets `shipping_skipped` ("no shippable diff") and the ticket is auto-ignored.
- `teatree.core.merge.ticket_resolution.resolve_gated_ticket` finds no ledger row and no `MergeClear`, so the reviewer's brief carries no rubric and the rubric done-gate never binds the merge.
- The hook writes its placeholder body before any ticket is known, so it carries no `Tracks <issue>` line. Unless the commit message itself has a closing keyword, `src/teatree/loop/manual_pr_reconcile.py` cannot attach the PR either.

That is how PR #4903 (issue #4900) merged with its ticket ignored and its rubric out of the merge gate's reach.

## Prevent

- Create the checkout with `t3 <overlay> workspace ticket <issue-url>`, which writes the `Worktree` row.
- For a checkout made any other way, run `t3 <overlay> workspace ticket <issue-url> --adopt` inside it before the first push. On a detached HEAD, pass `--adopt-branch <branch>` instead.
- Pass the ticket's exact `issue_url`, as `mcp__teatree__ticket_get` shows it, including any `#dream-batch=` fragment. `teatree.utils.url_slug.repo_namespaced_key` keeps the fragment in the ticket key, so the bare issue URL binds a different ticket.

## Side Effect: a One-Hour Delivery Lease

Every `workspace ticket` call claims its ticket for an external owner, `--adopt` included, even when the checkout already has its row. `teatree.core.management.commands._workspace.ticket_intake.build_ticket` calls `teatree.core.models.external_delivery.mark_external_delivery`, which sets `extra['external_delivery']` to expire `teatree.core.models.external_delivery.LEASE_SECONDS` (3600 s, one hour) later. While that lease is live:

- `teatree.core.models.task.Task.dispatchable_q` excludes every task on the ticket, so the loop dispatches none of its testing, reviewing or shipping tasks.
- `teatree.loop.scanners.pr_sweep_review_gate.arm_cold_review` arms no review for the ticket's PR.
- Each `t3 <overlay> ticket transition`, and each recorded plan, renews it for another hour. No command releases it early; it lapses on its TTL.

So a loop-dispatched coder or tester reads `mcp__teatree__worktree_status <ticket>` before running `--adopt`. A worktree listed on the branch means the row exists and `--adopt` is not needed. An agent that does run `--adopt` says in its result that the ticket's next task waits up to an hour.

## Verify

After the push that opens the PR, `mcp__teatree__pr_for_ticket <ticket>` lists its URL. An empty list means nothing was recorded, so heal it before the reviewing phase ends.

## Heal

1. Run the `--adopt` command inside the checkout. The next review then sees the worktree's commits and schedules shipping. The adopt also stamps the delivery lease above, so that review waits up to an hour.
2. Re-running `t3 <overlay> pr ensure-pr` does not heal it. On a branch with an open PR, `teatree.core.management.commands._ensure_pr.skip_for_classified` returns the "open PR exists" skip and records nothing.
3. On GitHub, the ship's `pr create` adopts the open PR (`teatree.backends.github.pr_create._adopt_existing_pr_or_reraise`) and records it. Read `mcp__teatree__pr_for_ticket` again afterwards. GitLab has no such adoption, so there step 4 applies.
4. If the list is still empty, or the ticket is already ignored or merged, ask the owner, naming the PR and the ticket. Never report the work delivered.
