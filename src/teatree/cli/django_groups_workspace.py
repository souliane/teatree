"""Worktree and workspace command groups of the overlay command catalogue."""

from teatree.cli.django_group import DjangoGroup

WORKSPACE_GROUPS: dict[str, DjangoGroup] = {
    "worktree": DjangoGroup(
        "Per-worktree FSM operations.",
        [
            ("provision", "Run DB import + env cache + direnv + prek + overlay setup steps for one worktree."),
            ("start", "Boot ``docker compose up`` for one worktree."),
            ("verify", "Run overlay health checks for one worktree."),
            ("ready", "Run runtime readiness probes for one worktree."),
            ("teardown", "Stop docker, drop DB, remove git worktree, delete row."),
            ("status", "Report FSM state, branch, and allocated host ports for one worktree."),
            ("diagnose", "Print a structured health checklist for one worktree."),
            ("smoke-test", "Quick health check: overlay loads, CLI responds, imports OK."),
            ("diagram", "Print a state diagram as Mermaid. Models: worktree, ticket, task."),
            ("occupancy", "Show every checkout a live agent currently holds."),
            ("claim-occupancy", "Claim a checkout for a hand-driven lane, refusing if an agent already holds it."),
            ("release-occupancy", "Hand a checkout back, naming whose claim was freed."),
            ("attest", "Record the checkout state after the owner has inspected it."),
            ("adopt", "Register an existing on-disk checkout as a Worktree row."),
        ],
    ),
    "workspace": DjangoGroup(
        "Ticket-level workspace operations (every worktree in the ticket).",
        [
            ("ticket", "Create or update a ticket and trigger worktree provisioning."),
            ("provision", "Provision every worktree in the current ticket workspace."),
            ("start", "Start docker for every worktree in the current ticket workspace."),
            ("ready", "Run readiness probes for every worktree in the ticket workspace."),
            ("teardown", "Tear down every worktree in the current ticket workspace."),
            ("finalize", "Squash worktree commits and rebase on the default branch."),
            ("doctor", "Detect state drift across every store; optionally fix it."),
            ("clean-merged", "Tear down every worktree whose ticket is already MERGED."),
            ("clean-all", "Prune merged worktrees, stale branches, orphaned stashes, orphan DBs, old DSLR snapshots."),
            ("repair-split", "Move a ticket's divergent worktrees into its canonical workspace dir (keeps all work)."),
            (
                "relocate",
                "Move this overlay's existing worktrees under the per-overlay workspace dir (git worktree move).",
            ),
            ("list-orphans", "List orphan branches (commits not on main, no open PR)."),
            ("landscape", "Survey in-flight PRs/MRs and local unsynced work before planning (read-only)."),
            ("branch-verdict", "Is this branch's work already on the default branch? The canonical answer."),
            ("reap-stale", "Tear down ABANDONED docker stacks no live worktree owns (age-guarded)."),
            (
                "reclaim-disk",
                "Reclaim disk via zero-data-loss docker prunes (builder + dangling images + unreferenced volumes).",
            ),
            ("stamp-identity", "Stamp the repo's local git identity to the GitHub noreply form (public-push safety)."),
            (
                "stamp-owners",
                "Record which checkout owns each auto-isolated env dir this venue can see (deletes nothing).",
            ),
            (
                "release-dead-rows",
                "Delete Worktree rows whose checkout is provably gone — the row alone, nothing else touched.",
            ),
            (
                "repair-branch-upstreams",
                "Point every branch tracking someone else's ref back at its own, or untrack it.",
            ),
            ("emit", "Print the JSON handoff for every NOT-auto-deleted worktree (the judgment skill's input)."),
            ("salvage", "Capture a branch's unique content to a PR, verify it landed, then delete the branch."),
            ("restore", "Apply a captured salvage bundle back into a checkout (--into, --dry-run)."),
            (
                "prek-patches",
                "Which prek patches hold work absent from the tree, and restore one (--repo, --restore, --dry-run).",
            ),
        ],
    ),
}
