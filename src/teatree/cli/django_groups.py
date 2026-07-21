"""Overlay Django command-group catalogue.

The static description of every ``t3 <overlay> <group> <sub>`` command tree
(``DjangoGroup`` + the ``DJANGO_GROUPS`` table), split out of ``overlay.py`` so
the app-builder logic and this data catalogue each stay a focused module.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class DjangoGroup:
    """One overlay sub-app group description.

    ``core_dispatch`` flags groups whose subcommands live in
    ``teatree.core.management.commands`` (not in any overlay-owned
    ``manage.py``). When ``True``, :meth:`OverlayAppBuilder._bridge_subcommand`
    dispatches via :func:`managepy_core` (``python -m teatree``) so the call
    never reaches an overlay's ``manage.py`` whose settings module may not
    register the command (#1318 follow-up to #1312).

    ``core_subcommands`` is the *per-subcommand* variant for a mixed group:
    most subcommands route through the overlay ``manage.py`` (they drive the
    overlay's own behaviour) but a named few must run in the teatree-core
    runtime. ``db approve`` is the canonical case — it records a ``DbApproval``
    row in the teatree-core control DB the gate reads, so it must run in the
    runtime process (``python -m teatree``) rather than route through an overlay
    ``manage.py`` whose self-DB lacks the row (#953/#126).

    ``overlay_settings_subcommands`` is the third variant: subcommands that must
    run in the active overlay's **own** Django settings context when it ships
    one, so an overlay-shipped Django app (and its migrations) is in
    ``INSTALLED_APPS``. ``db migrate`` is the case — it must apply BOTH the
    teatree-core migrations AND the overlay app's migrations against the same
    canonical control DB. The core ``teatree.settings`` cannot see the overlay
    app: its entry-point app discovery imports the overlay class at
    settings-bootstrap time, which raises before the app registry is ready (or
    on a missing overlay dependency), so the overlay app is dropped and its
    migrations are structurally invisible on the ``python -m teatree`` path. The
    overlay's own settings module lists the app explicitly and resolves the same
    canonical DB, so :meth:`OverlayAppBuilder._bridge_subcommand` routes migrate
    through the overlay ``manage.py`` when the overlay ships its own settings
    module, and keeps the in-process core path when it runs on the base
    ``teatree.settings`` (nothing extra to load) or ships no project dir.
    """

    help_text: str
    subcommands: list[tuple[str, str]]
    core_dispatch: bool = False
    core_subcommands: frozenset[str] = frozenset()
    overlay_settings_subcommands: frozenset[str] = frozenset()

    def dispatches_to_core(self, sub_name: str) -> bool:
        """True iff *sub_name* must dispatch via teatree-core, not overlay manage.py."""
        return self.core_dispatch or sub_name in self.core_subcommands

    def needs_overlay_settings(self, sub_name: str) -> bool:
        """True iff *sub_name* must run in the overlay's own settings context when it ships one."""
        return sub_name in self.overlay_settings_subcommands

    def resolve_core_dispatch(self, sub_name: str, *, ships_own_overlay_settings: bool) -> bool:
        """Whether *sub_name* dispatches via teatree-core, given this overlay's settings.

        Core-dispatched when the group/subcommand is marked core-only, OR when it
        is an overlay-settings subcommand (``db migrate``) but the overlay does
        NOT ship its own settings module — the overlay ``manage.py`` context
        would add nothing (its ``INSTALLED_APPS`` are the core ones), so the
        in-process core path is kept (#126, no ``uv --directory`` hop). An
        overlay-settings subcommand on an overlay that DOES ship its own settings
        module routes to the overlay ``manage.py``, where the overlay app (and
        its migrations) is in ``INSTALLED_APPS``.
        """
        if self.dispatches_to_core(sub_name):
            return True
        return self.needs_overlay_settings(sub_name) and not ships_own_overlay_settings


DJANGO_GROUPS: dict[str, DjangoGroup] = {
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
            (
                "relocate",
                "Move this overlay's existing worktrees under the per-overlay workspace dir (git worktree move).",
            ),
            ("list-orphans", "List orphan branches (commits not on main, no open PR)."),
            ("landscape", "Survey in-flight PRs/MRs and local unsynced work before planning (read-only)."),
            ("reap-stale", "Tear down ABANDONED docker stacks no live worktree owns (age-guarded)."),
            (
                "reclaim-disk",
                "Reclaim disk via zero-data-loss docker prunes (builder + dangling images + unreferenced volumes).",
            ),
            ("stamp-identity", "Stamp the repo's local git identity to the GitHub noreply form (public-push safety)."),
            ("emit", "Print the JSON handoff for every NOT-auto-deleted worktree (the judgment skill's input)."),
            ("salvage", "Capture a branch's unique content to a PR, verify it landed, then delete the branch."),
        ],
    ),
    "run": DjangoGroup(
        "Run services.",
        [
            ("verify", "Verify worktree state and return URLs."),
            ("services", "Return configured run commands."),
            ("backend", "Start the backend dev server."),
            ("frontend", "Start the frontend dev server."),
            ("build-frontend", "Build the frontend for production/testing."),
            ("tests", "Run the project test suite."),
            ("lint", "Run the overlay's lint pipeline on this worktree."),
        ],
    ),
    "e2e": DjangoGroup(
        "E2E test commands.",
        [
            ("run", "Run E2E tests — dispatches to project or external runner based on overlay config."),
            ("trigger-ci", "Trigger E2E tests on a remote CI pipeline."),
            ("external", "Run Playwright tests from the external test repo (T3_PRIVATE_TESTS)."),
            ("project", "Run E2E tests from the project's own test directory."),
            (
                "post-test-plan",
                "Post/update the ticket's single test-plan note (side-by-side Dev|Local test plan) from a manifest.",
            ),
            (
                "tracked-manifest",
                "Print a manifest's authored half (run provenance stripped) for a private test repo to commit.",
            ),
            ("retract-evidence", "Withdraw the ticket's single test-plan note."),
            (
                "post-evidence",
                "[Deprecated] Alias for post-test-plan (renamed; kept one release for back-compat).",
            ),
        ],
    ),
    "db": DjangoGroup(
        "Database operations.",
        [
            ("migrate", "Apply pending migrations to the runtime self-DB (non-destructive self-rescue)."),
            ("refresh", "Re-import the worktree database from dump/DSLR."),
            ("approve", "Record a single-use DbApproval that satisfies the #777 fresh-dump gate without a TTY (#953)."),
            ("restore-ci", "Restore database from the latest CI dump."),
            ("reset-passwords", "Reset all user passwords to a known dev value."),
            ("query", "Run a read-only SQL query against the control DB; emit rows as JSON."),
            ("shell", "Drop into a Django shell against the resolved (gate) control DB."),
        ],
        # `approve` records a DbApproval row in the teatree-core control DB the
        # gate reads at consume time, so it must run in the runtime process
        # (`python -m teatree`) rather than route through an overlay manage.py
        # whose self-DB lacks the row (#953/#126). `migrate` must apply BOTH the
        # core migrations AND the overlay app's migrations against that same
        # canonical DB — the core settings cannot see an overlay-shipped app
        # (its bootstrap-time app discovery fails), so migrate runs in the
        # overlay's own settings context when the overlay ships one, and stays
        # on the core path otherwise. Their siblings (refresh/restore-ci/
        # reset-passwords) always route through the overlay manage.py for the
        # overlay's db_import strategy.
        core_subcommands=frozenset({"approve"}),
        overlay_settings_subcommands=frozenset({"migrate"}),
    ),
    "pr": DjangoGroup(
        "Pull request helpers.",
        [
            ("create", "Create a pull request for the ticket's branch."),
            ("merge", "[Removed] Refuses with a redirect to the §17.4 keystone (`ticket clear` + `ticket merge`)."),
            ("ensure-pr", "Create a PR for an orphan branch (idempotent)."),
            ("check-gates", "Check whether session gates allow a phase transition."),
            ("fetch-issue", "Fetch issue details from the configured tracker."),
            ("detect-tenant", "Detect the current tenant variant from the overlay."),
            ("post-test-plan", "Post a test plan as a PR comment."),
            ("post-evidence", "[Deprecated] Alias for post-test-plan (renamed; kept one release for back-compat)."),
            ("sweep", "List your open PRs across the forge for the /t3:sweeping-prs skill."),
        ],
        # `create` gate-validates against the teatree-core control DB the
        # shipping gate reads (`assert_lifecycle_db_is_canonical`), and every
        # sibling lives in the same core-only `pr.py` module. Without
        # `core_dispatch`, the overlay project-path resolver prefers any
        # `manage.py` discovered from the invoking cwd — which, from inside a
        # ticket worktree, is that worktree's OWN `manage.py`, running against
        # its per-worktree auto-isolated DB the gate never consults (#2925,
        # the same #126 class `db migrate`/`db approve` were fixed under).
        core_dispatch=True,
    ),
    "tasks": DjangoGroup(
        "Async task queue.",
        [
            ("cancel", "Cancel a task by ID."),
            ("claim", "Claim the next available task."),
            ("complete", "Mark a claimed task COMPLETED for work finished out-of-band."),
            ("create", "Enqueue the next-phase task for a ticket."),
            ("list", "List tasks with optional filters; --session scopes to the current harness session's todos."),
            ("start", "Claim and run the next interactive task in the current terminal."),
            (
                "work-next-headless",
                ("Claim and execute a headless task; refuses loop-dispatched phases while agent_runtime=interactive."),
            ),
        ],
    ),
    "queue": DjangoGroup(
        "Background-task DB queue (inspect, expire stale jobs).",
        [
            ("status", "Print the queue breakdown by status and READY jobs by task name (read-only)."),
            ("expire-stale", "Retire READY jobs older than the threshold to FAILED so a drainer never runs them."),
        ],
        core_dispatch=True,
    ),
    "followup": DjangoGroup(
        "Follow-up snapshots.",
        [
            ("refresh", "Return counts of tickets and tasks."),
            ("sync", "Synchronize followup data from MRs."),
            ("discover-mrs", "List the user's open non-draft PRs/MRs awaiting a review request."),
            ("remind", "Return list of pending user input tasks."),
        ],
        core_dispatch=True,
    ),
    "standup": DjangoGroup(
        "Auto-generated daily update (read-only).",
        [
            ("generate", "Generate a standup from transition + attempt data (read-only)."),
            ("stale", "List tickets with no activity past the staleness threshold (read-only)."),
        ],
    ),
    "checking": DjangoGroup(
        "Terse 'what did I miss' report since the last check (read-only).",
        [
            ("show", "Print grouped merged/in-flight/needs-you changes since the last check (read-only)."),
        ],
        core_dispatch=True,
    ),
    "health": DjangoGroup(
        "Global operational-health verdict + known-issues registry.",
        [
            ("show", "Reconcile and print the green/yellow/red verdict + open KnownIssue rows."),
            ("add", "Record a manual operational-health issue the deterministic signals miss."),
            ("dismiss", "Acknowledge and close an open KnownIssue by id."),
        ],
        core_dispatch=True,
    ),
    "waiting": DjangoGroup(
        "The durable 'waiting on you' lane — questions, merge authorizations, reviews, manual items.",
        [
            ("list", "List everything currently waiting on the user (all kinds), computed live."),
            ("add", "Record a manual waiting item the live sources cannot see."),
            ("resolve", "Resolve a manual waiting item by id."),
        ],
        core_dispatch=True,
    ),
    "handover": DjangoGroup(
        "Hand all current work from this session to another session.",
        [
            ("create", "Hand this session's full durable state to the loop owner, a named session, or next."),
            ("whoami", "Print this Claude session's own id."),
            ("claim-on-start", "Claim an unclaimed hand-off for a starting session (SessionStart hook entry)."),
        ],
        core_dispatch=True,
    ),
    "session": DjangoGroup(
        "Session-lifecycle operations.",
        [
            ("prepare-stop", "Refresh the durable recovery artifacts (TODO mirror, resume plan, at-risk worktrees)."),
        ],
        # ``prepare-stop`` reads the teatree-core control DB (open PRs, deferred
        # questions) — dispatch via ``python -m teatree`` so a cwd inside a ticket
        # worktree resolves core, not the worktree's per-worktree DB.
        core_dispatch=True,
    ),
    "lifecycle": DjangoGroup(
        "Session lifecycle and phase tracking.",
        [
            ("visit-phase", "Mark a phase as visited on the ticket's latest session."),
            ("clear-ledger", "Clear a reused ticket's stale phase ledger (sanctioned session-retire)."),
            ("record-review-skill-run", "Record evidence the configured review skill ran (reviewing-phase gate)."),
            ("record-review-context", "Record referenced-context retrieval before reviewing (deep-retrieval gate)."),
            ("record-e2e-run", "Record a green E2E run + posted evidence, clearing the mandatory-E2E gate (#1967)."),
            ("record-anti-vacuity", "Record the SHA-bound anti-vacuity attestation before review-request/merge."),
        ],
        # Every subcommand records phase attestations against the
        # teatree-core control DB the shipping gate reads
        # (`assert_lifecycle_db_is_canonical`) — same #2925/#126 reasoning as
        # the `pr` group above: without `core_dispatch`, a cwd inside a ticket
        # worktree resolves that worktree's own `manage.py` and its
        # per-worktree auto-isolated DB instead.
        core_dispatch=True,
    ),
    "env": DjangoGroup(
        "Inspect and mutate the worktree env cache.",
        [
            ("show", "Print the env cache as the DB would render it."),
            ("set-var", "Persist an override on the worktree and refresh the cache."),
            ("unset", "Delete an override row and refresh the cache."),
            ("overrides", "List user-declared overrides for this worktree."),
            ("check", "Exit non-zero if the on-disk cache diverges from the DB render."),
            ("migrate-secrets", "Move POSTGRES_PASSWORD literals out of .t3-env.cache into pass."),
        ],
    ),
    "ticket": DjangoGroup(
        "Ticket state management.",
        [
            ("transition", "Transition a ticket to a new state."),
            ("plan", 'Record a PlanArtifact and advance STARTED → PLANNED (`plan <id> "<text>"`).'),
            ("plan-bypass", "Record an audited PlanArtifact bypass and advance to PLANNED (--human-authorize)."),
            ("skip-planning", "Mark a trivial ticket to skip planning and advance to PLANNED (--reason, no artifact)."),
            ("plan-reconcile-inflight", "Retroactively advance STARTED tickets to PLANNED after the gate was added."),
            ("e2e-bypass", "Record a single-use user bypass of the mandatory-E2E gate (#1967)."),
            ("dod-override", "Record the DoD local-E2E gate escape hatch for a ticket (#88)."),
            ("clear", "Issue a per-diff CLEAR — the orchestrator's only merge output (BLUEPRINT §17.4.2)."),
            ("merge", "Execute the IN_REVIEW → MERGED keystone transition (BLUEPRINT §17.4)."),
            ("list", "List tickets, optionally filtered by state and/or overlay."),
            ("sync-completions", "Check post-ship tickets against upstream issues and advance completed ones."),
            ("comment", "Post a comment to an issue or work item by its URL."),
            ("create-sub", "Create a child work item nested under a parent issue/work item."),
            ("context", "Durable per-ticket knowledge store: show / add / edit (#627)."),
        ],
        core_dispatch=True,
    ),
    "review": DjangoGroup(
        "Persist + look up cold-review verdicts per MR.",
        [
            ("record", "Persist a cold-review verdict for a PR at an exact reviewed SHA."),
            ("status", "Report whether an MR is safe to approve at its current head (read-only)."),
        ],
        core_dispatch=True,
    ),
    "availability": DjangoGroup(
        "24/7 dual question-mode (#58, BLUEPRINT §17.1 invariant 9).",
        [
            ("away", "Set manual away-mode override (questions queue as DeferredQuestion rows)."),
            (
                "autonomous-away",
                "Set manual autonomous-away override (questions queue; the self-pump keeps running, #2544).",
            ),
            ("present", "Set manual present-mode override (questions ask interactively)."),
            ("auto", "Clear manual override and fall back to schedule/default."),
            ("show", "Print the currently resolved mode and source (override/schedule/default)."),
        ],
    ),
    "config_setting": DjangoGroup(
        "DB-home settings store — the sole tier for a DB-home setting below env (#1775).",
        [
            ("set", "Upsert a DB row for a DB-home setting (JSON value)."),
            ("seed", "Provenance-aware deploy seed of a DB-home setting (#3435)."),
            ("get", "Print a setting's resolved value and its source (db vs file/env)."),
            ("clear", "Remove a DB row, falling back to the dataclass default."),
            ("list", "List every DB config setting row (read-only)."),
            ("import", "Seed the DB store from operational [teatree] toml keys (one-time)."),
        ],
        core_dispatch=True,
    ),
    "approval_dial": DjangoGroup(
        "Per-action-class approval dial — graduate a class from ask to auto (#119).",
        [
            ("set", "Set an action class's trust (ask|auto) in the dial table."),
            ("clear", "Remove an action class from the dial table (falls back to ask)."),
            ("show", "Render each class's trust, never-fades floor, breach, and verdict."),
        ],
        core_dispatch=True,
    ),
    "questions": DjangoGroup(
        "Manage the away-mode deferred-question backlog (#58).",
        [
            ("record", "Record a deferred question (used by the PreToolUse away-mode hook)."),
            ("list", "List pending deferred questions, oldest first."),
            ("answer", "Resolve a pending question with a user answer."),
            ("dismiss", "Dismiss a pending question without answering it."),
            ("resurface", "Re-post the pending backlog to the user's Slack DM (away→present drain)."),
        ],
    ),
    "pending_chat": DjangoGroup(
        "Manage the inbound Slack-DM queue (#1063).",
        [
            ("list", "List inbound rows from the last hour (or --all)."),
            ("mark-answered", "Stamp ``answered_at`` on rows matching a Slack ts."),
        ],
    ),
    "notify": DjangoGroup(
        "Slack egress from the shell (#1030, #1750).",
        [
            ("send", "DM the user; exit 0 on delivery, 1 otherwise (sub-agent direct notify)."),
            ("post", "Post, token routed by destination (self-DM→bot, colleague/channel→xoxp); exit 0 on ``ok``."),
            ("react", "React, token routed by destination (self-DM→bot, colleague/channel→xoxp); exit 0 on ``ok``."),
        ],
    ),
    "mr_reminder": DjangoGroup(
        'Cross-repo "my open MRs" Slack reminder (TODO-276).',
        [
            ("preview", "Assemble the per-channel reminder read-only (no Slack post)."),
            ("send", "Post the per-channel reminder to Slack (one message per routed channel)."),
        ],
        core_dispatch=True,
    ),
    "retro": DjangoGroup(
        "Retrospective enforcement tooling (#1573).",
        [
            (
                "review-findings",
                "Classify a PR's review findings A/B/C and auto-file a deduped enforcement issue per class-C.",
            ),
            (
                "gate-failures",
                (
                    "Extract a session's gate failures, classify preventable/environmental, "
                    "and --escalate a deduped enforcement issue per recurring preventable one."
                ),
            ),
        ],
        core_dispatch=True,
    ),
    "honesty": DjangoGroup(
        "Situational honesty-critical escalation (#2263).",
        [
            (
                "escalate",
                "Record a situational escalation so the next verification spawn routes to the most-honest model.",
            ),
        ],
        core_dispatch=True,
    ),
    "memory": DjangoGroup(
        "Cold-tier memory recall (#2746).",
        [
            ("recall", "Surface the cold-tier (MEMORY_ARCHIVE.md) rules most relevant to a query (read-only)."),
        ],
        core_dispatch=True,
    ),
    "learnings": DjangoGroup(
        "Durable per-repo knowledge store, DB-placed (#2892).",
        [
            ("show", "Print the repo's durable learnings store."),
            ("add", "Append a timestamped entry to the repo's durable learnings store."),
            ("edit", "Open the repo's full learnings store in $EDITOR and replace it."),
        ],
        core_dispatch=True,
    ),
}
