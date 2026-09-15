"""UserPromptSubmit: the loop OWNER's once-per-session registration emission.

Extracted from the shrink-only ``hook_router`` (#2663). It does NOT live in the
sibling ``loop_registrations``: that module is the LAYER-2 adapter whose purity
``tests/test_standing_directives_adapter.py::TestLayerPurity`` pins — it renders
directives and may carry no layer-one policy. Owner election, the emit-once
marker and the back-off are exactly that policy, so they get their own module.

Stdlib-only at import; every router internal is back-imported at CALL time, so a
test patching one on the router still steers this handler (the ``cron_tracking``
pattern).
"""

import sys

from hooks.scripts.loop_registrations import emit_loop_registrations, emit_standing_directives_once


def handle_enforce_loop_on_prompt(data: dict) -> None:
    """On first prompt, the loop OWNER registers the reactive infra ``/loop``s.

    PR-28 retired the per-enabled-DB-loop ``CronCreate`` mirror (the worker owns that
    cadence now), so this emits ONLY the three reactive infra ``/loop <duration>``
    slots (Slack-answer, self-improve, drain-queue) via the bare sibling
    :mod:`loop_registrations`. Fail-open: no reactive slot resolvable emits nothing.
    Emit-once per session, keyed on the ``loop-pending`` marker (also the
    ``_skill_loading_exempt`` bootstrap signal), so a repeated prompt does not re-nag.

    It ALSO delivers the standing directives (#4166) — same sibling, emitted
    BEFORE the owner election because the INJECTED shape reaches every engaged
    session; the sibling gates the self-waking shape itself, per slot.
    """
    from hooks.scripts.hook_router import (  # noqa: PLC0415 deferred back-import
        _claim_loop_ownership,
        _cleanup_stale_pending,
        _ensure_state_dir,
        _loop_auto_load_active,
        _session_owns_loop,
        _state_file,
    )

    session_id = data.get("session_id", "")
    if not session_id:
        return
    emit_standing_directives_once(session_id, sys.stdout)
    if not _loop_auto_load_active(session_id):
        return
    _claim_loop_ownership(session_id)
    # STICKY ELECTION (#2650): only the OWNER registers. A session that did NOT
    # win/hold the tick-owner record (a DIFFERENT live session owns it) registers
    # NOTHING and writes no pending marker — the loser backs off automatically.
    # ``_session_owns_loop`` reads what ``_claim_loop_ownership`` just decided under the
    # flock (file owner + the #1604 ``_pid_is_foreign`` DB cross-check).
    if not _session_owns_loop(session_id):
        return
    _ensure_state_dir()
    _cleanup_stale_pending(session_id)
    pending = _state_file(session_id, "loop-pending")
    if pending.is_file():  # reactive registrations already emitted this session — do not re-nag
        return
    if emit_loop_registrations(sys.stdout):
        pending.write_text("1", encoding="utf-8")


__all__ = ["handle_enforce_loop_on_prompt"]
