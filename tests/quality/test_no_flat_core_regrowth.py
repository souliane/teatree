"""Ratchet: the flat ``src/teatree/core/`` leaf pile cannot silently regrow.

The file-hierarchy campaign clustered the flat ``core/*.py`` leaves into
cohesive subpackages (``core/cleanup/``, ``core/worktree/``, ``core/provision/``,
``core/factory/``, ``core/intake/``, ``core/review/``, ``core/evidence/``, and
``pr_create_verify`` into the existing ``core/merge/``). The remaining root
modules are the honest permanent baseline — Django app internals, own-tach-node
modules, the heavily-imported hubs, and genuinely shared leaves.

Nothing stops a new flat ``core/<leaf>.py`` from being dropped straight at the
root again — the naming convention (``cleanup_*`` / ``worktree_*`` / …) is a
de-facto namespace, but a convention is not enforcement. This ratchet is the
enforcement: it pins the exact count of flat leaf modules directly under
``src/teatree/core/`` (subpackages and ``__init__.py`` excluded).

The pin is a CEILING, and the gate is one-sided. Growth — a new leaf added at
the root instead of inside the subpackage that owns its concern — pushes the
count above the ceiling and fires; put it in the right subpackage, or raise the
ceiling in the same commit if it is a genuine new root concern. A shrink — a
leaf moved into a subpackage, or deleted outright — simply passes. Lowering the
ceiling to match banks the headroom and is welcome, but it is a one-line edit
nobody is forced to make: an equality here made every deletion red until the
integer was hand-edited downward, which taxed exactly the direction this ratchet
wants to encourage. ``tests/quality/test_ratchet_direction.py`` pins both halves.
"""

from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree" / "core"

