# A Hook-Opened PR and Its Ticket's Ledger

Mechanics for [`../SKILL.md`](../SKILL.md) § 4a1.

## How one missing row loses the PR

The no-orphan pre-push hook runs `t3 <overlay> pr ensure-pr` with no ticket handle, and `teatree.loop.scanners.pending_pr.PendingPrDrainScanner` re-runs the same path for a deferred first push. Both resolve the owning ticket through `teatree.core.management.commands._ensure_pr._ticket_for_branch` alone: the newest `Worktree` row on the branch. A checkout made by a bare `git worktree add` has no row, so the PR opens and nothing records it:

- No `PullRequest` row is written, so `mcp__teatree__pr_for_ticket` is empty.
- `teatree.core.models.ticket_introspection.TicketIntrospectionModel.has_shippable_diff` finds no worktree with commits ahead. The review step sets `shipping_skipped` ("no shippable diff") and the ticket is auto-ignored.
- `teatree.core.merge.ticket_resolution.resolve_gated_ticket` finds no ledger row and no `MergeClear`, so the reviewer's brief carries no rubric and the rubric done-gate never binds the merge.
- The hook writes its placeholder body before any ticket is known, so it carries no `Tracks <issue>` line. Unless the commit message itself has a closing keyword, `src/teatree/loop/manual_pr_reconcile.py` cannot attach the PR either.

That is how PR #4903 (issue #4900) merged with its ticket ignored and its rubric out of the merge gate's reach.

## The Hook Runs the Installed `t3`

The `PullRequest` ledger row, the `PendingPullRequest` obligation and the ticket's `extra` must land in the canonical control DB, the one the merge gates read. A checkout's own editable install resolves a per-worktree isolated SQLite copy instead (`teatree.core.provision.db_anchor.active_db_is_worktree_isolated`), so a hook that runs the checkout's code opens the PR and writes rows no gate ever sees: the reviewer's brief carries no rubric and the critic and anti-vacuity gates never bind.

`scripts/hooks/ensure-pr-installed-t3.sh` runs `t3 teatree pr ensure-pr --repo <toplevel>` with every `PATH` entry under the checkout removed, so the `t3` it finds is the installed one (`deploy/t3` dispatches it into the worker). It acts only when `PRE_COMMIT_REMOTE_BRANCH` is `refs/heads/*`. The prek manual stage and `t3 tool verify-gates` leave that variable unset, and tag and claim pushes name other refs, so none of them writes an obligation.

- The wrapper exits 0 with a warning that prints `t3 <overlay> pr ensure-pr --repo <abs> --branch <branch>` when `t3` exits `69`, `75`, `126`, `127` or `137`: `deploy/t3` returns `69` when this venue cannot run `t3` (checkout outside every translatable root, Docker unreachable, stack never built, secret store wedged) and `75` for a stack mid-update; a shell returns `126` for a `t3` that is not executable and `127` for no installed `t3`; `137` is a `t3` that was killed or ran out of memory. A broken deployed image therefore never blocks the push of its own fix: fix the image or stack, then run the printed command, which is the only thing that opens the PR. The hook is `verbose: true` because prek hides a passing hook's output otherwise. Any other exit code fails the push, and `SKIP=ensure-pr` skips the hook for that one push.
- A ship push (`mcp__teatree__pr_create`, `t3 <overlay> pr create`, the loop's ship) sets `TEATREE_SHIP_PUSH` (`teatree.core.forge_push.SHIP_PUSH_ENV`, passed as `push_branch(ship_opens_pr=True)`), and the wrapper then exits 0 without calling `t3`. The ship opens the PR itself right after the push, and `pr create --sync` holds the control DB write lock (`BEGIN IMMEDIATE`) across that push, so the hook's own obligation write would wait on the lock for about 150 s (`retry_on_locked`, 5 x 30 s) and fail the push. A PR-open that fails after the push is the ship's own failure result, and the ship's retry opens it; no obligation row covers that branch.
- `ensure-pr` on an isolated DB, which only a checkout's own `uv run` reaches, returns an `error` naming the installed `t3` and writes nothing. `ensure-pr` is in `soft_refusal_commands`, so the error still exits 0 and never blocks the push.

Not covered:

- `--repo` carries the checkout's host path and `deploy/t3` translates only the working directory. On a host whose paths differ from the container's, `ensure-pr` returns an `error` and exits 0, so no PR opens. The box has path identity.
- A push from a detached HEAD to a branch name with no local branch: the classifier reads local refs and answers "branch ref is missing".
- `t3 fast-push` opens its PRs through its own upsert and records no ledger row.
- prek uses only the first ref of a multi-ref push, so a push that lists a tag first skips the branch.
- A branch whose `.pre-commit-config.yaml` still carries the `uv run` entry keeps the isolated-DB behaviour until it merges `main`.
- A push that is not a ship push still waits on the control DB write lock when another process holds it: the obligation write retries for about 150 s, then the push fails. `SKIP=ensure-pr` skips the hook for that push, and `t3 <overlay> pr ensure-pr --repo <abs> --branch <branch>` opens the PR afterwards.
- The `t3` call has no deadline. `deploy/t3` waits up to `TEATREE_UPDATE_WAIT_SECONDS` (180 s) for a stack mid-update before it exits `75`, and a wedged `docker compose exec` holds the push. The wrapper sets none because macOS has no `timeout`.

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
4. If the push printed the hook's `ensure-pr skipped` warning, run the command it prints from a checkout under a mounted root. Nothing opens the PR until then.
5. If the list is still empty, or the ticket is already ignored or merged, ask the owner, naming the PR and the ticket. Never report the work delivered.
