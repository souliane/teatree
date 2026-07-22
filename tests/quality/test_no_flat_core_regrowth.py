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

Both directions fail on purpose. Growth — a new leaf added at the root instead
of inside the subpackage that owns its concern — pushes the count above the pin;
put it in the right subpackage, or raise the pin in the same commit if it is a
genuine new root concern. Unrecorded shrink — a leaf moved into a subpackage
(welcome) — drops the count below the pin, so the pin is lowered in the same
commit, keeping every reduction an explicit reviewed decision rather than silent
drift that would reopen headroom for a future regrowth.
"""

from pathlib import Path

_CORE_DIR = Path(__file__).resolve().parents[2] / "src" / "teatree" / "core"

# The post-split flat-leaf count. Raise it ONLY for a genuine new root concern
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
# teatree.loop; and it composes teatree.core.models + teatree.core.availability (the
# presence heartbeat) + teatree.loop.preset_resolution, fitting no existing subpackage.
# 80: +git_merge_driver.py (#3582) — the per-clone `merge.generated.driver` registration
# seam, the exact sibling of the flat git-hooks install helper prek_hook.py (both are
# per-checkout .git/config installers consumed by `t3 setup` + worktree provisioning).
# Django-free and owned by no subpackage — gates/ is gate/deny logic, not an installer.
PINNED_FLAT_CORE_MODULES = 80


def _flat_core_modules() -> list[str]:
    """Leaf ``.py`` modules directly under ``core/`` (no subpackages, no ``__init__``)."""
    return sorted(p.name for p in _CORE_DIR.glob("*.py") if p.name != "__init__.py")


def test_flat_core_leaf_count_is_pinned() -> None:
    modules = _flat_core_modules()
    assert len(modules) == PINNED_FLAT_CORE_MODULES, (
        f"flat core leaf count is {len(modules)}, pinned at {PINNED_FLAT_CORE_MODULES}. "
        "A new root leaf: move it into the subpackage that owns its concern "
        "(cleanup/ worktree/ provision/ factory/ intake/ review/ evidence/ merge/), "
        "or raise the pin with a justification if it is a genuine new root concern. "
        "A removed/relocated leaf: lower the pin in the same commit.\n"
        f"current flat leaves:\n" + "\n".join(f"  {m}" for m in modules)
    )