# The post-split flat-leaf ceiling. Raise it ONLY for a genuine new root concern
# (with justification); lower it whenever a leaf legitimately moves into a
# subpackage. Never bump it to absorb a leaf that belongs in an existing package.
# 66: +send_proxy.py (#117) — the single outbound chokepoint, a flat sibling of the
# other send leaves it routes (notify.py, reply_transport.py, on_behalf_egress.py,
# backend_factory.py); a genuine new root concern, not a member of any subpackage.
# 67: +fast_push.py (directive #8) — the leak-gated fast delivery lane; a whole
# ship-flow alternative (stage → in-process leak gates → commit/push → PR upsert),
# owned by no existing subpackage (merge/ is the keystone transition, runners/ is
# the RunnerBase fleet).
# 66: -reply_retry.py (U24 hygiene) — the failed-dispatch retry sweep, an unwired
# leaf whose loop-tick integration was a deferred follow-up that never landed, so no
# production caller reached it; removed, returning the flat-core count to 66.
# 67: +issue_title.py (directive #3) — the forge issue-title resolution seam
# bridging the dashboard/new-ticket signal + the backfill command to the backend
# registry (read_issue_title + fetch_issue_title). A genuine shared root leaf: it
# must import backend_registry + overlay_loader (which core/models/ may not), so it
# cannot live under models/, and no cleanup/intake/review/… subpackage owns it.
# 69: +notify_targets.py (#3421) — the owner-DM target resolution split out of
# notify.py to keep notify.py under the 500-LOC module-health cap. A flat sibling
# of the notify leaves it serves (notify.py, send_proxy.py, reply_transport.py),
# owned by no existing subpackage.
# 70: +e2e_scenario.py (#3329/#3331) — the e2e seam value types (the authoring
# Scenario/Capture shapes + the runner→seam E2eExtrasContext). A genuine shared
# root leaf: it must be importable by BOTH teatree.core.overlay (the OverlayE2E
# seam) and _e2e_runners (the runner) with no cycle, so it cannot live under a
# management-command subpackage (layering) nor models/; no existing subpackage owns it.
# 71: +failed_e2e_watcher.py (#3329/#3331) — the FailedE2EWatcher value type split
# out of overlay.py to keep it under the 500-LOC cap once OverlayE2E grew the
# spec_paths seam. A pure overlay-config leaf consumed by the loop's
# FailedE2EPostsScanner, owned by no existing subpackage (mirrors notify_targets.py).
# 74: +connector_probes.py / messaging_tokens.py / overlay_skills.py (#3333/#3334/#3355) —
# each a flat sibling of the existing root leaves it belongs with: connector_probes
# with connector_preflight/connector_manifest/connector_keys; messaging_tokens with
# send_proxy/notify/reply_transport; overlay_skills with overlay/overlay_loader/
# overlay_metadata. None is owned by an existing subpackage.
# 75: +handover_orchestration.py (directive #8) — the hand-off/shutdown seam that
# drives in-flight sub-agent worktrees through fast-push before termination. A
# genuine new root concern bridging two flat root leaves — handover.py (the
# hand-off record + mirror) and fast_push.py (the leak-gated ship lane) — owned by
# no existing subpackage (merge/ is the keystone transition, not the hand-off seam).
# 77: +speak_cleaning.py / toml_backends.py (PR #3479 module-health split) — each a
# flat sibling of the root leaf it was carved out of to hold it under the 500-LOC
# module-health cap. speak_cleaning (the spoken-text cleaning regexes + clean_for_speech)
# sits beside speak.py; toml_backends (the path-only-TOML backend construction) sits
# beside backend_factory.py. Neither is owned by an existing subpackage — both are
# leaf helpers of a flat root hub, mirroring notify_targets.py beside notify.py.
# 78: +managers_overlay.py (PR #3479 WP9 F1.6) — the overlay-scope Q-builders
# (overlay_scope_q + for_overlay) carved out of managers.py to hold it under the
# 500-LOC module-health cap. A leaf helper of the flat root managers.py hub,
# consumed by managers.py and selectors._filters; owned by no existing subpackage,
# mirroring speak_cleaning.py beside speak.py.
# 79: +mode_resolution.py (#61 availability+preset merge) — the unified operating-mode
# resolver (resolve_active_mode + the set/clear override chokepoint). A genuine new
# core concern that MUST live at the core root: its domain-layer consumers speak.py and
# stop_snapshot.py cannot import the orchestration layer, so the resolver cannot live in
# teatree.loop; and it composes teatree.core.models + teatree.live_presence (the
# presence heartbeat) + teatree.loop.preset_resolution, fitting no existing subpackage.
# 80: +git_merge_driver.py (#3582) — the per-clone `merge.generated.driver` registration
# seam, the exact sibling of the flat git-hooks install helper prek_hook.py (both are
# per-checkout .git/config installers consumed by `t3 setup` + worktree provisioning).
# Django-free and owned by no subpackage — gates/ is gate/deny logic, not an installer.
# 92: +loop_lease_liveness.py — the ORM-free lease-liveness predicates (lease_is_live +
# live_foreign_owner_session + pid_is_foreign + anchorable_owner_pid) carved out of the
# flat loop_lease_manager.py queryset hub to hold it under the 500-LOC module-health cap.
# A pure-predicate leaf helper of that flat root hub, owned by no existing subpackage,
# mirroring managers_overlay.py beside managers.py.
# 93: +agent_admission.py — the F9 headless-lane admission chokepoint (governor consult)
# 94: +managers_task_claim.py — the claim-admission/ordering concern carved from managers.py (module health)
# 96: +notify_types.py / notify_ledger.py — carved out of notify.py to hold it under the 500-LOC
# module-health cap. notify_types (the NotifyKind/NotifyReason/NotifyOutcome/NotifyOptions value
# vocabulary) and notify_ledger (the BotPing/OutboundClaim/PendingChatInjection durable-audit ops)
# are flat leaf helpers of the flat root notify.py hub, mirroring notify_targets.py beside notify.py.
# Owned by no existing subpackage.
# 97: +managers_issue_match.py — the issue-URL alias-collapse Q-builder (matching_issue_q) carved
# out of managers.py to hold it under the 500-LOC module-health cap. A pure Q-builder leaf helper of
# the flat root managers.py queryset hub, mirroring managers_overlay.py / managers_task_claim.py.
# 98: +overlay_name_resolution.py — the overlay-name resolvers (cwd_overlay_name / overlay_name_of /
# resolve_overlay_name) carved out of overlay_loader.py to hold it under the 500-LOC module-health cap.
# A leaf helper of the flat root overlay_loader.py hub, owned by no existing subpackage.
# 99: +retention.py (#3693) — the age-based prune of the high-churn control-DB tables. A genuine new
# root data-lifecycle concern: it spans TaskAttempt + IncomingEvent + Ticket and fits no existing
# subpackage (cleanup/ is worktree/branch/stash reaping, not DB-row retention).
# 100: +forge_pr_probe.py — the single tri-state open-PR probe (find_open_pr_for_branch → FOUND/NONE/
# UNKNOWN) that unified the three hand-rolled `gh pr list` / `glab mr list` probes. A genuine shared
# root leaf bridging two gates/ modules (orphan_guard, open_pr_teardown_gate) and the flat root
# fast_push.py: gates/ is gate/deny logic, not a forge-transport probe, and no other subpackage owns it.
# 101: +managers_phase_cadence.py — the periodic-scanner dedupe/last-run Task queryset helpers
# (in_flight_for_phase + last_run_at_for_phase) carved out of managers.py to hold it under the 500-LOC
# module-health cap. A pure queryset-builder leaf helper of the flat root managers.py hub, mirroring
# managers_overlay.py / managers_issue_match.py / managers_task_claim.py. Owned by no existing subpackage.
# 102: +managers_session.py — SessionQuerySet (overlay/agent scoping + the bounded-liveness
# `live()` query) carved out of managers.py to hold it under the 500-LOC module-health cap.
# A queryset leaf helper of the flat root managers.py hub, mirroring managers_phase_cadence.py /
# managers_overlay.py / managers_issue_match.py / managers_task_claim.py. Owned by no existing
# subpackage — models/ may not import config, and this reads `session_stale_after_hours`.
# 103: +config_display.py — the setting-name secrecy taxonomy (is_secret) and the one
# value-to-display rule, relocated out of teatree.dash. Three surfaces now consume it —
# the two dash config pages and ConfigSettingAdmin — and the admin sits in the domain
# layer, which may not import the interface-layer dash. It must also stay OUT of
# core/models/ (which may not import teatree.config, and this reads the settings schema),
# so no existing subpackage owns it.
# 102: -availability.py (#3826) — the legacy availability layer is retired. Its fast-hook
# posture mirror is deleted (the hooks cold-read the control DB instead), and the two
# things that survived it were never mode concepts: the keyboard heartbeat moved DOWN to
# the foundation leaf teatree.live_presence (so the Django resolver, the cold resolver and
# the bare UserPromptSubmit hook share ONE implementation), and the two DeferredQuestion
# helpers collapsed onto DeferredQuestion.pending, which already was their whole body.
# 103: +config_seed_tables.py (#3825) — the seed half of the TOML interchange (the
# [loops]/[modes]/[schedules] classify + emit + write) carved out of config_migration.py
# to hold that hub's growth once the two export filters and the defaults-shape emitter
# landed. A leaf helper of the flat root config_migration.py hub and consumed by it
# alone; owned by no existing subpackage, mirroring speak_cleaning.py beside speak.py
# and managers_overlay.py beside managers.py.
# 102: -retention.py (#3871) — retention became the subpackage core/retention/ once it grew
# a second module. The #3693 lane orchestrator is now retention/prune.py and the adapter onto
# django_tasks_db's own shipped prune is retention/task_results.py, kept apart so the
# orchestrator never imports a third-party table's library directly. Two root leaves collapse
# to one package entry, mirroring cleanup/ and evidence/.
# 103: +invocation_cwd.py — where the operator STOOD when they invoked ``t3``, read from the
# environment the containerized ``deploy/t3`` entry point exports and degrading to ``Path.cwd()``
# when unset. A genuine new root concern that must have NO teatree deps: its consumer is the
# interface layer (``cli/overlay_dev.py``), so it cannot live under a domain subpackage, and it
# imports nothing of teatree's own — no existing subpackage owns a dependency-free
# process-environment leaf.
# 104: +projection_signals.py — the ORM half of the host-projection seam: the post_save/post_delete
# receivers that republish ``config/host_projection.py``'s payload whenever a projected control-DB
# row changes. It bridges core models and teatree.config, so core/models/ may not own it (that
# package may not import config) and config/ may not either (it may not import the ORM); the
# registration point is ``core/apps.py``'s ``ready()``, which is a root concern.
# 105: +managers_inbound.py (#3910) — the IncomingEvent/ReplyDispatch queryset predicates carved
# out of managers.py, which had reached the 500-LOC module-health ceiling and could not take
# another method. The inbound queue answers "what is still owed a drain", a different question
# from the ticket/worktree/task lifecycle the hub keeps. It lands at the root beside the four
# managers_* leaves it belongs with (managers_overlay, managers_issue_match,
# managers_phase_cadence, managers_task_claim); filing it under a subpackage would separate it
# from its siblings to satisfy a counter.
# 106: +schema_readiness.py (#3901) — the schema-vs-code admission predicate plus the
# `pending_migrations` graph walk it now owns. A genuine new root concern with no other
# home: the claim chokepoint reads it through managers_task_claim (its own tach node), and
# core/gates/schema_guard reads the same primitive, so putting it under gates/ would put
# managers on the teatree.core node it is itself a dependency OF — a cycle tach refuses.
# 107: +forge_push.py (#3927) — the `t3 push` credential-resolution + non-interactive-push seam
# (the ONE supported push path from the worker container). A flat root leaf consumed by the
# `t3 push` CLI command and `utils/git_sync.push`; owned by no existing subpackage — merge/ is
# the keystone transition, not a push-credential seam.
# 108: +managers_task_sweeps.py (#3957) — the boot-sweep concern carved out of managers.py,
# which had crossed the 500-LOC module-health ceiling and could not take another method.
# reclaim_orphaned_claims, replay_orphaned_transitions and reap_stale_claims share ONE ordering
# contract — the run_boot_sweeps rescue-before-fail sequence — so they move together or not at
# all. TaskQuerySet delegates, leaving the public API and every call site unchanged. It lands at
# the root beside the managers_* leaves it belongs with (managers_overlay, managers_inbound,
# managers_issue_match, managers_phase_cadence, managers_task_claim); filing it under a
# subpackage would separate it from its siblings to satisfy a counter.
# 109: +dispatch_admission.py (#4107) — the governor's third admission lane: the harness
# Agent/Task dispatch, which asked nothing while both factory lanes did. It lands at the
# root beside the two flat admission leaves it belongs with (admission_governor.py, the
# pure decision; agent_admission.py, the headless consult); filing it under a
# subpackage would separate it from its siblings to satisfy a counter. Its callers are
# the PreToolUse/TaskCreated gates in hooks/scripts, which tach forbids the platform-layer
# teatree.hooks node from reaching, so it cannot live there either.
# 107: -config_migration.py / -config_seed_tables.py (#4147) — the config-store <-> TOML
# interchange became the subpackage core/config_interchange/ once the withheld-key data-loss fix
# needed two more modules beside the hub: secret_guard (what must never be shared) and
# registry_rows (the merge rule that keeps a redacted export from deleting what it redacted).
# The hub and the rules its two directions must agree on move together — they are one concern,
# and letting a shared rule drift from the pair that shares it is how export and import came to
# disagree at all. Two root leaves collapse to one package entry and the two new modules never
# land at the root, mirroring retention/.
# 108: +forge_push_refs.py (#4117) — the branch-ref normalization (BranchRef + local_tip)
# carved out of forge_push.py, which crossed the 500-LOC module-health ceiling once every ref
# read and write went through one form. It answers "which string names this branch to git", a
# different question from the credential/classification/verification seam the hub keeps. It
# lands at the root beside forge_push.py, mirroring speak_cleaning.py beside speak.py and
# notify_targets.py beside notify.py; no existing subpackage owns it (merge/ is the keystone
# transition, not a push seam).
# 109: +handover_wrapup.py (#4194) — the sub-agent barrier's returns: the stored per-agent
# union, its merge, its renderer and the one-block upsert onto the row. Carved out of
# handover.py, which crossed the module-health public-function ceiling once the resolve/write
# split landed. It answers "what does each agent still owe", a different question from the
# payload/target resolution and XDG mirror the hub keeps. It lands at the root beside
# handover.py and handover_orchestration.py, the two flat leaves this concern already occupies
# by the #75 decision above, mirroring forge_push_refs.py beside forge_push.py and
# speak_cleaning.py beside speak.py; no existing subpackage owns the hand-off (merge/ is the
# keystone transition, not the hand-off seam).
# 110: +claim_liveness.py (#4164) — "is this process still executing that task claim?", the
# registry the three lease sweeps consult before reaping. A flat sibling of
# loop_lease_liveness.py, whose pid-attribution seam it reuses and whose shape it mirrors
# exactly: an ORM-free predicate layer that must be importable by core/models/ (task_claim),
# core/managers*, core/tasks AND teatree.loops with no cycle, so it can live under none of
# them; modelkit/ is a zero-dependency tach node and cannot take the loop_lease_liveness edge.
# 111: +process_freshness.py (#4387) — "is the code THIS process loaded as new as the schema
# the DB has applied?", the mirror of schema_readiness.py's deploy-order gate and its flat
# sibling by construction: the same shape (a frozen snapshot plus a memoised verdict), read
# by the same claim chokepoint through managers_task_claim, and importable by core/managers*,
# core/apps AND core/gates with no cycle. It must also stay ORM-light enough to run in
# ``AppConfig.ready()``, which rules out models/; no cleanup/factory/intake/... subpackage
# owns "what did this interpreter load".
PINNED_FLAT_CORE_MODULES = 111


def flat_core_modules(root: Path = _CORE_DIR) -> list[str]:
    """Leaf ``.py`` modules directly under *root* (no subpackages, no ``__init__``)."""
    return sorted(p.name for p in root.glob("*.py") if p.name != "__init__.py")


def exceeds_ceiling(root: Path = _CORE_DIR, ceiling: int = PINNED_FLAT_CORE_MODULES) -> bool:
    """True iff *root* carries MORE flat leaves than *ceiling* — the ratchet's whole verdict.

    One-sided on purpose. Growth is the regression this gate exists for; a
    shrink is the improvement it must never tax, so a count below the ceiling
    is simply satisfied. ``tests/quality/test_ratchet_direction.py`` drives this
    predicate over a synthetic tree in both directions.
    """
    return len(flat_core_modules(root)) > ceiling


def test_flat_core_leaf_count_is_at_or_below_the_ceiling() -> None:
    modules = flat_core_modules()
    assert not exceeds_ceiling(), (
        f"flat core leaf count is {len(modules)}, ceiling {PINNED_FLAT_CORE_MODULES}. "
        "Move the new leaf into the subpackage that owns its concern "
        "(cleanup/ worktree/ provision/ factory/ intake/ review/ evidence/ merge/), "
        "or raise the ceiling with a justification if it is a genuine new root concern.\n"
        f"current flat leaves:\n" + "\n".join(f"  {m}" for m in modules)
    )
